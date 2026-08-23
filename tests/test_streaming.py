# 覆盖：Gateway 流式输出产出 text 帧 + done 终态
import pytest  # pytest

from app.schemas.api_request import InvokeRequest, Message  # 入站
from app.services.gateway_service import GatewayService  # 编排
from app.services.model_router import ModelRouter  # 路由
from tests.fakes import FakeAdapter  # 假 Adapter


@pytest.mark.asyncio
async def test_streaming_chunks(prompt_service, limiter, traces):
    """stream=true 时应收到多段 text，并以 done=true 结束。"""
    fake = FakeAdapter(platform_model="deepseek-v4-flash", stream_parts=["你", "好"])  # Flash 假适配器
    router = ModelRouter()  # 新建路由
    router._adapters = {"deepseek-v4-flash": fake}  # 注入假 Adapter
    router._upstream_names = {"deepseek-v4-flash": "fake-flash"}  # 上游名
    gw = GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)

    req = InvokeRequest(
        model="deepseek-v4-flash",
        stream=True,
        messages=[Message(role="user", content="hi")],
    )
    chunks = []  # 收集帧
    async for chunk in gw.stream(req):  # 消费流
        chunks.append(chunk)

    texts = [c.text for c in chunks if not c.done]  # 中间帧文本
    assert texts == ["你", "好"]  # 顺序正确
    assert chunks[-1].done is True  # 最后一帧 done
    assert chunks[-1].usage is not None  # 最后一帧带 usage
    assert chunks[-1].usage.input_tokens == 3  # 假 Adapter 用量
    assert chunks[-1].error_code is None  # 成功流没有错误码


@pytest.mark.asyncio
async def test_streaming_mid_error_frame(prompt_service, limiter, traces):
    """上游中途失败时，已吐出的文本后应跟一帧 error，而不是只 raise。"""
    fake = FakeAdapter(
        platform_model="deepseek-v4-flash",
        stream_parts=["你", "好"],
        stream_error_after=1,  # 第一段之后失败
    )
    router = ModelRouter()
    router._adapters = {"deepseek-v4-flash": fake}
    router._upstream_names = {"deepseek-v4-flash": "fake-flash"}
    gw = GatewayService(router=router, prompts=prompt_service, limiter=limiter, traces=traces)
    req = InvokeRequest(
        model="deepseek-v4-flash",
        stream=True,
        messages=[Message(role="user", content="hi")],
    )
    chunks = []  # 收集帧
    async for chunk in gw.stream(req):
        chunks.append(chunk)
    assert chunks[0].text == "你"  # 已经发出的增量保留
    assert chunks[-1].done is True  # 错误终态
    assert chunks[-1].error_code == "upstream_error"  # 稳定错误码
    assert chunks[-1].error_message is not None  # 有说明
