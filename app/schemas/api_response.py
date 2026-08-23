# 对外 HTTP 出站契约：扁平、厂商无关；不含 choices / object / delta 等 OpenAI 专用结构
from typing import Optional  # 可选字段

from pydantic import BaseModel  # 出站 JSON 校验

from app.schemas.api_request import Message  # 非流式完整回复复用 Message
from app.schemas.common import UsageInfo  # Token 统计（input_tokens / output_tokens）


class InvokeResponse(BaseModel):
    """POST /v1/invoke 非流式响应。一次调用一条 message，不搞 choices 数组。"""

    id: str  # 本次调用 ID，通常与 trace_id 相同
    model: str  # 回显平台逻辑模型名（不是上游真实 ID）
    message: Message  # 助手完整回复
    stop_reason: Optional[str] = "stop"  # 结束原因：stop / length / content_filter 等（网关统一命名）
    usage: UsageInfo  # Token 分类统计
    trace_id: Optional[str] = None  # 观测查询 ID，对应 GET /v1/traces/{trace_id}


class StreamChunk(BaseModel):
    """SSE 每一帧 data: 后的 JSON。扁平结构，Adapter 负责从厂商 chunk 翻译过来。"""

    id: str  # 整次调用同一 ID
    model: str  # 平台逻辑模型名
    text: str = ""  # 本帧新增文本；无新增内容时为空字符串
    done: bool = False  # False=中间帧；True=最后一帧（可附带 stop_reason / usage）
    stop_reason: Optional[str] = None  # 仅 done=true 时通常有值
    usage: Optional[UsageInfo] = None  # 可选：最后一帧附带 Token 统计
    error_code: Optional[str] = None  # 流中途失败时的稳定错误码；成功帧为 None
    error_message: Optional[str] = None  # 流中途失败时的说明；禁止含 API Key
