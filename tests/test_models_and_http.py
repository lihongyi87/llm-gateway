# 覆盖：两个平台模型均可路由；未知模型报错；HTTP 健康检查
import pytest  # pytest
from fastapi.testclient import TestClient  # FastAPI 测试客户端

from app.core.errors import ErrorCode, GatewayError  # 错误断言
from app.main import app  # FastAPI 应用
from app.schemas.api_request import InvokeRequest, Message  # 入站
from app.services.gateway_service import GatewayService  # 编排
from app.services.model_router import ModelRouter  # 路由
from tests.fakes import FakeAdapter  # 假 Adapter


def test_router_unknown_model():
    """未知模型应 unknown_model。"""
    router = ModelRouter()  # 真实路由白名单
    with pytest.raises(GatewayError) as ei:
        router.resolve("gpt-does-not-exist")
    assert ei.value.code == ErrorCode.UNKNOWN_MODEL


@pytest.mark.asyncio
async def test_both_models_invoke(prompt_service, limiter, traces):
    """Pro 与 Flash 两个平台名都应能走通 invoke。"""
    pro = FakeAdapter(platform_model="deepseek-v4-pro", content="from-pro")
    flash = FakeAdapter(platform_model="deepseek-v4-flash", content="from-flash")
    router = ModelRouter()
    router._adapters = {
        "deepseek-v4-pro": pro,
        "deepseek-v4-flash": flash,
    }
    router._upstream_names = {
        "deepseek-v4-pro": "fake-pro",
        "deepseek-v4-flash": "fake-flash",
    }
    gw = GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)

    r1 = await gw.invoke(
        InvokeRequest(model="deepseek-v4-pro", messages=[Message(role="user", content="a")])
    )
    r2 = await gw.invoke(
        InvokeRequest(model="deepseek-v4-flash", messages=[Message(role="user", content="b")])
    )
    assert r1.message.content == "from-pro"  # Pro 通
    assert r2.message.content == "from-flash"  # Flash 通


def test_health_endpoint():
    """HTTP /v1/health 应返回 ok，并公开实际协议映射。"""
    client = TestClient(app)  # 同步测试客户端
    resp = client.get("/v1/health")  # 发请求
    assert resp.status_code == 200  # 成功
    body = resp.json()  # 解析 JSON
    assert body["status"] == "ok"  # 存活
    models = {item["model"]: item for item in body["models"]}  # 按平台名索引
    from app.config import settings  # 协议由配置驱动（.env 可切 Responses/Chat）
    expected_pro = ("openai_responses"
                    if settings.pro_protocol.strip().lower() == "openai_responses"
                    else "openai_chat_completions")
    assert models["deepseek-v4-pro"]["protocol"] == expected_pro  # Pro 协议=配置
    assert models["deepseek-v4-flash"]["protocol"] == "anthropic_messages"  # Flash 实际协议
    wired = {m["model"] for m in body["models"]}  # 已挂路由的模型集
    assert "glm-responses" in wired  # Responses 槽显式在表（上游就绪即真跑）


def test_http_sse_stream(monkeypatch, prompt_service, limiter, traces):
    """HTTP 层必须真走 SSE：Content-Type + data 行 + [DONE]。"""
    fake = FakeAdapter(platform_model="deepseek-v4-flash", stream_parts=["你", "好"])
    router = ModelRouter()
    router._adapters = {"deepseek-v4-flash": fake}
    router._upstream_names = {"deepseek-v4-flash": "fake-flash"}
    gw = GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)
    monkeypatch.setattr("app.api.routes.gateway_service", gw)  # 注入假网关，不打真实上游
    client = TestClient(app)  # 同步客户端
    with client.stream(
        "POST",
        "/v1/invoke",
        json={
            "model": "deepseek-v4-flash",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ) as resp:
        assert resp.status_code == 200  # SSE 成功开始
        assert "text/event-stream" in resp.headers["content-type"]  # 正确媒体类型
        body = "".join(resp.iter_text())  # 拼完整 SSE 文本
    assert "data:" in body  # 至少有一帧
    assert "[DONE]" in body  # 结束标记
    assert "你" in body  # 第一段
    assert "好" in body  # 第二段


def test_http_sse_mid_stream_error(monkeypatch, prompt_service, limiter, traces):
    """HTTP 流中途失败应仍是 SSE，且 data 里带 error_code。"""
    fake = FakeAdapter(
        platform_model="deepseek-v4-flash",
        stream_parts=["你", "好"],
        stream_error_after=1,
    )
    router = ModelRouter()
    router._adapters = {"deepseek-v4-flash": fake}
    router._upstream_names = {"deepseek-v4-flash": "fake-flash"}
    gw = GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)
    monkeypatch.setattr("app.api.routes.gateway_service", gw)
    client = TestClient(app)
    with client.stream(
        "POST",
        "/v1/invoke",
        json={
            "model": "deepseek-v4-flash",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ) as resp:
        assert resp.status_code == 200  # 已经开始推流，不能改成 502 JSON
        body = "".join(resp.iter_text())
    assert "upstream_error" in body  # 错误码在 SSE 帧里
    assert "[DONE]" in body  # 仍然收尾
