# 网关统一错误码与异常类：HTTP 状态可变，code 字符串必须稳定
from typing import Optional  # 可选的 HTTP 状态覆盖

from app.schemas.errors import ErrorDetail, ErrorResponse  # 对外错误 JSON 形状


class ErrorCode:
    """稳定错误码常量。客户端应只依赖这些字符串做分支，不解析 message。"""

    UNKNOWN_MODEL = "unknown_model"  # 请求的 model 不在平台白名单
    UNKNOWN_PROMPT_TEMPLATE = "unknown_prompt_template"  # 模板名或版本不存在
    MISSING_PROMPT_VARIABLE = "missing_prompt_variable"  # 模板渲染缺变量
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"  # 网关按模型限流，超限
    UPSTREAM_ERROR = "upstream_error"  # 上游失败且重试耗尽
    UPSTREAM_TIMEOUT = "upstream_timeout"  # 上游超时
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"  # 结构化输出校验失败
    INVALID_REQUEST = "invalid_request"  # 入站参数不合法（编排层发现）
    INTERNAL_ERROR = "internal_error"  # 未分类的内部错误


class GatewayError(Exception):
    """
    网关业务异常：携带稳定错误码、HTTP 状态、是否可重试。
    FastAPI 异常处理器把它翻译成 ErrorResponse JSON。
    """

    def __init__(
        self,
        code: str,  # 稳定错误码，见 ErrorCode
        message: str,  # 人类可读说明，禁止包含 API Key
        http_status: int = 400,  # 建议的 HTTP 状态码
        retryable: bool = False,  # 是否建议客户端重试
    ) -> None:
        super().__init__(message)  # 让 str(exc) 能打印 message
        self.code = code  # 保存错误码供处理器读取
        self.message = message  # 保存说明文字
        self.http_status = http_status  # 保存 HTTP 状态
        self.retryable = retryable  # 保存是否可重试

    def to_response(self) -> ErrorResponse:
        """把异常转成对外 ErrorResponse，供 FastAPI 返回 JSON。"""
        return ErrorResponse(
            error=ErrorDetail(
                code=self.code,  # 稳定错误码
                message=self.message,  # 说明
                retryable=self.retryable,  # 是否可重试
            )
        )


def is_retryable_http_status(status_code: Optional[int]) -> bool:
    """
    判断上游 HTTP 状态是否属于「短暂故障，网关可退避重试」。
    429 / 5xx 可重试；401 / 400 等不可重试。
    """
    if status_code is None:  # 无状态码（如纯网络错误）视为可重试
        return True
    if status_code == 429:  # 上游限流，可退避后重试
        return True
    if 500 <= status_code < 600:  # 服务端临时故障
        return True
    return False  # 其它状态（4xx 参数/鉴权等）不重试
