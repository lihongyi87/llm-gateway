# 覆盖：按模型独立限流，超限返回 rate_limit_exceeded / 429
import pytest  # pytest

from app.core.errors import ErrorCode, GatewayError  # 错误断言
from app.services.rate_limiter import PerModelRateLimiter  # 被测限流器


def test_per_model_rate_limit_independent(limiter: PerModelRateLimiter):
    """Pro 与 Flash 配额独立；Pro 超限不影响 Flash。"""
    limiter.acquire("deepseek-v4-pro")  # Pro 第 1 次
    limiter.acquire("deepseek-v4-pro")  # Pro 第 2 次（上限 2）
    with pytest.raises(GatewayError) as ei:  # Pro 第 3 次应失败
        limiter.acquire("deepseek-v4-pro")
    assert ei.value.code == ErrorCode.RATE_LIMIT_EXCEEDED  # 错误码
    assert ei.value.http_status == 429  # HTTP 429
    limiter.acquire("deepseek-v4-flash")  # Flash 仍可用


def test_unknown_model_not_limited(limiter: PerModelRateLimiter):
    """未知模型名不由限流器拦截（交给路由层）。"""
    limiter.acquire("some-unknown-model")  # 应静默通过
