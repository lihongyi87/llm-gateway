# 统一错误协议：HTTP 状态码可以变，code 字符串必须稳定，便于客户端分支
from pydantic import BaseModel  # 错误体同样走 Pydantic，保证字段齐全


class ErrorDetail(BaseModel):
    """错误详情。客户端只依赖 code / retryable 做程序判断。"""

    code: str  # 稳定错误码，例如 unknown_model、rate_limit_exceeded
    message: str  # 人类可读说明，不包含 API Key、内部堆栈
    retryable: bool = False  # True 表示短暂故障，客户端可有限重试


class ErrorResponse(BaseModel):
    """HTTP 错误响应外壳，与成功响应对称，始终是 {\"error\": {...}}。"""

    error: ErrorDetail  # 唯一错误对象，不混入其它顶层字段
