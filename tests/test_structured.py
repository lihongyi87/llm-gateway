# 覆盖：结构化输出本地校验（合法通过 / 非法字段失败）
import pytest  # pytest

from app.core.errors import ErrorCode, GatewayError  # 错误断言
from app.schemas.api_request import (  # 入站模型
    InvokeRequest,
    Message,
    OutputFormat,
    SchemaDefinition,
)
from app.services.gateway_service import GatewayService  # 编排
from app.services.model_router import ModelRouter  # 路由
from tests.fakes import FakeAdapter  # 假 Adapter


def _gateway(content: str, prompt_service, limiter, traces) -> GatewayService:
    """用指定 content 的 FakeAdapter 组装 Gateway。"""
    fake = FakeAdapter(platform_model="deepseek-v4-pro", content=content)  # 固定输出
    router = ModelRouter()
    router._adapters = {"deepseek-v4-pro": fake}
    router._upstream_names = {"deepseek-v4-pro": "fake-pro"}
    return GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)


@pytest.mark.asyncio
async def test_structured_output_ok(prompt_service, limiter, traces):
    """返回符合 schema 的 JSON 应通过。"""
    gw = _gateway('{"sentiment":"positive","confidence":0.9}', prompt_service, limiter, traces)
    req = InvokeRequest(
        model="deepseek-v4-pro",
        messages=[Message(role="user", content="分析情感")],
        output_format=OutputFormat(
            type="json_schema",
            json_schema=SchemaDefinition(
                name="sentiment",
                schema={
                    "type": "object",
                    "properties": {
                        "sentiment": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["sentiment", "confidence"],
                },
            ),
        ),
    )
    resp = await gw.invoke(req)  # 应成功
    assert "positive" in resp.message.content  # 内容保留


@pytest.mark.asyncio
async def test_structured_output_missing_field(prompt_service, limiter, traces):
    """缺必填字段应 schema_validation_failed。"""
    gw = _gateway('{"sentiment":"positive"}', prompt_service, limiter, traces)  # 缺 confidence
    req = InvokeRequest(
        model="deepseek-v4-pro",
        messages=[Message(role="user", content="分析情感")],
        output_format=OutputFormat(
            type="json_schema",
            json_schema=SchemaDefinition(
                name="sentiment",
                schema={
                    "type": "object",
                    "required": ["sentiment", "confidence"],
                },
            ),
        ),
    )
    with pytest.raises(GatewayError) as ei:
        await gw.invoke(req)
    assert ei.value.code == ErrorCode.SCHEMA_VALIDATION_FAILED  # 校验失败码
