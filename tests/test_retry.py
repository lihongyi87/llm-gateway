# 覆盖：指数退避重试；短暂故障可重试，耗尽后 upstream_error
import pytest  # pytest

from app.core.errors import ErrorCode, GatewayError  # 错误断言
from app.services.retry import retry_async  # 被测重试


@pytest.mark.asyncio
async def test_retry_then_success(monkeypatch):
    """前两次失败、第三次成功；retry_count 应为 2；不真实 sleep。"""
    monkeypatch.setattr("app.services.retry.asyncio.sleep", _noop_sleep)  # 跳过等待
    monkeypatch.setattr("app.services.retry.compute_backoff_seconds", lambda _i: 0)  # 退避为 0
    state = {"n": 0}  # 可变计数器

    async def flaky():
        state["n"] += 1  # 每次调用 +1
        if state["n"] < 3:  # 前两次失败
            raise GatewayError(
                code=ErrorCode.UPSTREAM_ERROR,
                message="temp fail",
                http_status=502,
                retryable=True,
            )
        return "ok"  # 第三次成功

    result, retry_count = await retry_async(flaky, max_attempts=3)  # 最多 3 次
    assert result == "ok"  # 最终成功
    assert retry_count == 2  # 重试了 2 次
    assert state["n"] == 3  # 总共调用 3 次


@pytest.mark.asyncio
async def test_retry_exhausted(monkeypatch):
    """一直失败则抛 upstream_error，且 retryable=False。"""
    monkeypatch.setattr("app.services.retry.asyncio.sleep", _noop_sleep)
    monkeypatch.setattr("app.services.retry.compute_backoff_seconds", lambda _i: 0)

    async def always_fail():
        raise GatewayError(
            code=ErrorCode.UPSTREAM_ERROR,
            message="down",
            http_status=502,
            retryable=True,
        )

    with pytest.raises(GatewayError) as ei:
        await retry_async(always_fail, max_attempts=3)
    assert ei.value.code == ErrorCode.UPSTREAM_ERROR  # 耗尽错误码
    assert ei.value.retryable is False  # 不再建议重试


@pytest.mark.asyncio
async def test_rate_limit_not_retried():
    """网关自身限流错误不应进入退避重试。"""

    async def limited():
        raise GatewayError(
            code=ErrorCode.RATE_LIMIT_EXCEEDED,
            message="limited",
            http_status=429,
            retryable=True,
        )

    with pytest.raises(GatewayError) as ei:
        await retry_async(limited, max_attempts=3)
    assert ei.value.code == ErrorCode.RATE_LIMIT_EXCEEDED  # 原样抛出


@pytest.mark.asyncio
async def test_type_error_not_retried(monkeypatch):
    """SDK 签名错误（TypeError）不得当网络抖动重试三遍。"""
    monkeypatch.setattr("app.services.retry.asyncio.sleep", _noop_sleep)
    state = {"n": 0}  # 调用计数

    async def boom():
        state["n"] += 1  # 每次进入 +1
        raise TypeError("messages.create() got an unexpected keyword argument 'temperature'")

    with pytest.raises(TypeError):  # 原样抛出，不包成耗尽错误
        await retry_async(boom, max_attempts=3)
    assert state["n"] == 1  # 只打一次


@pytest.mark.asyncio
async def test_http_400_not_retried(monkeypatch):
    """上游 400 是请求错误，不可重试。"""
    monkeypatch.setattr("app.services.retry.asyncio.sleep", _noop_sleep)
    state = {"n": 0}

    class Http400(Exception):
        """带 status_code 的假 HTTP 错误。"""

        def __init__(self) -> None:
            super().__init__("bad request")  # 文案
            self.status_code = 400  # 客户端错误

    async def bad_request():
        state["n"] += 1
        raise Http400()

    with pytest.raises(Http400):
        await retry_async(bad_request, max_attempts=3)
    assert state["n"] == 1  # 不重试


@pytest.mark.asyncio
async def test_connect_error_is_retried(monkeypatch):
    """名字像连接失败的异常应退避后重试。"""
    monkeypatch.setattr("app.services.retry.asyncio.sleep", _noop_sleep)
    monkeypatch.setattr("app.services.retry.compute_backoff_seconds", lambda _i: 0)
    state = {"n": 0}

    class ConnectError(Exception):
        """模拟 httpx.ConnectError。"""

    async def flaky_net():
        state["n"] += 1
        if state["n"] < 2:  # 第一次失败
            raise ConnectError("connection refused")
        return "ok"  # 第二次成功

    result, retry_count = await retry_async(flaky_net, max_attempts=3)
    assert result == "ok"  # 最终成功
    assert retry_count == 1  # 重试了 1 次
    assert state["n"] == 2  # 共两次


async def _noop_sleep(_seconds: float) -> None:
    """测试用空 sleep，避免真的等待。"""
    return None
