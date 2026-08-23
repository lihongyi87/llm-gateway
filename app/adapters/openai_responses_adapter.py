# OpenAI Responses API Adapter（可选，默认未挂进 ModelRouter）
# 现网 Pro 槽走 Chat Completions；本文件保留给需要 Responses 协议时接入
# 职责：InternalRequest ↔ Responses 协议互译；SDK 对象不出本文件
import time  # 测量 total_ms / ttft_ms
from typing import Any, AsyncIterator, Dict, List, Optional  # 类型注解

from openai import AsyncOpenAI  # OpenAI 官方异步客户端（Responses API）

from app.adapters.model_adapter import ModelAdapter  # 统一接口
from app.adapters.translate import map_stop_reason, map_usage, read_int  # 字段翻译
from app.config import settings  # 读取 Key / Base URL
from app.core.errors import ErrorCode, GatewayError, is_retryable_exception  # 统一错误与重试分类
from app.schemas.api_request import OutputFormat  # 网关结构化约束
from app.schemas.common import LatencyInfo  # 延迟结构
from app.schemas.internal import (  # 内部请求/响应/流事件
    InternalMessage,
    InternalRequest,
    InternalResponse,
    StreamEvent,
)


class OpenAIResponsesAdapter(ModelAdapter):
    """把网关内部模型翻译成 OpenAI Responses API，再翻回来。"""

    platform_model = "deepseek-v4-pro"  # 路由表用的平台名

    def __init__(
        self,
        api_key: Optional[str] = None,  # 可注入，方便测试
        base_url: Optional[str] = None,  # 可注入上游地址
    ) -> None:
        key = api_key if api_key is not None else settings.deepseek_pro_api_key  # 默认读配置
        url = base_url if base_url is not None else settings.deepseek_pro_base_url  # 默认读配置
        # max_retries=0：禁止 SDK 隐式重试，重试只在网关 retry_async
        self._client = AsyncOpenAI(api_key=key or "missing-key", base_url=url, max_retries=0)

    def _to_input_items(self, messages: List[InternalMessage]) -> List[Dict[str, Any]]:
        """InternalMessage 列表 → Responses API 的 input 数组。"""
        items: List[Dict[str, Any]] = []  # 累积上游 input
        for msg in messages:  # 逐条翻译
            items.append(
                {
                    "role": msg.role,  # system / user / assistant / tool
                    "content": msg.content,  # 纯文本
                }
            )
        return items  # 返回厂商 input

    def _to_text_format(self, output_format: Optional[OutputFormat]) -> Optional[Dict[str, Any]]:
        """
        网关 output_format → Responses API 的 text.format。
        这是厂商字段，只在本 Adapter 内出现。
        """
        if output_format is None:  # 自由文本
            return None
        if output_format.type == "json_object":  # 只要合法 JSON
            return {"format": {"type": "json_object"}}
        if output_format.type == "json_schema" and output_format.json_schema is not None:
            schema_def = output_format.json_schema  # SchemaDefinition
            return {
                "format": {
                    "type": "json_schema",  # 厂商字段名
                    "name": schema_def.name,  # Schema 名
                    "schema": schema_def.schema_body,  # 厂商 JSON 键仍叫 schema
                    "strict": schema_def.strict,  # 严格模式
                }
            }
        return None  # 其它情况不附加

    def _extract_output_text(self, response: Any) -> str:
        """从 Responses 对象提取助手文本；优先用 output_text，否则遍历 output。"""
        text = getattr(response, "output_text", None)  # SDK 便捷属性
        if isinstance(text, str) and text:  # 非空字符串直接用
            return text
        parts: List[str] = []  # 拼接多段文本
        for item in getattr(response, "output", None) or []:  # 遍历输出项
            for content in getattr(item, "content", None) or []:  # 每项的 content 列表
                if getattr(content, "type", None) in {"output_text", "text"}:  # 文本类型
                    value = getattr(content, "text", None)  # 取 text 字段
                    if value:  # 有内容才追加
                        parts.append(str(value))
        return "".join(parts)  # 拼成完整字符串

    def _usage_from_response(self, response: Any) -> Any:
        """从 Responses 对象读 usage，兼容 input_tokens / prompt_tokens 两种命名。"""
        usage = getattr(response, "usage", None)  # 上游 usage 对象
        total_raw = read_int(usage, "total_tokens", default=-1)  # -1 表示上游没给合计
        return map_usage(
            input_tokens=read_int(usage, "input_tokens", "prompt_tokens"),  # 输入（兼容旧名）
            output_tokens=read_int(usage, "output_tokens", "completion_tokens"),  # 输出（兼容旧名）
            total_tokens=None if total_raw < 0 else total_raw,  # 缺省交给 map_usage 自算
            cached_tokens=read_int(usage, "cached_tokens"),  # 缓存（有则填）
            reasoning_tokens=read_int(usage, "reasoning_tokens"),  # 推理（有则填）
        ) if usage is not None else map_usage()  # 无 usage 则全 0

    def _wrap_upstream_error(self, exc: Exception) -> GatewayError:
        """把 OpenAI SDK 异常包装成 GatewayError，供 retry_async 判断是否重试。"""
        name = type(exc).__name__.lower()  # 异常类名小写
        is_timeout = "timeout" in name  # 粗判超时
        code = ErrorCode.UPSTREAM_TIMEOUT if is_timeout else ErrorCode.UPSTREAM_ERROR  # 选错误码
        http_status = 504 if is_timeout else 502  # 超时用 504
        return GatewayError(
            code=code,  # 稳定错误码
            message=f"OpenAI Responses upstream error: {exc}",  # 不含 Key
            http_status=http_status,  # HTTP 建议码
            retryable=is_retryable_exception(exc) or is_timeout,  # TypeError/4xx 不重试
        )

    async def invoke(self, request: InternalRequest) -> InternalResponse:
        """非流式：调用 responses.create，返回 InternalResponse。"""
        started = time.perf_counter()  # 开始计时
        kwargs: Dict[str, Any] = {
            "model": request.upstream_model,  # 上游真实模型 ID
            "input": self._to_input_items(request.messages),  # 翻译后的 input
            "temperature": request.temperature,  # 采样温度
            "max_output_tokens": request.max_tokens,  # Responses 用 max_output_tokens
        }
        text_format = self._to_text_format(request.output_format)  # 结构化输出
        if text_format is not None:  # 有约束才附加
            kwargs["text"] = text_format  # 厂商字段 text.format
        try:
            response = await self._client.responses.create(**kwargs)  # 真正打上游
        except Exception as exc:  # noqa: BLE001 — SDK 异常统一包装
            raise self._wrap_upstream_error(exc) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000.0  # 总耗时毫秒
        content = self._extract_output_text(response)  # 提取文本
        usage = self._usage_from_response(response)  # 翻译用量
        # Responses 的 status / incomplete 细节简化为 stop_reason
        raw_reason = getattr(response, "status", None)  # 可能是 completed
        stop = map_stop_reason("stop" if raw_reason in (None, "completed") else str(raw_reason))
        return InternalResponse(
            content=content,  # 助手文本
            usage=usage,  # 统一用量
            stop_reason=stop,  # 统一结束原因
            latency=LatencyInfo(ttft_ms=None, generation_ms=None, total_ms=elapsed_ms),  # 非流式暂不拆 TTFT
        )

    async def stream(self, request: InternalRequest) -> AsyncIterator[StreamEvent]:
        """流式：边收 Responses 事件边 yield StreamEvent。"""
        started = time.perf_counter()  # 请求开始时刻
        first_token_at: Optional[float] = None  # 首个有内容 delta 的时刻
        kwargs: Dict[str, Any] = {
            "model": request.upstream_model,  # 上游模型
            "input": self._to_input_items(request.messages),  # input 数组
            "temperature": request.temperature,  # 温度
            "max_output_tokens": request.max_tokens,  # 输出上限
            "stream": True,  # 打开流式
        }
        text_format = self._to_text_format(request.output_format)  # 结构化（流式同样可带）
        if text_format is not None:
            kwargs["text"] = text_format
        try:
            stream = await self._client.responses.create(**kwargs)  # 返回异步事件流
            final_usage = map_usage()  # 累积最终用量
            stop_reason = "stop"  # 默认结束原因
            async for event in stream:  # 逐个厂商事件
                event_type = getattr(event, "type", "") or ""  # 事件类型字符串
                # Responses 文本增量常见类型：response.output_text.delta
                if event_type.endswith("output_text.delta") or event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", None)  # 增量文本
                    if delta:  # 非空才算首 token
                        if first_token_at is None:
                            first_token_at = time.perf_counter()  # 记录 TTFT 起点
                        yield StreamEvent(type="text_delta", content=str(delta))  # 内部事件
                # 完成事件：尽量读 usage
                if event_type in {"response.completed", "response.incomplete"}:
                    response_obj = getattr(event, "response", None)  # 完整响应对象
                    if response_obj is not None:
                        final_usage = self._usage_from_response(response_obj)  # 翻译用量
                        raw = getattr(response_obj, "status", None)
                        stop_reason = map_stop_reason(
                            "stop" if raw in (None, "completed") else str(raw)
                        )
            yield StreamEvent(type="usage", usage=final_usage)  # 先发用量事件
            yield StreamEvent(type="done", stop_reason=stop_reason)  # 再发终态
            _ = started, first_token_at  # 保留变量供后续 Gateway 侧 TTFT 计算扩展
        except Exception as exc:  # noqa: BLE001
            wrapped = self._wrap_upstream_error(exc)  # 包装错误
            yield StreamEvent(
                type="error",
                error_code=wrapped.code,  # 稳定错误码
                error_message=wrapped.message,  # 说明
            )
