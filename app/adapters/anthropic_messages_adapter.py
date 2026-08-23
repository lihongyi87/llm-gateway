# Anthropic Messages API Adapter：供 deepseek-v4-flash 使用
# 职责：InternalRequest ↔ Messages 协议互译；SDK 对象不出本文件
import time  # 测量延迟
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple  # 类型注解

from anthropic import AsyncAnthropic  # Anthropic 官方异步客户端

from app.adapters.model_adapter import ModelAdapter  # 统一接口
from app.adapters.translate import map_stop_reason, map_usage, read_int  # 字段翻译
from app.config import settings  # Key / Base URL
from app.core.errors import ErrorCode, GatewayError, is_retryable_http_status  # 统一错误
from app.schemas.api_request import OutputFormat  # 网关结构化约束
from app.schemas.common import LatencyInfo  # 延迟
from app.schemas.internal import (  # 内部模型
    InternalMessage,
    InternalRequest,
    InternalResponse,
    StreamEvent,
)


class AnthropicMessagesAdapter(ModelAdapter):
    """把网关内部模型翻译成 Anthropic Messages API，再翻回来。"""

    platform_model = "deepseek-v4-flash"  # 路由表用的平台名

    def __init__(
        self,
        api_key: Optional[str] = None,  # 可注入，方便测试
        base_url: Optional[str] = None,  # 可注入上游地址
    ) -> None:
        key = api_key if api_key is not None else settings.deepseek_flash_api_key  # 默认配置
        url = base_url if base_url is not None else settings.deepseek_flash_base_url  # 默认配置
        # max_retries=0：关闭 SDK 隐式重试
        self._client = AsyncAnthropic(api_key=key or "missing-key", base_url=url, max_retries=0)

    def _split_system_and_messages(
        self,
        messages: List[InternalMessage],
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """
        Anthropic 要求 system 单独传，messages 里通常只有 user/assistant。
        多条 system 会拼成一段文本。
        """
        system_parts: List[str] = []  # 收集 system 文本
        body: List[Dict[str, Any]] = []  # Anthropic messages 数组
        for msg in messages:  # 逐条处理
            if msg.role == "system":  # system 不进 messages
                system_parts.append(msg.content)  # 追加
                continue
            # Anthropic content 可以是字符串，也可以是 content block 列表；此处用字符串简化
            body.append({"role": msg.role, "content": msg.content})
        system_text = "\n\n".join(system_parts) if system_parts else None  # 拼 system
        return system_text, body  # 返回 (system, messages)

    def _apply_output_format(
        self,
        kwargs: Dict[str, Any],
        output_format: Optional[OutputFormat],
    ) -> None:
        """
        把网关 output_format 尽量映射到 Anthropic 侧参数。
        兼容端点若支持 output_config / extra_body，则写入；否则退化为 system 追加约束说明。
        """
        if output_format is None:  # 无结构化要求
            return
        if output_format.type == "json_object":  # 弱约束：只要 JSON
            # 通过 extra_body 传递，避免硬绑某一版 SDK 参数名
            kwargs.setdefault("extra_body", {})["output_format"] = {"type": "json_object"}
            return
        if output_format.type == "json_schema" and output_format.json_schema is not None:
            schema_def = output_format.json_schema  # SchemaDefinition
            kwargs.setdefault("extra_body", {})["output_format"] = {
                "type": "json_schema",  # 厂商侧命名
                "schema": schema_def.schema,  # JSON Schema
                "name": schema_def.name,  # 名称
                "strict": schema_def.strict,  # 严格模式
            }
            return

    def _extract_text(self, message: Any) -> str:
        """从 Anthropic Message.content（list of blocks）提取文本。"""
        parts: List[str] = []  # 文本片段
        for block in getattr(message, "content", None) or []:  # 遍历 content blocks
            # 文本 block 通常 type == "text"，字段 text
            if getattr(block, "type", None) == "text":
                value = getattr(block, "text", None)
                if value:
                    parts.append(str(value))
            elif isinstance(block, dict) and block.get("type") == "text":  # dict 形态兜底
                value = block.get("text")
                if value:
                    parts.append(str(value))
        return "".join(parts)  # 拼接

    def _usage_from_message(self, message: Any) -> Any:
        """Anthropic usage 已是 input_tokens / output_tokens，映射到网关 UsageInfo。"""
        usage = getattr(message, "usage", None)  # 上游 usage
        if usage is None:  # 没给用量
            return map_usage()  # 全 0
        return map_usage(
            input_tokens=read_int(usage, "input_tokens"),  # Anthropic 输入字段
            output_tokens=read_int(usage, "output_tokens"),  # Anthropic 输出字段
            cached_tokens=read_int(usage, "cache_read_input_tokens", "cache_creation_input_tokens"),  # 缓存
        )

    def _wrap_upstream_error(self, exc: Exception) -> GatewayError:
        """把 Anthropic SDK 异常包装成 GatewayError。"""
        status_code = getattr(exc, "status_code", None)  # HTTP 状态
        if status_code is None and getattr(exc, "response", None) is not None:
            status_code = getattr(exc.response, "status_code", None)
        name = type(exc).__name__.lower()
        is_timeout = "timeout" in name
        code = ErrorCode.UPSTREAM_TIMEOUT if is_timeout else ErrorCode.UPSTREAM_ERROR
        http_status = 504 if is_timeout else 502
        return GatewayError(
            code=code,
            message=f"Anthropic Messages upstream error: {exc}",
            http_status=http_status,
            retryable=is_retryable_http_status(status_code) or is_timeout,
        )

    async def invoke(self, request: InternalRequest) -> InternalResponse:
        """非流式：调用 messages.create。"""
        started = time.perf_counter()  # 计时起点
        system, body = self._split_system_and_messages(request.messages)  # 拆 system
        kwargs: Dict[str, Any] = {
            "model": request.upstream_model,  # 上游模型 ID
            "messages": body,  # Anthropic messages
            "max_tokens": request.max_tokens,  # Anthropic 必填 max_tokens
            "temperature": request.temperature,  # 温度
        }
        if system:  # 有 system 才传
            kwargs["system"] = system
        self._apply_output_format(kwargs, request.output_format)  # 结构化输出翻译
        try:
            message = await self._client.messages.create(**kwargs)  # 打上游
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_upstream_error(exc) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000.0  # 总耗时
        content = self._extract_text(message)  # 提取文本
        usage = self._usage_from_message(message)  # 翻译用量
        raw_stop = getattr(message, "stop_reason", None)  # Anthropic 叫 stop_reason
        return InternalResponse(
            content=content,
            usage=usage,
            stop_reason=map_stop_reason(raw_stop),  # 归一化
            latency=LatencyInfo(ttft_ms=None, generation_ms=None, total_ms=elapsed_ms),
        )

    async def stream(self, request: InternalRequest) -> AsyncIterator[StreamEvent]:
        """流式：messages.stream / stream=True，产出 StreamEvent。"""
        system, body = self._split_system_and_messages(request.messages)  # 拆消息
        kwargs: Dict[str, Any] = {
            "model": request.upstream_model,
            "messages": body,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if system:
            kwargs["system"] = system
        self._apply_output_format(kwargs, request.output_format)
        final_usage = map_usage()  # 最终用量
        stop_reason = "stop"  # 默认结束原因
        try:
            # Anthropic 推荐 stream 上下文管理器；兼容端点也通常支持
            async with self._client.messages.stream(**kwargs) as stream:  # 打开流
                async for event in stream:  # 厂商事件
                    event_type = getattr(event, "type", "") or ""  # 事件类型
                    # 文本增量：content_block_delta + delta.type == text_delta
                    if event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        delta_type = getattr(delta, "type", None) if delta is not None else None
                        if delta_type == "text_delta":
                            text = getattr(delta, "text", None)  # 增量文本
                            if text:
                                yield StreamEvent(type="text_delta", content=str(text))
                    # message_delta 可能带 stop_reason / usage
                    if event_type == "message_delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None and getattr(delta, "stop_reason", None):
                            stop_reason = map_stop_reason(str(delta.stop_reason))
                        usage_obj = getattr(event, "usage", None)  # 部分实现挂在 event.usage
                        if usage_obj is not None:
                            final_usage = map_usage(
                                input_tokens=read_int(usage_obj, "input_tokens"),
                                output_tokens=read_int(usage_obj, "output_tokens"),
                            )
                    if event_type == "message_stop":  # 流结束标记
                        # 尝试从 get_final_message 补全用量（若 SDK 支持）
                        pass
                # 流结束后尽量取最终 message
                try:
                    final_message = await stream.get_final_message()  # SDK 便捷方法
                    final_usage = self._usage_from_message(final_message)
                    raw_stop = getattr(final_message, "stop_reason", None)
                    if raw_stop:
                        stop_reason = map_stop_reason(str(raw_stop))
                except Exception:  # noqa: BLE001 — 兼容端点可能没有该方法
                    pass
            yield StreamEvent(type="usage", usage=final_usage)  # 用量事件
            yield StreamEvent(type="done", stop_reason=stop_reason)  # 终态事件
        except Exception as exc:  # noqa: BLE001
            wrapped = self._wrap_upstream_error(exc)
            yield StreamEvent(
                type="error",
                error_code=wrapped.code,
                error_message=wrapped.message,
            )
