# 覆盖：可观测性 — Trace 含 Token 分类与延迟；可按 id 查询
import pytest  # pytest

from app.schemas.api_request import InvokeRequest, Message  # 入站
from app.services.gateway_service import GatewayService  # 编排
from app.services.model_router import ModelRouter  # 路由
from tests.fakes import FakeAdapter  # 假 Adapter


@pytest.mark.asyncio
async def test_trace_records_usage_and_latency(prompt_service, limiter, traces):
    """invoke 成功后 Trace 应有 input/output tokens 与 total_ms。"""
    fake = FakeAdapter(content="ok")  # 假 Adapter
    router = ModelRouter()
    router._adapters = {"deepseek-v4-pro": fake}
    router._upstream_names = {"deepseek-v4-pro": "fake-pro"}
    gw = GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)

    resp = await gw.invoke(
        InvokeRequest(
            model="deepseek-v4-pro",
            messages=[Message(role="user", content="hi")],
        )
    )
    record = gw.get_trace(resp.trace_id)  # 按 id 查询
    assert record.status == "ok"  # 成功
    assert record.usage.input_tokens == 3  # 分类：输入
    assert record.usage.output_tokens == 2  # 分类：输出
    assert record.usage.total_tokens == 5  # 合计
    assert record.latency.total_ms >= 0  # 有总延迟
    assert record.resolved_upstream_model == "fake-pro"  # 记录实际上游模型


@pytest.mark.asyncio
async def test_trace_records_prompt_and_retry(prompt_service, limiter, traces, monkeypatch):
    """使用模板且上游短暂失败重试后，Trace 应有 prompt_* 与 retry_count。"""
    monkeypatch.setattr("app.services.retry.asyncio.sleep", _noop)  # 跳过 sleep
    monkeypatch.setattr("app.services.retry.compute_backoff_seconds", lambda _i: 0)
    fake = FakeAdapter(fail_times=2, content="recovered")  # 失败两次再成功
    router = ModelRouter()
    router._adapters = {"deepseek-v4-pro": fake}
    router._upstream_names = {"deepseek-v4-pro": "fake-pro"}
    gw = GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)

    resp = await gw.invoke(
        InvokeRequest(
            model="deepseek-v4-pro",
            messages=[],
            prompt_template={
                "name": "summarize",
                "version": "1.0.0",
                "variables": {"text": "文章", "max_words": "10"},
            },
        )
    )
    record = gw.get_trace(resp.trace_id)
    assert record.retry_count == 2  # 重试两次
    assert record.prompt_name == "summarize"  # 模板名
    assert record.prompt_version == "1.0.0"  # 版本
    assert record.prompt_hash  # 有 hash
    assert fake.invoke_calls == 3  # 共调用 3 次


async def _noop(_s: float) -> None:
    """空等待。"""
    return None
