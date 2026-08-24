# HTTP 路由：对外只暴露网关自有协议，不透传厂商字段
from fastapi import APIRouter  # 路由分组
from fastapi.responses import StreamingResponse  # SSE 流式响应

from app.config import settings  # 健康检查回显实际上游模型名
from app.core.errors import GatewayError  # 流中途异常转 SSE 错误帧
from app.schemas.api_request import InvokeRequest  # 入站请求体
from app.schemas.api_response import InvokeResponse, StreamChunk  # 非流式响应 + SSE 帧
from app.schemas.trace import TraceRecord  # Trace 查询响应
from app.services.gateway_service import gateway_service  # 编排入口


# APIRouter 前缀 /v1，与路径拼成 /v1/invoke、/v1/traces/{id}
router = APIRouter(prefix="/v1")


@router.get("/health")
async def health() -> dict:
    """
    健康检查：进程存活即可，不探活上游。
    同时公开「平台名 / 实际上游 / 实际协议」，避免文档与接线说谎。
    """
    # 路由表从 ModelRouter 动态派生——诚实靠构造保证（配置切协议表跟着变）
    from app.services.model_router import model_router
    protocol_by_adapter = {
        "OpenAIChatCompletionsAdapter": "openai_chat_completions",
        "OpenAIResponsesAdapter": "openai_responses",
        "AnthropicMessagesAdapter": "anthropic_messages",
    }
    models = []
    for name, adapter in model_router._adapters.items():
        cls = type(adapter).__name__
        models.append({
            "model": name,
            "upstream_model": model_router._upstream_names[name],
            "protocol": protocol_by_adapter.get(cls, cls),
            "adapter": cls,
        })
    return {"status": "ok", "models": models}


@router.post("/invoke", response_model=None)
async def invoke(request: InvokeRequest):
    """
    统一调用入口。
    stream=false → 返回 InvokeResponse JSON
    stream=true  → 返回 text/event-stream（每行 data: StreamChunk）
    """
    if request.stream:  # 流式分支
        async def event_generator():
            """把 StreamChunk 编码成 SSE 帧；中途失败补一帧错误再 [DONE]。"""
            sent = False  # 是否已经写出过 data 行
            try:
                async for chunk in gateway_service.stream(request):  # 消费网关流
                    sent = True  # 已经开始对客户端说话
                    payload = chunk.model_dump_json()  # Pydantic → JSON 字符串
                    yield f"data: {payload}\n\n"  # SSE 标准格式
                    if chunk.done:  # 最后一帧后再发结束标记（可选约定）
                        yield "data: [DONE]\n\n"
            except GatewayError as exc:
                # StreamingResponse 响应头在开始迭代时已提交——生成器内
                # 任何时刻 raise 都只会变成 "response already started"
                # RuntimeError。SSE 的错误通道只有错误帧：一律补帧再 [DONE]，
                # 永不在生成器内 raise（零帧失败也走错误帧，客户端靠
                # error_code 判定，不依赖 HTTP 状态码）
                err = StreamChunk(
                    id="stream-error",  # 兜底 ID（网关层通常已带真实 trace_id）
                    model=request.model,  # 回显平台模型
                    text="",  # 无增量
                    done=True,  # 结束
                    error_code=exc.code,  # 稳定错误码
                    error_message=exc.message,  # 说明
                )
                yield f"data: {err.model_dump_json()}\n\n"  # 错误帧
                yield "data: [DONE]\n\n"  # 结束标记

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
