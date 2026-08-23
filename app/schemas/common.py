# 跨层共享的用量与延迟，不属于任何一家供应商协议
from typing import Optional  # 可选字段：上游没给 TTFT 时允许为空

from pydantic import BaseModel  # 用 Pydantic 保证 JSON 与类型一致


class UsageInfo(BaseModel):
    """一次模型调用的 Token 分类统计，供响应体和 Trace 共用。"""

    input_tokens: int = 0  # 输入 token（Adapter 从上游 prompt/input 字段翻译到这里）
    output_tokens: int = 0  # 输出 token（Adapter 从上游 completion/output 字段翻译到这里）
    total_tokens: int = 0  # 合计，通常等于 input_tokens + output_tokens
    cached_tokens: int = 0  # 缓存命中 token，上游未返回则保持 0
    reasoning_tokens: int = 0  # 推理 token，上游未返回则保持 0


class LatencyInfo(BaseModel):
    """一次调用的延迟拆分，单位毫秒。"""

    ttft_ms: Optional[float] = None  # 请求开始到首个有内容 token 的时间
    generation_ms: Optional[float] = None  # 首 token 到生成结束的时间
    total_ms: float = 0.0  # 端到端总耗时，含排队与网络
