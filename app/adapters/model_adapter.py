# ModelAdapter 抽象基类：编排层只依赖 invoke / stream，不依赖任何厂商 SDK
from abc import ABC, abstractmethod  # ABC=抽象基类；abstractmethod=子类必须实现
from typing import AsyncIterator  # 异步迭代器，用于 stream 的 yield

from app.schemas.internal import InternalRequest, InternalResponse, StreamEvent  # 内部领域模型


class ModelAdapter(ABC):
    """
    所有上游适配器的统一接口。
    方法名用 invoke/stream，与对外 InvokeRequest 对齐，不用 complete。
    """

    platform_model: str  # 本 Adapter 绑定的平台逻辑模型名，例如 deepseek-v4-pro

    @abstractmethod
    async def invoke(self, request: InternalRequest) -> InternalResponse:
        """
        非流式调用：InternalRequest → 厂商协议 → InternalResponse。
        SDK 原始对象不得返回到网关其它层。
        """
        raise NotImplementedError  # 子类必须覆盖；此处永不执行

    @abstractmethod
    async def stream(self, request: InternalRequest) -> AsyncIterator[StreamEvent]:
        """
        流式调用：逐块产出 StreamEvent（text_delta / usage / done / error）。
        Gateway 再把 StreamEvent 编成对外 StreamChunk。
        """
        raise NotImplementedError  # 子类必须覆盖
        # 下面这行仅满足类型检查器对 AsyncIterator 的理解，运行时不会走到
        yield StreamEvent(type="done")  # type: ignore[misc]
