# 本文件集中读取环境变量，业务代码不硬编码 API Key
from pydantic_settings import BaseSettings, SettingsConfigDict  # 从环境变量自动填充配置类


class Settings(BaseSettings):
    """网关全局配置：密钥、上游地址、限流、端口都从这里读。"""

    # pydantic v2 配置：指定 .env 路径和编码，忽略未声明的多余变量
    model_config = SettingsConfigDict(
        env_file=".env",            # 启动时尝试加载项目根目录的 .env
        env_file_encoding="utf-8",  # .env 按 UTF-8 解析，避免中文乱码
        extra="ignore",             # 环境里多出来的变量不报错
    )

    # Pro 槽 API Key。环境变量名是历史平台名，实际协议见 ModelRouter（Chat Completions）
    deepseek_pro_api_key: str = ""
    # Pro 槽上游 Base URL（联调智谱时常为 open.bigmodel.cn/api/paas/v4）
    deepseek_pro_base_url: str = "https://api.deepseek.com"
    # Pro 槽实际上游模型 ID（默认 glm-4.6，不是 DeepSeek 官网模型）
    upstream_model_pro: str = "glm-4.6"

    # Pro 槽协议开关：openai_responses | openai_chat_completions。
    # 作业要求 Responses 协议；现网 bigmodel 端点无 /responses(404 实测)，
    # 默认走 chat_completions，配了支持 /responses 的上游(如 api.deepseek.com)
    # 时切 openai_responses 即启用——协议切换零改码（Adapter 层已就绪）
    pro_protocol: str = "openai_chat_completions"
    # Responses 槽独立上游（切协议时用）：默认同 pro 槽
    responses_base_url: str = ""
    responses_api_key: str = ""

    # Flash 槽 API Key。实际协议是 Anthropic Messages（联调 MiniMax）
    deepseek_flash_api_key: str = ""
    # Flash 槽 Anthropic 兼容地址（MiniMax 国内常用 api.minimaxi.com/anthropic）
    deepseek_flash_base_url: str = "https://api.minimaxi.com/anthropic"
    # Flash 槽实际上游模型 ID（默认 MiniMax-M3）
    upstream_model_flash: str = "MiniMax-M3"

    # Pro 模型每分钟请求上限，超限返回 429
    rate_limit_pro: int = 60
    # Flash 模型每分钟请求上限，与 Pro 分开计数
    rate_limit_flash: int = 120

    # 网关监听端口
    port: int = 8000
    # 日志级别
    log_level: str = "INFO"
    # 最多尝试次数（含第一次），作业要求最多 3 次
    max_retry_attempts: int = 3
    # Prompt 模板文件所在目录
    prompts_dir: str = "app/prompts"


# 进程内单例：其它模块写 from app.config import settings 即可使用
settings = Settings()
