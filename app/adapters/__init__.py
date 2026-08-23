# adapters 包：厂商协议翻译层；文件名与类名一一对应
from app.adapters.anthropic_messages_adapter import AnthropicMessagesAdapter  # Messages
from app.adapters.model_adapter import ModelAdapter  # 基类
from app.adapters.openai_chat_completions_adapter import OpenAIChatCompletionsAdapter  # Chat Completions
from app.adapters.openai_responses_adapter import OpenAIResponsesAdapter  # Responses（可选）

__all__ = [
    "ModelAdapter",
    "OpenAIChatCompletionsAdapter",
    "OpenAIResponsesAdapter",
    "AnthropicMessagesAdapter",
]
