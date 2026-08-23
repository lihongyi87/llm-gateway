# Prompt 服务：按 name+version 加载模板，做受限变量替换，并返回可追溯的 hash
import hashlib  # 计算渲染结果 hash，供 Trace 回放
import re  # 用正则做 {{var}} 替换，禁止 eval/exec
from dataclasses import dataclass  # 简单结果对象，不必上 Pydantic
from pathlib import Path  # 跨平台路径
from typing import Dict, List, Optional, Set  # 类型注解

from app.config import settings  # 读取 prompts_dir
from app.core.errors import ErrorCode, GatewayError  # 模板缺失 / 变量缺失


# 只匹配 {{ variable_name }}，不允许表达式、过滤器、调用
_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass
class PromptRenderResult:
    """一次模板渲染的结果，交给 Gateway 写入 messages 和 Trace。"""

    name: str  # 模板名
    version: str  # 版本号
    content: str  # 渲染后的完整文本（通常作为 system）
    content_hash: str  # sha256 十六进制，不含密钥但能区分内容
    required_variables: List[str]  # 模板声明需要的变量列表


class PromptService:
    """
    文件系统版 Prompt 仓库。
    约定路径：{prompts_dir}/{name}/{version}.txt
    例如：app/prompts/summarize/1.0.0.txt
    """

    def __init__(self, prompts_dir: Optional[str] = None) -> None:
        # 允许注入目录，方便测试；默认读配置
        root = prompts_dir if prompts_dir is not None else settings.prompts_dir
        self._root = Path(root)  # 转成 Path 对象

    def _template_path(self, name: str, version: str) -> Path:
        """拼出模板文件路径；禁止 name/version 含路径穿越字符。"""
        if "/" in name or "\\" in name or ".." in name:  # 防目录穿越
            raise GatewayError(
                code=ErrorCode.INVALID_REQUEST,
                message=f"invalid prompt template name: {name}",
                http_status=400,
                retryable=False,
            )
        if "/" in version or "\\" in version or ".." in version:  # 版本同样校验
            raise GatewayError(
                code=ErrorCode.INVALID_REQUEST,
                message=f"invalid prompt template version: {version}",
                http_status=400,
                retryable=False,
            )
        return self._root / name / f"{version}.txt"  # 固定 .txt 后缀

    def load_raw(self, name: str, version: str) -> str:
        """读取模板原文；不存在则抛 unknown_prompt_template。"""
        path = self._template_path(name, version)  # 解析路径
        if not path.is_file():  # 文件不存在
            raise GatewayError(
                code=ErrorCode.UNKNOWN_PROMPT_TEMPLATE,
                message=f"prompt template not found: {name}@{version}",
                http_status=404,
                retryable=False,
            )
        return path.read_text(encoding="utf-8")  # UTF-8 读取全文

    def extract_variables(self, template_text: str) -> List[str]:
        """从模板正文提取所有 {{var}}，去重且保持出现顺序。"""
        seen: Set[str] = set()  # 已见过的变量名
        ordered: List[str] = []  # 有序列表
        for match in _VAR_PATTERN.finditer(template_text):  # 逐个匹配
            var_name = match.group(1)  # 捕获组 1 = 变量名
            if var_name not in seen:  # 首次出现才加入
                seen.add(var_name)
                ordered.append(var_name)
        return ordered  # 返回有序变量列表

    def render(self, name: str, version: str, variables: Dict[str, str]) -> PromptRenderResult:
        """
        加载模板并做受限变量替换。
        - 缺变量 → missing_prompt_variable
        - 多余变量允许忽略（不报错），避免客户端多传无关键
        - 禁止任意表达式：只替换 {{name}} 字面量
        """
        raw = self.load_raw(name, version)  # 读原文
        required = self.extract_variables(raw)  # 模板需要哪些变量
        missing = [v for v in required if v not in variables]  # 找出缺失
        if missing:  # 有缺失则拒绝渲染
            raise GatewayError(
                code=ErrorCode.MISSING_PROMPT_VARIABLE,
                message=f"missing prompt variables for {name}@{version}: {', '.join(missing)}",
                http_status=400,
                retryable=False,
            )

        def _replace(match: re.Match[str]) -> str:
            """把单个 {{var}} 替换成 variables 里的字符串值。"""
            key = match.group(1)  # 变量名
            return variables[key]  # 此处已保证存在

        rendered = _VAR_PATTERN.sub(_replace, raw)  # 全文替换
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()  # 内容指纹
        return PromptRenderResult(
            name=name,  # 模板名
            version=version,  # 版本
            content=rendered,  # 渲染结果
            content_hash=digest,  # hash
            required_variables=required,  # 变量清单
        )

    def list_versions(self, name: str) -> List[str]:
        """列出某模板已有版本号（文件名去掉 .txt），供管理/调试。"""
        directory = self._root / name  # 模板目录
        if not directory.is_dir():  # 目录不存在
            return []  # 空列表
        versions: List[str] = []  # 收集版本
        for path in sorted(directory.glob("*.txt")):  # 只认 .txt
            versions.append(path.stem)  # stem = 无后缀文件名
        return versions  # 返回版本列表


# 进程内默认单例，Gateway 可直接 import
prompt_service = PromptService()
