# adapters 包：厂商协议翻译层；对外只导出基类与两个具体 Adapter
from app.adapters.anthropic_messages import AnthropicMessagesAdapter  # Flash → Anthropic Messages
from app.adapters.base import ModelAdapter  # 统一接口 invoke/stream
from app.adapters.openai_responses import OpenAIResponsesAdapter  # Pro → OpenAI Responses

__all__ = [
    "ModelAdapter",  # 抽象基类
    "OpenAIResponsesAdapter",  # deepseek-v4-pro
    "AnthropicMessagesAdapter",  # deepseek-v4-flash
]
