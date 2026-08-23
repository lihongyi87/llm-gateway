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


@pytest.mark.asyncio
async def test_structured_output_wrong_type(prompt_service, limiter, traces):
    """必填字段类型不对应 schema_validation_failed。"""
    gw = _gateway('{"sentiment":123,"confidence":0.9}', prompt_service, limiter, traces)
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
    with pytest.raises(GatewayError) as ei:
        await gw.invoke(req)
    assert ei.value.code == ErrorCode.SCHEMA_VALIDATION_FAILED  # 类型错误


@pytest.mark.asyncio
async def test_structured_output_extra_field(prompt_service, limiter, traces):
    """strict 模式下多出未声明字段应失败。"""
    gw = _gateway(
        '{"sentiment":"positive","confidence":0.9,"hack":1}',
        prompt_service,
        limiter,
        traces,
    )
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
                strict=True,
            ),
        ),
    )
    with pytest.raises(GatewayError) as ei:
        await gw.invoke(req)
    assert ei.value.code == ErrorCode.SCHEMA_VALIDATION_FAILED  # 多余字段


def test_response_format_alias_accepted():
    """作业原文字段 response_format 应映射到 output_format。"""
    req = InvokeRequest.model_validate(
        {
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        }
    )
    assert req.output_format is not None  # 别名生效
    assert req.output_format.type == "json_object"  # 类型保留


@pytest.mark.asyncio
async def test_stream_validates_structured_output(prompt_service, limiter, traces):
    """流式结束后也要做 Schema 校验；非法 JSON 应出错误帧而不是假装成功。"""
    fake = FakeAdapter(
        platform_model="deepseek-v4-pro",
        stream_parts=["not-", "json"],  # 拼起来不是 JSON
    )
    router = ModelRouter()
    router._adapters = {"deepseek-v4-pro": fake}
    router._upstream_names = {"deepseek-v4-pro": "fake-pro"}
    gw = GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)
    req = InvokeRequest(
        model="deepseek-v4-pro",
        stream=True,
        messages=[Message(role="user", content="分析情感")],
        output_format=OutputFormat(type="json_object"),
    )
    chunks = []  # 收集帧
    async for chunk in gw.stream(req):
        chunks.append(chunk)
    assert chunks[-1].done is True  # 以终态结束
    assert chunks[-1].error_code == ErrorCode.SCHEMA_VALIDATION_FAILED  # 流结束校验失败
