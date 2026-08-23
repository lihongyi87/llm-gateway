# 对外 HTTP 入站契约：网关自有协议，字段名不与 OpenAI / Anthropic 绑定
# 厂商差异（prompt_tokens、choices、content blocks 等）只在 Adapter 内翻译
from typing import Any, Dict, List, Literal, Optional  # 请求体需要的类型注解

from pydantic import BaseModel, Field  # JSON 校验与安全的默认值


class Message(BaseModel):
    """一条对话消息。role/content 是通用对话概念，不属于某一家厂商。"""

    role: Literal["system", "user", "assistant", "tool"]  # 角色白名单
    content: str  # 纯文本正文（多模态以后用独立类型扩展）


class PromptTemplateRef(BaseModel):
    """引用网关内已版本化的 Prompt 模板。"""

    name: str  # 模板名，例如 summarize
    version: str  # 语义化版本，例如 1.0.0
    variables: Dict[str, str] = Field(default_factory=dict)  # 渲染变量；缺失由 Prompt 层拒绝


class SchemaDefinition(BaseModel):
    """结构化输出所用的 JSON Schema 定义。"""

    name: str  # Schema 名称，便于日志与上游识别
    schema: Dict[str, Any]  # JSON Schema 对象
    strict: bool = True  # 严格模式：不允许 Schema 未声明的字段


class OutputFormat(BaseModel):
    """约束模型输出为 JSON。网关对外统一叫 output_format，Adapter 再翻译成各厂商字段。"""

    type: Literal["json_schema", "json_object"]  # json_object=仅保证合法 JSON；json_schema=字段级约束
    json_schema: Optional[SchemaDefinition] = None  # type=json_schema 时由编排层校验必填


class InvokeRequest(BaseModel):
    """POST /v1/invoke 的入站请求体。这是网关对外唯一调用契约。"""

    model: str  # 平台逻辑模型名，例如 deepseek-v4-pro（不是上游真实 ID）
    messages: List[Message] = Field(default_factory=list)  # 对话消息；可与 prompt_template 组合使用
    stream: bool = False  # True=SSE 流式；False=一次返回完整 JSON
    temperature: Optional[float] = 0.7  # 采样温度；上游不支持时由 Adapter 忽略
    max_tokens: Optional[int] = 1024  # 输出 token 上限
    output_format: Optional[OutputFormat] = None  # 结构化输出约束；空=自由文本
    prompt_template: Optional[PromptTemplateRef] = None  # 模板引用；由网关渲染后再调模型
