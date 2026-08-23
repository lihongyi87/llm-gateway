# Gateway 编排服务：限流 → 路由 → Prompt → Adapter(invoke/stream) → 重试 → Trace
import json  # 结构化输出本地校验
import time  # 流式 TTFT / total 计时
from typing import AsyncIterator, List, Optional  # 类型注解

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

    def _validate_structured_content(self, content: str, output_format: Optional[OutputFormat]) -> None:
        """
        本地校验模型输出（全有或全无）。
        供应商声称支持 Schema ≠ 应用层可以省略校验。
        """
        if output_format is None:  # 自由文本不校验
            return
        try:
            parsed = json.loads(content)  # 必须是合法 JSON
        except json.JSONDecodeError as exc:
            raise GatewayError(
                code=ErrorCode.SCHEMA_VALIDATION_FAILED,
                message=f"model output is not valid JSON: {exc}",
                http_status=422,
                retryable=False,
            ) from exc
        if output_format.type == "json_object":  # 弱约束：只要是 JSON
            return
        if output_format.type == "json_schema" and output_format.json_schema is not None:
            schema = output_format.json_schema.schema  # JSON Schema 字典
            if not isinstance(parsed, dict):  # 根必须是 object（本网关简化假设）
                raise GatewayError(
                    code=ErrorCode.SCHEMA_VALIDATION_FAILED,
                    message="json_schema output root must be an object",
                    http_status=422,
                    retryable=False,
                )
            required = schema.get("required") or []  # 必填字段列表
            missing = [k for k in required if k not in parsed]  # 缺字段
            if missing:
                raise GatewayError(
                    code=ErrorCode.SCHEMA_VALIDATION_FAILED,
                    message=f"json_schema missing required fields: {', '.join(missing)}",
                    http_status=422,
                    retryable=False,
                )

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
            self._validate_structured_content(result.content, request.output_format)  # ⑥ 出口校验
            self._save_ok_trace(  # ⑦ 写成功 Trace
                trace_id=trace_id,
                route=route,
                rendered=rendered,
                usage=result.usage,
                latency=result.latency,
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

    async def stream(self, request: InvokeRequest) -> AsyncIterator[StreamChunk]:
        """
        流式编排：把 StreamEvent 翻译成 StreamChunk。
        注意：首 token 发出后不再换模型重试（本实现流式不做整段重试）。
        """
        trace_id = self._traces.new_trace_id()  # 追踪 ID
        route: Optional[RouteResult] = None
        rendered: Optional[PromptRenderResult] = None
        started = time.perf_counter()  # 总耗时起点
        first_token_at: Optional[float] = None  # 首个有内容 token 时刻
        final_usage = UsageInfo()  # 最终用量
        stop_reason: Optional[str] = "stop"  # 结束原因
        try:
            self._limiter.acquire(request.model)  # 限流
            self._validate_output_format(request.output_format)  # 入站校验
            route = self._router.resolve(request.model)  # 路由
            messages, rendered = self._build_messages(request)  # 模板
            internal = self._to_internal_request(
                request, route, messages, trace_id, rendered, stream=True
            )
            adapter: ModelAdapter = route.adapter  # 选中的 Adapter
            async for event in adapter.stream(internal):  # 消费内部事件
                if event.type == "text_delta":
                    text = event.content or ""  # 增量文本
                    if text and first_token_at is None:  # 首个非空内容
                        first_token_at = time.perf_counter()
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
                    stop_reason = event.stop_reason or "stop"
                    yield StreamChunk(  # 最后一帧
                        id=trace_id,
                        model=route.platform_model,
                        text="",
                        done=True,
                        stop_reason=stop_reason,
                        usage=final_usage,
                    )
                elif event.type == "error":
                    raise GatewayError(
                        code=event.error_code or ErrorCode.UPSTREAM_ERROR,
                        message=event.error_message or "stream error",
                        http_status=502,
                        retryable=False,
                    )
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
            raise

    def get_trace(self, trace_id: str) -> TraceRecord:
        """按 id 查询 TraceRecord，供 GET /v1/traces/{id} 使用。"""
        return self._traces.get(trace_id)  # 不存在则抛 unknown_trace


# 进程内默认网关服务
gateway_service = GatewayService()
