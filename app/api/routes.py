# HTTP 路由：对外只暴露网关自有协议，不透传厂商字段
import json  # SSE data 行序列化

from fastapi import APIRouter  # 路由分组
from fastapi.responses import StreamingResponse  # SSE 流式响应

from app.schemas.api_request import InvokeRequest  # 入站请求体
from app.schemas.api_response import InvokeResponse  # 非流式响应
from app.schemas.trace import TraceRecord  # Trace 查询响应
from app.services.gateway_service import gateway_service  # 编排入口


# APIRouter 前缀 /v1，与路径拼成 /v1/invoke、/v1/traces/{id}
router = APIRouter(prefix="/v1")


@router.get("/health")
async def health() -> dict:
    """健康检查：进程存活即可，不探活上游。"""
    return {"status": "ok"}  # 简单 JSON


@router.post("/invoke", response_model=None)
async def invoke(request: InvokeRequest):
    """
    统一调用入口。
    stream=false → 返回 InvokeResponse JSON
    stream=true  → 返回 text/event-stream（每行 data: StreamChunk）
    """
    if request.stream:  # 流式分支
        async def event_generator():
            """把 StreamChunk 编码成 SSE 帧。"""
            async for chunk in gateway_service.stream(request):  # 消费网关流
                payload = chunk.model_dump_json()  # Pydantic → JSON 字符串
                yield f"data: {payload}\n\n"  # SSE 标准格式
                if chunk.done:  # 最后一帧后再发结束标记（可选约定）
                    yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_generator(),  # 异步生成器
            media_type="text/event-stream",  # SSE Content-Type
            headers={
                "Cache-Control": "no-cache",  # 禁止中间层缓存聚合
                "Connection": "keep-alive",  # 保持长连接
                "X-Accel-Buffering": "no",  # 关闭 nginx 类代理缓冲
            },
        )
    # 非流式：直接返回 InvokeResponse
    result: InvokeResponse = await gateway_service.invoke(request)
    return result


@router.get("/traces/{trace_id}", response_model=TraceRecord)
async def get_trace(trace_id: str) -> TraceRecord:
    """按 trace_id 查询一次调用的 Token / 延迟 / 重试等观测数据。"""
    return gateway_service.get_trace(trace_id)  # 不存在则抛 GatewayError → 统一处理器
