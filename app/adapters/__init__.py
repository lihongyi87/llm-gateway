# adapters 包：厂商协议翻译层；文件名与类名一一对应（snake_case ↔ PascalCase）
from app.adapters.anthropic_messages_adapter import AnthropicMessagesAdapter  # Flash → Messages
from app.adapters.model_adapter import ModelAdapter  # 统一接口 invoke/stream
from app.adapters.openai_responses_adapter import OpenAIResponsesAdapter  # Pro → Responses

__all__ = [
    "ModelAdapter",  # model_adapter.py
    "OpenAIResponsesAdapter",  # openai_responses_adapter.py
    "AnthropicMessagesAdapter",  # anthropic_messages_adapter.py
]
