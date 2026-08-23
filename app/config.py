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

    # DeepSeek Pro 的 API Key，对应 OpenAI Responses API
    deepseek_pro_api_key: str = ""
    # DeepSeek Pro 的上游 Base URL
    deepseek_pro_base_url: str = "https://api.deepseek.com"
    # Pro 在供应商侧的真实模型名（平台别名 deepseek-v4-pro 会映射到这里）
    upstream_model_pro: str = "deepseek-chat"

    # DeepSeek Flash 的 API Key，对应 Anthropic Messages API
    deepseek_flash_api_key: str = ""
    # DeepSeek Flash 的 Anthropic 兼容地址
    deepseek_flash_base_url: str = "https://api.deepseek.com/anthropic"
    # Flash 在供应商侧的真实模型名
    upstream_model_flash: str = "deepseek-chat"

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
