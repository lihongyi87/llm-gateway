# Trace 存储：记录每次调用的 Token / 延迟 / 重试，并按 trace_id 查询
import uuid  # 生成唯一 trace_id
from datetime import datetime, timezone  # ISO8601 时间戳
from typing import Dict, List, Optional  # 类型注解

from app.core.errors import ErrorCode, GatewayError  # 查不到时抛统一错误
from app.schemas.common import LatencyInfo, UsageInfo  # 用量与延迟默认值
from app.schemas.trace import TraceRecord  # 观测记录模型


class TraceStore:
    """
    进程内内存 Trace 仓库（作业阶段够用）。
    Gateway 每次调用结束后 save；HTTP 层 GET /v1/traces/{id} 调 get。
    不保存 Prompt 原文与消息正文，只保存 hash / 元数据。
    """

    def __init__(self) -> None:
        self._records: Dict[str, TraceRecord] = {}  # trace_id → TraceRecord

    @staticmethod
    def new_trace_id() -> str:
        """生成带前缀的唯一追踪 ID，例如 tr_a1b2c3..."""
        return f"tr_{uuid.uuid4().hex}"  # hex 无连字符，便于 URL

    @staticmethod
    def now_iso() -> str:
        """返回 UTC 的 ISO8601 时间字符串，带 Z 后缀。"""
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def save(self, record: TraceRecord) -> TraceRecord:
        """写入或覆盖一条 Trace；返回同一条记录方便链式使用。"""
        self._records[record.trace_id] = record  # 以 trace_id 为键
        return record  # 原样返回

    def get(self, trace_id: str) -> TraceRecord:
        """按 trace_id 查询；不存在则抛 unknown_trace。"""
        record = self._records.get(trace_id)  # 查找
        if record is None:  # 未找到
            raise GatewayError(
                code=ErrorCode.UNKNOWN_TRACE,  # 稳定错误码
                message=f"trace not found: {trace_id}",  # 说明
                http_status=404,  # 资源不存在
                retryable=False,  # 查不到重试无意义
            )
        return record  # 返回记录

    def list_recent(self, limit: int = 20) -> List[TraceRecord]:
        """按写入顺序倒序列出最近若干条（调试用，非作业硬性要求）。"""
        values = list(self._records.values())  # 当前全部记录
        return list(reversed(values[-limit:]))  # 取末尾 limit 条再倒序

    def clear(self) -> None:
        """清空全部 Trace（单元测试用）。"""
        self._records.clear()  # 删光

    def build_record(
        self,
        *,
        trace_id: str,  # 追踪 ID
        model: str,  # 平台模型名
        resolved_upstream_model: str,  # 上游真实模型
        usage: Optional[UsageInfo] = None,  # Token；缺省全 0
        latency: Optional[LatencyInfo] = None,  # 延迟；缺省 total=0
        retry_count: int = 0,  # 重试次数
        status: str = "ok",  # ok / error
        error_code: Optional[str] = None,  # 失败错误码
        stop_reason: Optional[str] = None,  # 结束原因
        prompt_name: Optional[str] = None,  # 模板名
        prompt_version: Optional[str] = None,  # 模板版本
        prompt_hash: Optional[str] = None,  # 渲染 hash
    ) -> TraceRecord:
        """
        组装一条 TraceRecord（不自动 save）。
        Gateway 在调用结束时用此方法填字段，再调用 save。
        """
        return TraceRecord(
            trace_id=trace_id,  # ID
            model=model,  # 请求的平台模型
            resolved_upstream_model=resolved_upstream_model,  # 实际上游模型
            prompt_name=prompt_name,  # 模板名
            prompt_version=prompt_version,  # 模板版本
            prompt_hash=prompt_hash,  # 内容指纹
            usage=usage or UsageInfo(),  # 默认空用量
            latency=latency or LatencyInfo(),  # 默认空延迟
            retry_count=retry_count,  # 重试次数
            status="error" if status == "error" else "ok",  # 只允许两种终态
            error_code=error_code,  # 错误码
            stop_reason=stop_reason,  # 结束原因
            created_at=self.now_iso(),  # 创建时间
        )


# 进程内单例：全网关共享同一本账
trace_store = TraceStore()
