# Gateway 编排服务：限流 → 路由 → Prompt → Adapter(invoke/stream) → 重试 → Trace
import json
import re  # 结构化输出本地校验
import time  # 流式 TTFT / total 计时
from typing import Any, AsyncIterator, List, Optional  # 类型注解

from app.adapters.model_adapter import ModelAdapter  # Adapter 类型
from app.core.errors import ErrorCode, GatewayError  # 统一错误
from app.schemas.api_request import InvokeRequest, Message, OutputFormat  # 入站
from app.schemas.api_response import InvokeResponse, StreamChunk  # 出站
from app.schemas.common import LatencyInfo, UsageInfo  # 用量延迟
from app.schemas.internal import (  # 内部领域
    InternalMessage,
    InternalRequest,
    InternalResponse,
    StreamEvent,
)
from app.schemas.trace import TraceRecord  # Trace 查询返回类型
from app.services.model_router import ModelRouter, RouteResult, model_router  # 路由
from app.services.prompt_service import PromptRenderResult, PromptService, prompt_service  # 模板
from app.services.rate_limiter import PerModelRateLimiter, rate_limiter  # 限流
from app.services.retry import retry_async  # 重试
from app.services.trace_store import TraceStore, trace_store  # 观测


class GatewayService:
    """
    统一调用编排。HTTP 层只调本类的 invoke / stream / get_trace。
    厂商协议差异到此为止：本类只认 Internal* 与对外 Invoke*。
    """

    def __init__(
        self,
        router: Optional[ModelRouter] = None,  # 可注入，方便测试
        prompts: Optional[PromptService] = None,  # 可注入
        limiter: Optional[PerModelRateLimiter] = None,  # 可注入
        traces: Optional[TraceStore] = None,  # 可注入
    ) -> None:
        self._router = router or model_router  # 默认全局路由
        self._prompts = prompts or prompt_service  # 默认 Prompt 服务
        self._limiter = limiter or rate_limiter  # 默认限流器
        self._traces = traces or trace_store  # 默认 Trace 仓库

    def _validate_output_format(self, output_format: Optional[OutputFormat]) -> None:
        """入站结构化约束自检：json_schema 类型必须带 schema 定义。"""
        if output_format is None:  # 无约束
            return
        if output_format.type == "json_schema" and output_format.json_schema is None:
            raise GatewayError(
                code=ErrorCode.INVALID_REQUEST,
                message="output_format.type=json_schema requires json_schema field",
                http_status=400,
                retryable=False,
            )

    def _json_type_matches(self, value: Any, expected: str) -> bool:
        """对照 JSON Schema 的 type 字符串检查 Python 值。"""
        if expected == "string":  # JSON string
            return isinstance(value, str)
        if expected == "number":  # JSON number：int/float，排除 bool（bool 是 int 子类）
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "integer":  # JSON integer
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "boolean":  # JSON boolean
            return isinstance(value, bool)
        if expected == "object":  # JSON object
            return isinstance(value, dict)
        if expected == "array":  # JSON array
            return isinstance(value, list)
        if expected == "null":  # JSON null
            return value is None
        return True  # 未识别的 type 不硬拦，避免误杀扩展关键字

    def _matches_schema_type(self, value: Any, expected: Any) -> bool:
        """type 可能是字符串或数组（如 ["string","null"]）。"""
        if isinstance(expected, list):  # 联合类型：命中任一即可
            return any(self._json_type_matches(value, item) for item in expected if isinstance(item, str))
        if isinstance(expected, str):  # 单一类型
            return self._json_type_matches(value, expected)
        return True  # 没有 type 约束则放过

    def _fail_schema(self, message: str) -> None:
        """统一抛出结构化校验失败。"""
        raise GatewayError(
            code=ErrorCode.SCHEMA_VALIDATION_FAILED,  # 稳定错误码
            message=message,  # 说明缺字段 / 类型错 / 多余字段
            http_status=422,  # 语义上是内容无法通过约束
            retryable=False,  # 换请求或换模型输出才能好
        )

    def _validate_structured_content(self, content: str, output_format: Optional[OutputFormat]) -> None:
        """
        本地校验模型输出（全有或全无）。
        供应商声称支持 Schema ≠ 应用层可以省略校验。
        json_schema：必填 + 类型 + strict/additionalProperties=false 时禁多余字段。
        """
        if output_format is None:  # 自由文本不校验
            return
        # 宽松提取（思考型模型实测会包 ```json 围栏或带前导文字，
        # 裸 loads 首字符必挂）：strip → 剥围栏 → 首个 {...} 块 三通道
        candidates = [content.strip()]
        fence = re.search(r"```(?:json)?\s*(.+?)```", content, re.S)
        if fence:
            candidates.insert(0, fence.group(1).strip())
        brace = re.search(r"\{.*\}", content, re.S)
        if brace:
            candidates.insert(1, brace.group(0))
        parsed = None
        last_exc: Exception = None
        for cand in candidates:
            try:
                parsed = json.loads(cand)
                break
            except json.JSONDecodeError as exc:
                last_exc = exc
        if parsed is None:
            raise GatewayError(
                code=ErrorCode.SCHEMA_VALIDATION_FAILED,
                message=f"model output is not valid JSON: {last_exc}",
                http_status=422,
                retryable=False,
            ) from last_exc
        if output_format.type == "json_object":  # 弱约束：只要是 JSON
            return
        if output_format.type == "json_schema" and output_format.json_schema is not None:
            schema_def = output_format.json_schema  # SchemaDefinition
            schema = schema_def.schema_body  # JSON Schema 字典（字段名避开 BaseModel.schema）
            if not isinstance(parsed, dict):  # 根必须是 object（本网关简化假设）
                self._fail_schema("json_schema output root must be an object")
            required = schema.get("required") or []  # 必填字段列表
            missing = [k for k in required if k not in parsed]  # 缺字段
            if missing:
                self._fail_schema(f"json_schema missing required fields: {', '.join(missing)}")
            properties = schema.get("properties") or {}  # 已声明字段
            # strict=True 或 additionalProperties=false：禁止未声明字段
            deny_extra = schema_def.strict or (schema.get("additionalProperties") is False)
            if deny_extra:
                extra = [k for k in parsed if k not in properties]  # 多出来的键
                if extra:
                    self._fail_schema(f"json_schema unexpected fields: {', '.join(extra)}")
            for key, spec in properties.items():  # 逐个已声明字段做类型检查
                if key not in parsed:  # 非必填且未出现：跳过
                    continue
                if not isinstance(spec, dict):  # 属性描述不是对象则无法检查
                    continue
                expected_type = spec.get("type")  # JSON Schema type
                if expected_type is not None and not self._matches_schema_type(parsed[key], expected_type):
                    self._fail_schema(f"json_schema field '{key}' has wrong type")
                enum_values = spec.get("enum")  # 枚举约束
                if isinstance(enum_values, list) and parsed[key] not in enum_values:
                    self._fail_schema(f"json_schema field '{key}' is not in enum")

    def _build_messages(
        self,
        request: InvokeRequest,
    ) -> tuple[List[InternalMessage], Optional[PromptRenderResult]]:
        """把入站 messages + 可选模板渲染，合成 InternalMessage 列表。"""
        rendered: Optional[PromptRenderResult] = None  # 默认无模板
        internal: List[InternalMessage] = []  # 结果列表
        if request.prompt_template is not None:  # 引用了模板
            ref = request.prompt_template  # PromptTemplateRef
            rendered = self._prompts.render(ref.name, ref.version, ref.variables)  # 受限渲染
            # 模板内容作为 system，插在最前
            internal.append(InternalMessage(role="system", content=rendered.content))
        for msg in request.messages:  # 追加客户端消息
            internal.append(InternalMessage(role=msg.role, content=msg.content))
        if not internal:  # 既无模板也无消息
            raise GatewayError(
                code=ErrorCode.INVALID_REQUEST,
                message="either messages or prompt_template is required",
                http_status=400,
                retryable=False,
            )
        return internal, rendered  # 消息 + 模板元数据

    def _to_internal_request(
        self,
        request: InvokeRequest,
        route: RouteResult,
        messages: List[InternalMessage],
        trace_id: str,
        rendered: Optional[PromptRenderResult],
        stream: bool,
    ) -> InternalRequest:
        """组装交给 Adapter 的 InternalRequest。"""
        return InternalRequest(
            model=route.platform_model,  # 平台名
            upstream_model=route.upstream_model,  # 上游 ID
            messages=messages,  # 已渲染消息
            stream=stream,  # 是否流式
            temperature=request.temperature if request.temperature is not None else 0.7,
            max_tokens=request.max_tokens if request.max_tokens is not None else 1024,
            output_format=request.output_format,  # 结构化约束原样下传
            trace_id=trace_id,  # 追踪 ID
            prompt_name=rendered.name if rendered else None,  # 模板名
            prompt_version=rendered.version if rendered else None,  # 模板版本
        )

    def _save_ok_trace(
        self,
        *,
        trace_id: str,
        route: RouteResult,
        rendered: Optional[PromptRenderResult],
        usage: UsageInfo,
        latency: LatencyInfo,
        retry_count: int,
        stop_reason: Optional[str],
    ) -> None:
        """写入成功 Trace。"""
        record = self._traces.build_record(
            trace_id=trace_id,
            model=route.platform_model,
            resolved_upstream_model=route.upstream_model,
            usage=usage,
            latency=latency,
            retry_count=retry_count,
            status="ok",
            stop_reason=stop_reason,
            prompt_name=rendered.name if rendered else None,
            prompt_version=rendered.version if rendered else None,
            prompt_hash=rendered.content_hash if rendered else None,
        )
        self._traces.save(record)  # 持久到内存仓库

    def _save_error_trace(
        self,
        *,
        trace_id: str,
        route: Optional[RouteResult],
        rendered: Optional[PromptRenderResult],
        retry_count: int,
        error: GatewayError,
        model_hint: str,
    ) -> None:
        """写入失败 Trace（路由失败时 route 可能为 None）。"""
        record = self._traces.build_record(
            trace_id=trace_id,
            model=route.platform_model if route else model_hint,
            resolved_upstream_model=route.upstream_model if route else "",
            usage=UsageInfo(),
            latency=LatencyInfo(),
            retry_count=retry_count,
            status="error",
            error_code=error.code,
            prompt_name=rendered.name if rendered else None,
            prompt_version=rendered.version if rendered else None,
            prompt_hash=rendered.content_hash if rendered else None,
        )
        self._traces.save(record)

    def _normalize_nonstream_latency(self, latency: LatencyInfo) -> LatencyInfo:
        """
        非流式兜底：Adapter 若只填了 total_ms，把 ttft 补成同一值。
        含义：完整 JSON 到达的时刻 = 客户端第一次看见内容的时刻。
        """
        if latency.ttft_ms is not None:  # Adapter 已经填了
            return latency
        return LatencyInfo(
            ttft_ms=latency.total_ms,  # 与总耗时同刻
            generation_ms=0.0 if latency.generation_ms is None else latency.generation_ms,
            total_ms=latency.total_ms,
        )

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        """非流式完整调用编排。"""
        trace_id = self._traces.new_trace_id()  # 先发号，保证失败也能查
        route: Optional[RouteResult] = None  # 路由结果
        rendered: Optional[PromptRenderResult] = None  # 模板结果
        retry_count = 0  # 重试次数
        try:
            self._limiter.acquire(request.model)  # ① 按模型限流
            self._validate_output_format(request.output_format)  # ② 入站 Schema 自检
            route = self._router.resolve(request.model)  # ③ 选 Adapter
            messages, rendered = self._build_messages(request)  # ④ 渲染模板
            internal = self._to_internal_request(
                request, route, messages, trace_id, rendered, stream=False
            )

            async def _call() -> InternalResponse:
                """包一层无参协程，供 retry_async 反复调用。"""
                return await route.adapter.invoke(internal)  # type: ignore[union-attr]

            result, retry_count = await retry_async(  # ⑤ 有界重试
                _call,
                operation_name=f"invoke:{route.platform_model}",
            )
            # ⑥ 出口校验 + 修复重试（≤1 次）：实测部分"OpenAI Compatible"端点
            # 无视 response_format 返回散文——Schema 注入重问一次（Anthropic
            # 同款方案；全有或全无，修不好仍按校验失败拒绝）
            if request.output_format is not None:
                try:
                    self._validate_structured_content(
                        result.content, request.output_format)
                except GatewayError:
                    schema_txt = request.output_format.model_dump_json(
                        exclude_none=True)
                    repair_messages = [m.model_copy(deep=True)
                                       for m in internal.messages]
                    if repair_messages:
                        last = repair_messages[-1]
                        repair_messages[-1] = last.model_copy(update={
                            'content': (last.content + chr(10) + chr(10)
                                        + '【输出契约·最高优先级】只输出'
                                        '一个符合此 JSON Schema 的 JSON 对象，'
                                        '禁止 Markdown 围栏与解释文字：'
                                        + schema_txt)})
                    repair_internal = internal.model_copy(update={
                        'messages': repair_messages})
                    result2, _r2 = await retry_async(
                        lambda: route.adapter.invoke(repair_internal),  # type: ignore
                        operation_name=f"invoke-repair:{route.platform_model}")
                    self._validate_structured_content(
                        result2.content, request.output_format)
                    result = result2
                    retry_count += 1
            latency = self._normalize_nonstream_latency(result.latency)  # 非流式补 ttft
            self._save_ok_trace(  # ⑦ 写成功 Trace
                trace_id=trace_id,
                route=route,
                rendered=rendered,
                usage=result.usage,
                latency=latency,
                retry_count=retry_count,
                stop_reason=result.stop_reason,
            )
            return InvokeResponse(  # ⑧ 对外响应
                id=trace_id,
                model=route.platform_model,
                message=Message(role="assistant", content=result.content),
                stop_reason=result.stop_reason,
                usage=result.usage,
                trace_id=trace_id,
            )
        except GatewayError as exc:
            self._save_error_trace(
                trace_id=trace_id,
                route=route,
                rendered=rendered,
                retry_count=retry_count,
                error=exc,
                model_hint=request.model,
            )
            raise  # 交给 HTTP 层转 ErrorResponse

    def _stream_error_chunk(self, trace_id: str, model: str, error: GatewayError) -> StreamChunk:
        """流已经对外吐过帧时，用终态错误帧收尾，避免只在生成器里 raise 把 SSE 掐断。"""
        return StreamChunk(
            id=trace_id,  # 与前面帧同一调用 ID
            model=model,  # 平台模型名
            text="",  # 错误帧不带增量文本
            done=True,  # 告诉客户端流结束
            error_code=error.code,  # 稳定错误码
            error_message=error.message,  # 人类可读说明
        )

    async def stream(self, request: InvokeRequest) -> AsyncIterator[StreamChunk]:
        """
        流式编排：把 StreamEvent 翻译成 StreamChunk。
        首 token 发出后不再换模型重试；中途失败改发 SSE 错误帧，不再只 raise。
        若带 output_format，流结束后对拼接全文做与非流式相同的本地校验。
        """
        trace_id = self._traces.new_trace_id()  # 追踪 ID
        route: Optional[RouteResult] = None
        rendered: Optional[PromptRenderResult] = None
        started = time.perf_counter()  # 总耗时起点
        first_token_at: Optional[float] = None  # 首个有内容 token 时刻
        final_usage = UsageInfo()  # 最终用量
        stop_reason: Optional[str] = "stop"  # 结束原因
        emitted = False  # 是否已经向客户端 yield 过帧
        collected: List[str] = []  # 累积文本，供流结束结构化校验
        try:
            self._limiter.acquire(request.model)  # 限流
            self._validate_output_format(request.output_format)  # 入站校验
            route = self._router.resolve(request.model)  # 路由
            messages, rendered = self._build_messages(request)  # 模板
            internal = self._to_internal_request(
                request, route, messages, trace_id, rendered, stream=True
            )
            adapter: ModelAdapter = route.adapter  # 选中的 Adapter
            saw_done = False  # 上游是否发过 done；没发也要在循环后补校验
            async for event in adapter.stream(internal):  # 消费内部事件
                if event.type == "text_delta":
                    text = event.content or ""  # 增量文本
                    if text and first_token_at is None:  # 首个非空内容
                        first_token_at = time.perf_counter()
                    if text:  # 只把有内容的增量拼进校验缓冲
                        collected.append(text)
                    emitted = True  # 已经对外发过帧
                    yield StreamChunk(  # 对外扁平帧
                        id=trace_id,
                        model=route.platform_model,
                        text=text,
                        done=False,
                    )
                elif event.type == "usage":
                    if event.usage is not None:
                        final_usage = event.usage  # 记下用量
                elif event.type == "done":
                    saw_done = True  # 已收到终态
                    stop_reason = event.stop_reason or "stop"
                    # 流结束也做结构化校验，与非流式同一套规则
                    self._validate_structured_content("".join(collected), request.output_format)
                    emitted = True
                    yield StreamChunk(  # 最后一帧
                        id=trace_id,
                        model=route.platform_model,
                        text="",
                        done=True,
                        stop_reason=stop_reason,
                        usage=final_usage,
                    )
                elif event.type == "error":
                    raise GatewayError(  # 先转统一错误，下面按是否已吐帧决定 raise 或错误帧
                        code=event.error_code or ErrorCode.UPSTREAM_ERROR,
                        message=event.error_message or "stream error",
                        http_status=502,
                        retryable=False,
                    )
            if not saw_done and request.output_format is not None:  # 上游没发 done 也要验全文
                self._validate_structured_content("".join(collected), request.output_format)
            # 流正常结束后写 Trace（含 TTFT）
            ended = time.perf_counter()
            ttft_ms = None
            generation_ms = None
            if first_token_at is not None:
                ttft_ms = (first_token_at - started) * 1000.0
                generation_ms = (ended - first_token_at) * 1000.0
            total_ms = (ended - started) * 1000.0
            self._save_ok_trace(
                trace_id=trace_id,
                route=route,
                rendered=rendered,
                usage=final_usage,
                latency=LatencyInfo(ttft_ms=ttft_ms, generation_ms=generation_ms, total_ms=total_ms),
                retry_count=0,
                stop_reason=stop_reason,
            )
        except GatewayError as exc:
            self._save_error_trace(
                trace_id=trace_id,
                route=route,
                rendered=rendered,
                retry_count=0,
                error=exc,
                model_hint=request.model,
            )
            if emitted:  # 已经开始 SSE，不能再改 HTTP 状态，改发错误终态帧
                model_name = route.platform_model if route is not None else request.model
                yield self._stream_error_chunk(trace_id, model_name, exc)
                return
            raise  # 一帧都没发：交给 HTTP 层返回 ErrorResponse JSON

    def get_trace(self, trace_id: str) -> TraceRecord:
        """按 id 查询 TraceRecord，供 GET /v1/traces/{id} 使用。"""
        return self._traces.get(trace_id)  # 不存在则抛 unknown_trace


# 进程内默认网关服务
gateway_service = GatewayService()
