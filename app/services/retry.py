# 指数退避重试：只在网关这一层重试，SDK 必须设 max_retries=0
import asyncio  # 异步 sleep，用于退避等待
import random  # jitter，避免惊群效应
from typing import Awaitable, Callable, TypeVar  # 泛型与可调用类型

from app.config import settings  # 读取 max_retry_attempts
from app.core.errors import ErrorCode, GatewayError, is_retryable_http_status  # 统一错误

# 泛型 T：表示被重试函数的最终返回类型（任意类型）
T = TypeVar("T")


def compute_backoff_seconds(attempt_index: int) -> float:
    """
    计算第 attempt_index 次重试前的等待秒数（attempt_index 从 0 开始）。
    公式来自课件：delay = min(8, 0.5 * 2^attempt) + uniform(0, 0.2)
    """
    base = 0.5 * (2**attempt_index)  # 指数部分：0.5, 1.0, 2.0, ...
    capped = min(8.0, base)  # 上限 8 秒，防止等太久
    jitter = random.uniform(0.0, 0.2)  # 随机抖动 0~0.2 秒
    return capped + jitter  # 返回总等待时间


async def retry_async(
    operation: Callable[[], Awaitable[T]],  # 无参异步函数，返回 T
    *,
    max_attempts: int | None = None,  # 最多尝试次数；默认读配置
    operation_name: str = "upstream_call",  # 日志/错误里用的操作名
) -> tuple[T, int]:
    """
    对短暂故障做有界重试，返回 (结果, 实际重试次数)。
    重试次数 = 总尝试次数 - 1。
    """
    attempts_limit = max_attempts or settings.max_retry_attempts  # 默认 3 次
    last_error: Exception | None = None  # 记录最后一次异常
    retry_count = 0  # 已发生的重试次数（不含首次）

    for attempt in range(attempts_limit):  # attempt: 0,1,2 共 3 次尝试
        try:
            result = await operation()  # 执行被包装的异步调用
            return result, retry_count  # 成功则立刻返回
        except GatewayError as exc:
            last_error = exc  # 记下网关异常
            # GatewayError 若已标明不可重试，或不是上游类错误，直接抛出
            if not exc.retryable:
                raise
            # 限流类错误也可由网关重试（与上游 429 区分：那是网关自己的 rate_limit）
            if exc.code == ErrorCode.RATE_LIMIT_EXCEEDED:
                raise
        except Exception as exc:  # noqa: BLE001 — 兜底捕获上游 SDK/httpx 异常
            last_error = exc  # 记下原始异常
            status_code = getattr(exc, "status_code", None)  # httpx/openai 常有 status_code
            if not is_retryable_http_status(status_code):  # 不可重试则原样抛出
                raise

        # 走到这里说明本次失败且还可重试
        if attempt < attempts_limit - 1:  # 不是最后一次尝试
            delay = compute_backoff_seconds(attempt)  # 计算退避时间
            retry_count += 1  # 计一次重试
            await asyncio.sleep(delay)  # 异步等待后再试

    # 所有尝试耗尽：包装成统一上游错误抛出
    message = f"{operation_name} failed after {attempts_limit} attempts: {last_error}"
    raise GatewayError(
        code=ErrorCode.UPSTREAM_ERROR,  # 稳定错误码
        message=message,  # 含最后一次错误摘要
        http_status=502,  # 网关作为代理，上游失败常用 502
        retryable=False,  # 已重试耗尽，勿再重试
    ) from last_error  # 保留异常链，便于调试
