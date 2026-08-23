# 按模型独立限流：每个平台 model 各自计数，超限返回 429
import time  # 用单调时钟记录请求时间戳
from collections import defaultdict, deque  # 每个模型一个滑动窗口队列

from app.config import settings  # 读取各模型 RPM 配置
from app.core.errors import ErrorCode, GatewayError  # 超限时抛统一错误


class PerModelRateLimiter:
    """
    滑动窗口限流器：统计最近 60 秒内每个 model 的请求次数。
    超过 RPM（requests per minute）则拒绝并返回 rate_limit_exceeded。
    """

    def __init__(self) -> None:
        # 每个 model 对应一个 deque，存最近请求的时间戳（秒）
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        # 每个 model 的 RPM 上限；未配置则用默认值 60
        self._limits: dict[str, int] = {
            "deepseek-v4-pro": settings.rate_limit_pro,  # Pro 模型限额
            "deepseek-v4-flash": settings.rate_limit_flash,  # Flash 模型限额
        }
        self._window_seconds = 60.0  # 滑动窗口宽度：1 分钟

    def _cleanup_window(self, model: str, now: float) -> None:
        """丢掉窗口外的时间戳，只保留最近 60 秒内的请求记录。"""
        window = self._windows[model]  # 取出该模型的队列
        cutoff = now - self._window_seconds  # 早于这个时间的记录应删除
        while window and window[0] <= cutoff:  # 队首过期则弹出
            window.popleft()

    def acquire(self, model: str) -> None:
        """
        尝试占用一次配额。成功则静默返回；失败则抛 GatewayError(429)。
        应在真正调用上游之前调用（先限流，再请求）。
        """
        now = time.monotonic()  # 单调时钟，不受系统时间回拨影响
        limit = self._limits.get(model)  # 查该模型的 RPM 上限
        if limit is None:  # 未知模型：交给路由层处理，限流器不拦
            return

        self._cleanup_window(model, now)  # 清理过期记录
        window = self._windows[model]  # 当前窗口内的请求时间戳

        if len(window) >= limit:  # 最近 1 分钟已达上限
            raise GatewayError(
                code=ErrorCode.RATE_LIMIT_EXCEEDED,  # 稳定错误码
                message=f"model '{model}' rate limit exceeded ({limit} req/min)",  # 说明含限额
                http_status=429,  # HTTP 标准「请求过多」
                retryable=True,  # 客户端可稍后重试
            )

        window.append(now)  # 记录本次请求时间，占用一个配额

    def reset(self) -> None:
        """清空所有计数（主要用于单元测试）。"""
        self._windows.clear()  # 删掉全部模型的窗口数据


# 进程内单例：全网关共享同一套限流状态
rate_limiter = PerModelRateLimiter()
