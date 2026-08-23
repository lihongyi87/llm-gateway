# 测试用假 Adapter：不打真实上游，只返回可控的 InternalResponse / StreamEvent
from typing import AsyncIterator, List, Optional  # 类型注解

from app.adapters.model_adapter import ModelAdapter  # 统一接口
from app.core.errors import ErrorCode, GatewayError  # 可注入失败
from app.schemas.common import LatencyInfo, UsageInfo  # 用量延迟
from app.schemas.internal import InternalRequest, InternalResponse, StreamEvent  # 内部模型


class FakeAdapter(ModelAdapter):
    """可配置的假适配器，供 Gateway / 路由测试注入。"""

    def __init__(
        self,
        platform_model: str = "deepseek-v4-pro",  # 平台模型名
        content: str = "hello",  # 非流式返回文本
        fail_times: int = 0,  # 前 N 次 invoke 抛可重试错误
        stream_parts: Optional[List[str]] = None,  # 流式文本片段
    ) -> None:
        self.platform_model = platform_model  # 绑定平台名
        self.content = content  # 固定回复
        self.fail_times = fail_times  # 剩余失败次数
        self.stream_parts = stream_parts or ["hel", "lo"]  # 默认两段
        self.invoke_calls = 0  # 调用计数，供断言重试

    async def invoke(self, request: InternalRequest) -> InternalResponse:
        """非流式：按 fail_times 模拟上游短暂故障后成功。"""
        self.invoke_calls += 1  # 记一次调用
        if self.fail_times > 0:  # 还需要失败
            self.fail_times -= 1  # 减次数
            raise GatewayError(  # 可重试的上游错误
                code=ErrorCode.UPSTREAM_ERROR,
                message="simulated upstream 503",
                http_status=502,
                retryable=True,
            )
        return InternalResponse(  # 成功结果
            content=self.content,
            usage=UsageInfo(input_tokens=3, output_tokens=2, total_tokens=5),
            stop_reason="stop",
            latency=LatencyInfo(total_ms=12.0),
        )

    async def stream(self, request: InternalRequest) -> AsyncIterator[StreamEvent]:
        """流式：依次产出 text_delta，再 usage / done。"""
        for part in self.stream_parts:  # 逐段文本
            yield StreamEvent(type="text_delta", content=part)
        yield StreamEvent(  # 用量
            type="usage",
            usage=UsageInfo(input_tokens=3, output_tokens=2, total_tokens=5),
        )
        yield StreamEvent(type="done", stop_reason="stop")  # 终态
