# 模型路由器：平台逻辑模型名 → Adapter 实例 + 上游真实模型 ID
from dataclasses import dataclass  # 路由结果打包

from app.adapters.anthropic_messages_adapter import AnthropicMessagesAdapter  # Flash Adapter
from app.adapters.model_adapter import ModelAdapter  # 统一接口类型
from app.adapters.openai_responses_adapter import OpenAIResponsesAdapter  # Pro Adapter
from app.config import settings  # 读取 upstream_model_* 配置
from app.core.errors import ErrorCode, GatewayError  # 未知模型错误


@dataclass
class RouteResult:
    """一次路由决策的结果。"""

    adapter: ModelAdapter  # 选中的适配器
    platform_model: str  # 平台逻辑名（回显给客户端）
    upstream_model: str  # 实际上游模型 ID（只给 Adapter）


class ModelRouter:
    """
    根据 InvokeRequest.model 选择 Adapter。
    白名单之外的模型一律 unknown_model，不静默透传上游。
    """

    def __init__(self) -> None:
        # 启动时创建两个 Adapter；密钥留在 Adapter / config，不进业务请求
        self._adapters: dict[str, ModelAdapter] = {
            "deepseek-v4-pro": OpenAIResponsesAdapter(),  # Responses 协议
            "deepseek-v4-flash": AnthropicMessagesAdapter(),  # Messages 协议
        }
        # 平台名 → 上游真实模型名（来自配置，可热改配置文件后重启生效）
        self._upstream_names: dict[str, str] = {
            "deepseek-v4-pro": settings.upstream_model_pro,  # Pro 上游 ID
            "deepseek-v4-flash": settings.upstream_model_flash,  # Flash 上游 ID
        }

    def resolve(self, platform_model: str) -> RouteResult:
        """解析平台模型名；未知则抛 GatewayError(unknown_model)。"""
        adapter = self._adapters.get(platform_model)  # 查 Adapter
        if adapter is None:  # 不在白名单
            raise GatewayError(
                code=ErrorCode.UNKNOWN_MODEL,  # 稳定错误码
                message=f"unknown model: {platform_model}",  # 说明
                http_status=400,  # 客户端参数错误
                retryable=False,  # 换模型名才能好
            )
        upstream = self._upstream_names[platform_model]  # 一定存在（与 adapters 同步）
        return RouteResult(
            adapter=adapter,  # 适配器
            platform_model=platform_model,  # 平台名
            upstream_model=upstream,  # 上游 ID
        )


# 进程内默认路由器
model_router = ModelRouter()
