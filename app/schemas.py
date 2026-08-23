# 本文件定义网关对外协议和内部领域模型，Adapter 只吃内部模型
from typing import Any, Dict, List, Literal, Optional  # 类型注解：任意对象、字典、列表、字面量、可选

from pydantic import BaseModel, Field  # BaseModel 做 JSON 校验；Field 给字段默认值


class ChatMessage(BaseModel):
    """一条对话消息，对齐 OpenAI Chat Completions 的 message 形状。"""

    role: Literal["system", "user", "assistant", "tool"]  # 角色只允许这四种
    content: str  # 本作业简化为纯文本正文


class PromptTemplateRef(BaseModel):
    """请求里引用已存储的 Prompt 模板：名称 + 版本 + 变量。"""

    name: str  # 模板名，例如 summarize
    version: str  # 语义化版本，例如 1.0.0
    variables: Dict[str, str] = Field(default_factory=dict)  # 渲染时填入模板的键值对


class JsonSchemaFormat(BaseModel):
    """结构化输出时，json_schema 这一层的定义。"""

    name: str  # Schema 名称，给上游识别用
    schema: Dict[str, Any]  # JSON Schema 对象本身
    strict: bool = True  # 若上游支持严格模式则打开


class ResponseFormat(BaseModel):
    """response_format 字段：约束模型返回合法 JSON。"""

    type: Literal["json_schema", "json_object"]  # 两种约束级别
    json_schema: Optional[JsonSchemaFormat] = None  # type 为 json_schema 时填写


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions 的请求体，客户端只认这一套字段。"""

    model: str  # 平台模型名：deepseek-v4-pro 或 deepseek-v4-flash
    messages: List[ChatMessage] = Field(default_factory=list)  # 直接传消息时使用
    stream: bool = False  # True 时按 SSE 逐块返回
    temperature: Optional[float] = 0.7  # 采样温度
    max_tokens: Optional[int] = 1024  # 最大输出 token
    response_format: Optional[ResponseFormat] = None  # 结构化输出约束，可空
    prompt_template: Optional[PromptTemplateRef] = None  # 若提供则渲染模板再调模型


class UsageInfo(BaseModel):
    """一次调用的 Token 分类统计。"""

    prompt_tokens: int = 0  # 输入 token
    completion_tokens: int = 0  # 输出 token
    total_tokens: int = 0  # 合计
    cached_tokens: int = 0  # 缓存命中 token（上游有则填）
    reasoning_tokens: int = 0  # 推理 token（上游有则填）


class LatencyInfo(BaseModel):
    """延迟指标，单位毫秒。"""

    ttft_ms: Optional[float] = None  # 首个有内容 token 的延迟
    generation_ms: Optional[float] = None  # 首 token 到生成结束
    total_ms: float = 0.0  # 端到端总耗时


class ChatCompletionChoice(BaseModel):
    """非流式响应里的一条候选回复。"""

    index: int = 0  # 候选下标，通常为 0
    message: ChatMessage  # 助手消息
    finish_reason: Optional[str] = "stop"  # 结束原因：stop / length 等


class ChatCompletionResponse(BaseModel):
    """非流式完整响应，形状接近 OpenAI Chat Completions。"""

    id: str  # 本次响应 ID，可与 trace_id 相同
    object: str = "chat.completion"  # 固定对象类型
    model: str  # 请求里的平台模型名
    choices: List[ChatCompletionChoice]  # 候选列表
    usage: UsageInfo  # Token 统计
    trace_id: Optional[str] = None  # 网关生成的追踪 ID，方便查观测数据


class StreamDelta(BaseModel):
    """流式 chunk 里的增量字段。"""

    role: Optional[str] = None  # 第一包可能带 role
    content: Optional[str] = None  # 文本增量，可能为空


class StreamChoice(BaseModel):
    """流式响应里的一条 choice。"""

    index: int = 0  # 候选下标
    delta: StreamDelta  # 本 chunk 的增量
    finish_reason: Optional[str] = None  # 最后一包才可能有结束原因


class ChatCompletionChunk(BaseModel):
    """SSE 每一行 data: 后面的 JSON 结构。"""

    id: str  # 与整次调用同一 ID
    object: str = "chat.completion.chunk"  # 固定为 chunk 类型
    model: str  # 平台模型名
    choices: List[StreamChoice]  # 增量候选
    usage: Optional[UsageInfo] = None  # 部分实现会在最后一包带 usage


class InternalMessage(BaseModel):
    """内部消息，不绑定任何供应商字段名。"""

    role: str  # 角色字符串
    content: str  # 文本内容


class InternalRequest(BaseModel):
    """编排完成后交给 Adapter 的统一请求。"""

    model: str  # 平台模型名，用于路由
    upstream_model: str  # 解析后的上游真实模型名
    messages: List[InternalMessage]  # 已渲染好的消息列表
    stream: bool = False  # 是否流式
    temperature: float = 0.7  # 采样温度
    max_tokens: int = 1024  # 最大输出 token
    response_format: Optional[ResponseFormat] = None  # 结构化输出，可空
    trace_id: str  # 全链路追踪 ID
    prompt_name: Optional[str] = None  # 实际使用的模板名
    prompt_version: Optional[str] = None  # 实际使用的模板版本


class InternalResponse(BaseModel):
    """Adapter 归一化后的非流式结果，Gateway 其它层只认这个。"""

    content: str  # 助手文本或 JSON 字符串
    usage: UsageInfo  # Token 统计
    finish_reason: str = "stop"  # 结束原因
    latency: LatencyInfo  # 本段调用测到的延迟


class StreamEvent(BaseModel):
    """Adapter 产出的内部流事件，禁止把供应商原始 chunk 往外传。"""

    type: Literal["text_delta", "usage", "done", "error"]  # 事件类型
    content: Optional[str] = None  # text_delta 时的文本片段
    usage: Optional[UsageInfo] = None  # usage 事件时的用量
    finish_reason: Optional[str] = None  # done 时的结束原因
    error_code: Optional[str] = None  # error 时的稳定错误码
    error_message: Optional[str] = None  # error 时的说明


class ErrorDetail(BaseModel):
    """统一错误详情，客户端只看 code / message / retryable。"""

    code: str  # 稳定错误码，例如 rate_limit_exceeded
    message: str  # 人类可读说明
    retryable: bool = False  # 是否建议客户端重试


class ErrorResponse(BaseModel):
    """HTTP 错误响应外壳。"""

    error: ErrorDetail  # 错误详情对象


class TraceRecord(BaseModel):
    """一次调用的可观测记录，供 GET /v1/traces/{id} 查询。"""

    trace_id: str  # 追踪 ID
    model: str  # 请求的平台模型名
    resolved_upstream_model: str  # 实际打到上游的模型名
    prompt_name: Optional[str] = None  # Prompt 模板名
    prompt_version: Optional[str] = None  # Prompt 版本
    prompt_hash: Optional[str] = None  # 渲染后模板哈希，便于回放
    usage: UsageInfo  # Token 分类统计
    latency: LatencyInfo  # 延迟（含 TTFT）
    retry_count: int = 0  # 实际重试次数
    status: Literal["ok", "error"] = "ok"  # 调用终态
    error_code: Optional[str] = None  # 失败时的错误码
    finish_reason: Optional[str] = None  # 模型结束原因
    created_at: str  # ISO8601 创建时间
