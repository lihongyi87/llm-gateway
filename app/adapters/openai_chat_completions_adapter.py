# OpenAI Chat Completions 兼容 Adapter（供 GLM / 智谱等 OpenAI 兼容端点）
# 与 Responses / Anthropic Messages 协议不同；SDK 对象不出本文件
import time  # 测延迟
from typing import Any, AsyncIterator, Dict, List, Optional  # 类型

from openai import AsyncOpenAI  # OpenAI SDK（chat.completions）

from app.adapters.model_adapter import ModelAdapter  # 统一接口
from app.adapters.translate import map_stop_reason, map_usage, read_int  # 字段翻译
from app.config import settings  # 配置
from app.core.errors import ErrorCode, GatewayError, is_retryable_http_status  # 错误
from app.schemas.api_request import OutputFormat  # 结构化约束
from app.schemas.common import LatencyInfo  # 延迟
from app.schemas.internal import (  # 内部模型
    InternalMessage,
    InternalRequest,
    InternalResponse,
    StreamEvent,
)


class OpenAIChatCompletionsAdapter(ModelAdapter):
    """InternalRequest ↔ Chat Completions 协议互译（OpenAI 兼容端点）。"""

    platform_model = "deepseek-v4-pro"  # 默认挂在 Pro 槽位；Router 可覆盖实例

    def __init__(
        self,
        api_key: Optional[str] = None,  # 可注入
        base_url: Optional[str] = None,  # 可注入
        platform_model: Optional[str] = None,  # 可改平台名
    ) -> None:
        if platform_model:  # 允许 Router 指定平台名
            self.platform_model = platform_model
        key = api_key if api_key is not None else settings.deepseek_pro_api_key  # 默认 Pro 槽密钥
        url = base_url if base_url is not None else settings.deepseek_pro_base_url  # 默认 Pro 地址
        self._client = AsyncOpenAI(api_key=key or "missing-key", base_url=url, max_retries=0)  # 关 SDK 重试

    def _to_messages(self, messages: List[InternalMessage]) -> List[Dict[str, str]]:
        """内部消息 → Chat Completions messages 数组。"""
        return [{"role": m.role, "content": m.content} for m in messages]  # 逐条拷贝

    def _response_format(self, output_format: Optional[OutputFormat]) -> Optional[Dict[str, Any]]:
        """网关 output_format → Chat Completions response_format。"""
        if output_format is None:  # 无约束
            return None
        if output_format.type == "json_object":  # 弱 JSON
            return {"type": "json_object"}
        if output_format.type == "json_schema" and output_format.json_schema is not None:
            schema_def = output_format.json_schema  # Schema 定义
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_def.name,
                    "schema": schema_def.schema,
                    "strict": schema_def.strict,
                },
            }
        return None

    def _usage_from(self, usage: Any) -> Any:
        """Chat Completions usage：prompt_tokens / completion_tokens → 网关字段。"""
        if usage is None:
            return map_usage()
        total_raw = read_int(usage, "total_tokens", default=-1)
        return map_usage(
            input_tokens=read_int(usage, "prompt_tokens", "input_tokens"),
            output_tokens=read_int(usage, "completion_tokens", "output_tokens"),
            total_tokens=None if total_raw < 0 else total_raw,
        )

    def _wrap(self, exc: Exception) -> GatewayError:
        """包装上游异常。"""
        status_code = getattr(exc, "status_code", None)
        if status_code is None and getattr(exc, "response", None) is not None:
            status_code = getattr(exc.response, "status_code", None)
        name = type(exc).__name__.lower()
        is_timeout = "timeout" in name
        return GatewayError(
            code=ErrorCode.UPSTREAM_TIMEOUT if is_timeout else ErrorCode.UPSTREAM_ERROR,
            message=f"Chat Completions upstream error: {exc}",
            http_status=504 if is_timeout else 502,
            retryable=is_retryable_http_status(status_code) or is_timeout,
        )

    async def invoke(self, request: InternalRequest) -> InternalResponse:
        """非流式 chat.completions.create。"""
        started = time.perf_counter()
        kwargs: Dict[str, Any] = {
            "model": request.upstream_model,
            "messages": self._to_messages(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        rf = self._response_format(request.output_format)
        if rf is not None:
            kwargs["response_format"] = rf
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc) from exc
        choice = (resp.choices or [None])[0]  # 取第一条
        content = ""  # 默认空
        finish = "stop"
        if choice is not None:
            content = getattr(getattr(choice, "message", None), "content", None) or ""
            finish = map_stop_reason(getattr(choice, "finish_reason", None))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return InternalResponse(
            content=content,
            usage=self._usage_from(getattr(resp, "usage", None)),
            stop_reason=finish,
            latency=LatencyInfo(total_ms=elapsed_ms),
        )

    async def stream(self, request: InternalRequest) -> AsyncIterator[StreamEvent]:
        """流式 chat.completions.create(stream=True)。"""
        kwargs: Dict[str, Any] = {
            "model": request.upstream_model,
            "messages": self._to_messages(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": True,
        }
        rf = self._response_format(request.output_format)
        if rf is not None:
            kwargs["response_format"] = rf
        # 部分兼容端点支持 stream_options.include_usage
        kwargs["stream_options"] = {"include_usage": True}
        final_usage = map_usage()
        stop_reason = "stop"
        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for event in stream:
                usage = getattr(event, "usage", None)
                if usage is not None:
                    final_usage = self._usage_from(usage)
                choices = getattr(event, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    yield StreamEvent(type="text_delta", content=str(text))
                fr = getattr(choice, "finish_reason", None)
                if fr:
                    stop_reason = map_stop_reason(str(fr))
            yield StreamEvent(type="usage", usage=final_usage)
            yield StreamEvent(type="done", stop_reason=stop_reason)
        except TypeError:
            # 部分端点不支持 stream_options：去掉后重试一次流式
            kwargs.pop("stream_options", None)
            try:
                stream = await self._client.chat.completions.create(**kwargs)
                async for event in stream:
                    choices = getattr(event, "choices", None) or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) if delta is not None else None
                    if text:
                        yield StreamEvent(type="text_delta", content=str(text))
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        stop_reason = map_stop_reason(str(fr))
                yield StreamEvent(type="usage", usage=final_usage)
                yield StreamEvent(type="done", stop_reason=stop_reason)
            except Exception as exc:  # noqa: BLE001
                wrapped = self._wrap(exc)
                yield StreamEvent(type="error", error_code=wrapped.code, error_message=wrapped.message)
        except Exception as exc:  # noqa: BLE001
            wrapped = self._wrap(exc)
            yield StreamEvent(type="error", error_code=wrapped.code, error_message=wrapped.message)
