# 本包按协议边界拆分：对外=api_*，对内=internal，跨层=common/errors/trace
# 原则：客户端只认 InvokeRequest/InvokeResponse/StreamChunk；厂商字段只在 Adapter 翻译

from app.schemas.api_request import (  # 对外入站
    InvokeRequest,  # POST /v1/invoke 请求体
    Message,  # 对话消息
    OutputFormat,  # 结构化输出约束
    PromptTemplateRef,  # Prompt 模板引用
    SchemaDefinition,  # JSON Schema 定义
)
from app.schemas.api_response import (  # 对外出站
    InvokeResponse,  # 非流式完整响应
    StreamChunk,  # SSE 流式单帧
)
from app.schemas.common import LatencyInfo, UsageInfo  # Token 与延迟
from app.schemas.errors import ErrorDetail, ErrorResponse  # 统一错误
from app.schemas.internal import (  # 内部领域（Adapter 边界）
    InternalMessage,
    InternalRequest,
    InternalResponse,
    StreamEvent,
)
from app.schemas.trace import TraceRecord  # 可观测记录

__all__ = [
    # 对外 API
    "Message",
    "PromptTemplateRef",
    "SchemaDefinition",
    "OutputFormat",
    "InvokeRequest",
    "InvokeResponse",
    "StreamChunk",
    # 共用
    "UsageInfo",
    "LatencyInfo",
    # 内部
    "InternalMessage",
    "InternalRequest",
    "InternalResponse",
    "StreamEvent",
    # 错误与观测
    "ErrorDetail",
    "ErrorResponse",
    "TraceRecord",
]
