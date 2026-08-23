# 一次上游调用的观测记录：Metrics 聚合看它，排障回放也看它
from typing import Literal, Optional  # 终态枚举与可空字段

from pydantic import BaseModel  # Trace 作为可查询的结构化记录

from app.schemas.common import LatencyInfo, UsageInfo  # Trace 必须带用量和延迟


class TraceRecord(BaseModel):
    """GET /v1/traces/{id} 返回的单次调用事实包，不含 Prompt 原文。"""

    trace_id: str  # 追踪 ID，与响应里的 id / trace_id 对齐
    model: str  # 请求的平台模型名（requested_model）
    resolved_upstream_model: str  # 实际打到上游的模型 ID（resolved_model）
    prompt_name: Optional[str] = None  # 使用的模板名
    prompt_version: Optional[str] = None  # 使用的模板版本
    prompt_hash: Optional[str] = None  # 渲染结果哈希，用于确认「当时跑的是哪份模板」
    usage: UsageInfo  # Token 分类统计
    latency: LatencyInfo  # TTFT / 生成 / 总耗时
    retry_count: int = 0  # 网关侧实际重试次数，不含 SDK 隐式重试
    status: Literal["ok", "error"] = "ok"  # 调用终态
    error_code: Optional[str] = None  # 失败时的稳定错误码
    stop_reason: Optional[str] = None  # 模型结束原因（网关统一命名）
    created_at: str  # ISO8601 创建时间
