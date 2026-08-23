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
    return {
        "status": "ok",  # 进程活着
        "models": [  # 当前路由表的诚实映射（不是作业模板里的假想接线）
            {
                "model": "deepseek-v4-pro",  # 平台逻辑名
                "upstream_model": settings.upstream_model_pro,  # .env 里的真实上游 ID
                "protocol": "openai_chat_completions",  # 实际走 Chat Completions
                "adapter": "OpenAIChatCompletionsAdapter",  # 实现类
            },
            {
                "model": "deepseek-v4-flash",  # 平台逻辑名
                "upstream_model": settings.upstream_model_flash,  # 默认 MiniMax-M3
                "protocol": "anthropic_messages",  # 实际走 Anthropic Messages
                "adapter": "AnthropicMessagesAdapter",  # 实现类
            },
        ],
        "optional_adapters": [  # 已实现但默认未挂进路由，避免被当成现网路径
            {
                "adapter": "OpenAIResponsesAdapter",  # Responses 协议翻译器
                "protocol": "openai_responses",  # 协议名
                "status": "implemented_but_not_wired",  # 未接入 ModelRouter
            }
        ],
    }


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
                if not sent:  # 一帧都没发：让 FastAPI 走统一 JSON 错误
                    raise
                # 已经发过 SSE，只能再补错误帧，不能改 HTTP 状态
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
