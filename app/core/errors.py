# 网关统一错误码与异常类：HTTP 状态可变，code 字符串必须稳定
from typing import Optional  # 可选的 HTTP 状态覆盖

from app.schemas.errors import ErrorDetail, ErrorResponse  # 对外错误 JSON 形状

# 编程错误 / 入参错误：重试多少次都不会好，禁止当网络抖动处理
_NON_RETRYABLE_EXC_TYPES = (
    TypeError,  # 例如 SDK 签名不含 temperature
    ValueError,  # 参数值不合法
    AttributeError,  # 代码写错属性
    KeyError,  # 代码取错键
    NameError,  # 未定义名字
    AssertionError,  # 内部断言失败
    ImportError,  # 依赖缺失
)

# 无 HTTP 状态码时，只有名字像网络故障才重试（避免 TypeError 被当成可重试）
_NETWORK_NAME_HINTS = (
    "timeout",  # 读写/连接超时
    "connect",  # 连接失败
    "connection",  # 连接断开
    "unavailable",  # 服务暂不可用
    "reset",  # 连接重置
    "brokenpipe",  # 管道破裂
    "apiconnection",  # OpenAI APIConnectionError
    "apitimeout",  # OpenAI APITimeoutError
    "remoteprotocol",  # httpx RemoteProtocolError
    "readtimeout",  # 读超时
    "writetimeout",  # 写超时
    "connecttimeout",  # 连接超时
    "pooltimeout",  # 连接池超时
)


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
    UNKNOWN_TRACE = "unknown_trace"  # trace_id 不存在


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
    无状态码时返回 False：必须再走 is_retryable_exception 区分网络错误与编程错误。
    """
    if status_code is None:  # 单独看状态码无法判断，交给异常分类
        return False
    if status_code == 429:  # 上游限流，可退避后重试
        return True
    if 500 <= status_code < 600:  # 服务端临时故障
        return True
    return False  # 其它状态（4xx 参数/鉴权等）不重试


def is_retryable_exception(exc: Exception) -> bool:
    """
    对任意异常做重试分类：先排除编程/鉴权错误，再看 HTTP 状态，最后才认网络类名字。
    TypeError / 缺 Key / 4xx 一律不重试。
    """
    if isinstance(exc, _NON_RETRYABLE_EXC_TYPES):  # 代码或参数错误
        return False
    status_code = getattr(exc, "status_code", None)  # SDK 常直接挂状态码
    if status_code is None and getattr(exc, "response", None) is not None:
        status_code = getattr(exc.response, "status_code", None)  # 有的挂在 response 上
    name = type(exc).__name__.lower()  # 类名小写，便于匹配
    # 鉴权 / 非法请求：换 Key 或改请求才能好，重试无意义
    if any(hint in name for hint in ("auth", "permission", "badrequest", "invalidrequest", "notfound")):
        return False
    if status_code is not None:  # 有明确 HTTP 状态则按状态表
        return is_retryable_http_status(status_code)
    compact = name.replace("_", "")  # 去掉下划线方便子串匹配
    if any(hint.replace("_", "") in compact for hint in _NETWORK_NAME_HINTS):
        return True  # 无状态码但名字像网络故障
    message = str(exc).lower()  # 再看文案
    if any(hint in message for hint in ("timeout", "connection reset", "connection refused", "temporarily unavailable")):
        return True  # 文案像瞬时网络问题
    return False  # 默认不重试，避免把未知异常打三遍
