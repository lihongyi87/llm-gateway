# FastAPI 应用入口：组装路由、统一异常处理、提供 uvicorn 启动点
from fastapi import FastAPI, Request  # 应用与请求对象
from fastapi.responses import JSONResponse  # 错误 JSON 响应

from app.api.routes import router  # /v1 业务路由
from app.core.errors import GatewayError  # 业务异常
from app.schemas.errors import ErrorDetail, ErrorResponse  # 错误体形状


# 创建应用实例；title 仅文档用
app = FastAPI(
    title="LLM Gateway",  # OpenAPI 标题
    version="0.1.0",  # 与包版本对齐
    description="厂商无关的统一 LLM 调用网关",  # 简介
)

# 挂载业务路由
app.include_router(router)


@app.exception_handler(GatewayError)
async def gateway_error_handler(_request: Request, exc: GatewayError) -> JSONResponse:
    """
    把 GatewayError 翻译成统一 ErrorResponse。
    客户端只看 error.code / retryable，不依赖 HTTP 文案。
    """
    body = ErrorResponse(
        error=ErrorDetail(
            code=exc.code,  # 稳定错误码
            message=exc.message,  # 人类可读说明
            retryable=exc.retryable,  # 是否可重试
        )
    )
    return JSONResponse(
        status_code=exc.http_status,  # 业务建议的 HTTP 状态
        content=body.model_dump(),  # dict 形态 JSON
    )


@app.get("/")
async def root() -> dict:
    """根路径提示：避免 404 让人不知道服务是否起来。"""
    return {
        "service": "llm-gateway",
        "docs": "/docs",
        "invoke": "POST /v1/invoke",
        "traces": "GET /v1/traces/{trace_id}",
        "health": "GET /v1/health",
    }
