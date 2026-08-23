# 覆盖：Prompt 模板加载、变量替换、版本引用、缺变量/缺模板错误
import pytest  # pytest

from app.core.errors import ErrorCode, GatewayError  # 错误码断言
from app.services.prompt_service import PromptService  # 被测服务


def test_render_success(prompt_service: PromptService):
    """模板存在且变量齐全时应渲染成功，并带 hash。"""
    result = prompt_service.render(  # 渲染 1.0.0
        "summarize",
        "1.0.0",
        {"text": "很长的文章", "max_words": "50"},
    )
    assert "很长的文章" in result.content  # 变量已替换
    assert "50" in result.content  # 字数已替换
    assert result.name == "summarize"  # 名称回传
    assert result.version == "1.0.0"  # 版本回传
    assert len(result.content_hash) == 64  # sha256 hex 长度


def test_missing_variable(prompt_service: PromptService):
    """缺变量应抛 missing_prompt_variable。"""
    with pytest.raises(GatewayError) as ei:  # 捕获业务异常
        prompt_service.render("summarize", "1.0.0", {"text": "只有 text"})
    assert ei.value.code == ErrorCode.MISSING_PROMPT_VARIABLE  # 错误码


def test_unknown_template(prompt_service: PromptService):
    """不存在的版本应抛 unknown_prompt_template。"""
    with pytest.raises(GatewayError) as ei:
        prompt_service.render("summarize", "9.9.9", {"text": "a", "max_words": "1"})
    assert ei.value.code == ErrorCode.UNKNOWN_PROMPT_TEMPLATE  # 错误码


def test_list_versions(prompt_service: PromptService):
    """应能列出已有版本号。"""
    versions = prompt_service.list_versions("summarize")  # 列版本
    assert versions == ["1.0.0"]  # 临时目录只有一个
