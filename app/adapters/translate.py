# Adapter 内共用的用量/结束原因翻译辅助（只读厂商字段数值，不依赖 SDK 类型）
from typing import Any, Optional  # Any 用于宽松读取上游对象属性

from app.schemas.common import UsageInfo  # 网关统一用量模型


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
