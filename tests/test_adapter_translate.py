# 覆盖：Adapter 翻译合同（不打真实上游，只验 Internal* ↔ 厂商字段）
from app.adapters.anthropic_messages_adapter import AnthropicMessagesAdapter  # Flash 协议
from app.adapters.openai_chat_completions_adapter import OpenAIChatCompletionsAdapter  # Pro 协议
from app.adapters.translate import (  # 共用翻译
    map_stop_reason,
    map_usage,
    read_cached_tokens,
    read_int,
    read_reasoning_tokens,
)
from app.schemas.api_request import OutputFormat, SchemaDefinition  # 结构化约束
from app.schemas.internal import InternalMessage  # 内部消息


def test_chat_completions_messages_and_response_format():
    """Chat Completions：消息原样拷贝；output_format → response_format.json_schema。"""
    adapter = OpenAIChatCompletionsAdapter(api_key="test-key")  # 不发网
    messages = adapter._to_messages(
        [
            InternalMessage(role="system", content="规则"),
            InternalMessage(role="user", content="你好"),
        ]
    )
    assert messages == [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "你好"},
    ]
    fmt = adapter._response_format(
        OutputFormat(
            type="json_schema",
            json_schema=SchemaDefinition(
                name="sentiment",
                schema={"type": "object", "properties": {"sentiment": {"type": "string"}}},
            ),
        )
    )
    assert fmt is not None  # 必须翻译出厂商字段
    assert fmt["type"] == "json_schema"  # Chat Completions 的 type
    assert fmt["json_schema"]["name"] == "sentiment"  # Schema 名下传
    assert fmt["json_schema"]["schema"]["type"] == "object"  # 本体仍叫 schema


def test_anthropic_splits_system_out_of_messages():
    """Anthropic：system 必须抽出去，messages 里只留 user/assistant。"""
    adapter = AnthropicMessagesAdapter(api_key="test-key")  # 不发网
    system, body = adapter._split_system_and_messages(
        [
            InternalMessage(role="system", content="规则A"),
            InternalMessage(role="system", content="规则B"),
            InternalMessage(role="user", content="问"),
        ]
    )
    assert system == "规则A\n\n规则B"  # 多条 system 拼接
    assert body == [{"role": "user", "content": "问"}]  # system 不进 messages


def test_translate_usage_and_stop_reason():
    """共用翻译：厂商用量字段 / 结束原因归一到网关名。"""
    usage = map_usage(input_tokens=3, output_tokens=2)  # 不传 total 则自加
    assert usage.input_tokens == 3  # 网关输入名
    assert usage.output_tokens == 2  # 网关输出名
    assert usage.total_tokens == 5  # 自行合计
    assert read_int({"prompt_tokens": 9}, "prompt_tokens", "input_tokens") == 9  # OpenAI 旧字段
    assert map_stop_reason("end_turn") == "stop"  # Anthropic → 网关
    assert map_stop_reason("max_tokens") == "length"  # 触顶
    assert map_stop_reason("content_filter") == "content_filter"  # 安全


def test_glm_reasoning_and_cached_tokens():
    """GLM / OpenAI 兼容 usage：思考 token 从 completion_tokens_details 拆出。"""
    usage = {
        "prompt_tokens": 10,  # 输入
        "completion_tokens": 50,  # 输出（可能含思考）
        "total_tokens": 60,  # 合计
        "completion_tokens_details": {"reasoning_tokens": 18},  # 智谱思考
        "prompt_tokens_details": {"cached_tokens": 4},  # 缓存命中
    }
    assert read_reasoning_tokens(usage) == 18  # 思考拆出来
    assert read_cached_tokens(usage) == 4  # 缓存拆出来
    adapter = OpenAIChatCompletionsAdapter(api_key="test-key")  # 不发网
    info = adapter._usage_from(usage)  # 走 Adapter 翻译
    assert info.input_tokens == 10
    assert info.output_tokens == 50
    assert info.total_tokens == 60
    assert info.reasoning_tokens == 18
    assert info.cached_tokens == 4


def test_reasoning_tokens_zero_when_absent():
    """上游没给思考字段时保持 0，不能瞎填。"""
    assert read_reasoning_tokens({"prompt_tokens": 1, "completion_tokens": 2}) == 0
    assert read_cached_tokens({"prompt_tokens": 1}) == 0
