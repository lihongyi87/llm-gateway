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
    """HTTP /v1/health 应返回 ok。"""
    client = TestClient(app)  # 同步测试客户端
    resp = client.get("/v1/health")  # 发请求
    assert resp.status_code == 200  # 成功
    assert resp.json()["status"] == "ok"  # 正文
