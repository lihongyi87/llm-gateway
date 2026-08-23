# 内部领域模型：编排层与 Adapter 的交界；SDK 类型不得越过此边界
from typing import List, Literal, Optional  # 内部类型注解

from pydantic import BaseModel  # 内部对象同样可校验、可序列化

from app.schemas.api_request import OutputFormat  # 结构化约束从入站拷贝，语义不变
from app.schemas.common import LatencyInfo, UsageInfo  # 用量与延迟


class InternalMessage(BaseModel):
    """内部消息：已完成模板渲染，字段名不跟随任何厂商 SDK。"""

    role: str  # 角色字符串（入站已校验，内部用 str 更灵活）
    content: str  # 最终送入模型的文本


class InternalRequest(BaseModel):
    """编排完成后交给 Adapter 的统一请求。Adapter 在此翻译成 OpenAI / Anthropic 等协议。"""

    model: str  # 平台逻辑模型名（限流键、Trace 键）
    upstream_model: str  # 上游真实模型 ID，仅 Adapter 发出 HTTP 时使用
    messages: List[InternalMessage]  # 已渲染消息列表
    stream: bool = False  # True 走 stream()，False 走 invoke()
    temperature: float = 0.7  # 采样温度
    max_tokens: int = 1024  # 输出上限
    output_format: Optional[OutputFormat] = None  # 结构化输出；Adapter 翻译成 response_format / output_config 等
    trace_id: str  # 全链路追踪 ID
    prompt_name: Optional[str] = None  # 实际使用的模板名
    prompt_version: Optional[str] = None  # 实际使用的模板版本


class InternalResponse(BaseModel):
    """Adapter 归一化后的非流式结果。上游的 finish_reason 等在此统一为 stop_reason。"""

    content: str  # 助手输出（文本或 JSON 字符串）
    usage: UsageInfo  # 已翻译为 input_tokens / output_tokens
    stop_reason: str = "stop"  # 结束原因（从上游 finish_reason / stop_reason 统一而来）
    latency: LatencyInfo  # 本段调用的延迟指标


class StreamEvent(BaseModel):
    """Adapter 内部流事件。Gateway 再编码为 StreamChunk，不直接暴露厂商 chunk。"""

    type: Literal["text_delta", "usage", "done", "error"]  # 事件类型
    content: Optional[str] = None  # text_delta 时的文本片段
    usage: Optional[UsageInfo] = None  # usage 事件
    stop_reason: Optional[str] = None  # done 时的结束原因
    error_code: Optional[str] = None  # error 时的稳定错误码
    error_message: Optional[str] = None  # error 说明（不含密钥与堆栈）
