# Adapter 内共用的用量/结束原因/非流式延迟翻译辅助（只读厂商字段，不依赖 SDK 类型）
import time  # 非流式 ttft/total 用同一把尺子
from typing import Any, Optional  # Any 用于宽松读取上游对象属性

from app.schemas.common import LatencyInfo, UsageInfo  # 网关统一用量与延迟


def map_usage(
    *,
    input_tokens: int = 0,  # 上游输入 token（已从厂商字段取出）
    output_tokens: int = 0,  # 上游输出 token
    total_tokens: Optional[int] = None,  # 上游合计；缺省则自行相加
    cached_tokens: int = 0,  # 缓存命中
    reasoning_tokens: int = 0,  # 推理 token
) -> UsageInfo:
    """把已抽出的数字填进网关 UsageInfo（字段名永远是 input/output）。"""
    total = total_tokens if total_tokens is not None else (input_tokens + output_tokens)  # 缺省自算
    return UsageInfo(
        input_tokens=input_tokens,  # 统一输入名
        output_tokens=output_tokens,  # 统一输出名
        total_tokens=total,  # 合计
        cached_tokens=cached_tokens,  # 缓存
        reasoning_tokens=reasoning_tokens,  # 推理
    )


def read_int(obj: Any, *names: str, default: int = 0) -> int:
    """
    从上游对象或 dict 上按候选字段名读取整数。
    例如 OpenAI 可能是 prompt_tokens，也可能是 input_tokens。
    """
    if obj is None:  # 上游没给 usage
        return default
    for name in names:  # 按优先级尝试字段名
        if isinstance(obj, dict):  # dict 形态
            value = obj.get(name)  # 安全取值
        else:  # 对象形态（SDK 模型）
            value = getattr(obj, name, None)  # 属性不存在则 None
        if value is not None:  # 找到了有效值
            try:
                return int(value)  # 转成 int
            except (TypeError, ValueError):  # 无法转换则试下一个名字
                continue
    return default  # 全都没有则默认值


def _child(obj: Any, name: str) -> Any:
    """从 dict 或对象上取一层子对象，没有则 None。"""
    if obj is None:  # 上游没给
        return None
    if isinstance(obj, dict):  # dict 形态
        return obj.get(name)
    return getattr(obj, name, None)  # SDK 对象形态


def _has_field(obj: Any, name: str) -> bool:
    """字段存在且不是 None 才算「上游给了」，避免 0 被当成没给。"""
    if obj is None:
        return False
    if isinstance(obj, dict):
        return name in obj and obj[name] is not None
    return getattr(obj, name, None) is not None


def read_reasoning_tokens(usage: Any) -> int:
    """
    抽出思考/推理 token。GLM / OpenAI 兼容端点常见位置：
    - usage.reasoning_tokens / usage.thinking_tokens
    - usage.completion_tokens_details.reasoning_tokens（智谱 GLM-4.6 / OpenAI o 系列）
    - usage.output_tokens_details.reasoning_tokens
    """
    if usage is None:
        return 0
    if _has_field(usage, "reasoning_tokens"):  # 顶层
        return read_int(usage, "reasoning_tokens")
    if _has_field(usage, "thinking_tokens"):  # 部分国产端点
        return read_int(usage, "thinking_tokens")
    completion_details = _child(usage, "completion_tokens_details")  # OpenAI / GLM
    if completion_details is not None:
        if _has_field(completion_details, "reasoning_tokens"):
            return read_int(completion_details, "reasoning_tokens")
        if _has_field(completion_details, "thinking_tokens"):
            return read_int(completion_details, "thinking_tokens")
    output_details = _child(usage, "output_tokens_details")  # 少数 Responses 形态
    if output_details is not None:
        return read_int(output_details, "reasoning_tokens", "thinking_tokens")
    return 0


def read_cached_tokens(usage: Any) -> int:
    """抽出缓存命中 token：顶层或 prompt_tokens_details.cached_tokens。"""
    if usage is None:
        return 0
    if _has_field(usage, "cached_tokens"):
        return read_int(usage, "cached_tokens")
    if _has_field(usage, "cache_read_input_tokens"):  # Anthropic
        return read_int(usage, "cache_read_input_tokens")
    prompt_details = _child(usage, "prompt_tokens_details") or _child(usage, "input_tokens_details")
    if prompt_details is not None:
        return read_int(prompt_details, "cached_tokens", "cache_read_input_tokens")
    return 0


def nonstream_latency(started: float) -> LatencyInfo:
    """
    非流式延迟：客户端第一次看到内容就是完整响应到达。
    因此 ttft_ms = total_ms，generation_ms = 0（拆不出「首 token 之后」）。
    """
    total_ms = (time.perf_counter() - started) * 1000.0  # 端到端毫秒
    return LatencyInfo(ttft_ms=total_ms, generation_ms=0.0, total_ms=total_ms)


def map_stop_reason(raw: Optional[str], *, default: str = "stop") -> str:
    """
    把厂商结束原因字符串归一成网关 stop_reason。
    OpenAI: stop / length / content_filter
    Anthropic: end_turn / max_tokens / stop_sequence → 映射到 stop / length
    """
    if not raw:  # 上游没给
        return default
    normalized = raw.strip().lower()  # 统一小写便于比较
    if normalized in {"end_turn", "stop", "stop_sequence", "tool_use"}:  # 正常结束类
        return "stop"
    if normalized in {"length", "max_tokens"}:  # 触达长度上限
        return "length"
    if normalized in {"content_filter", "refusal"}:  # 安全/拒答
        return "content_filter"
    return normalized  # 其它原样保留，便于排障
