# pytest 公共夹具：临时 Prompt 目录、干净的限流器与 Trace 仓库
import pytest  # pytest 装饰器

from app.services.prompt_service import PromptService  # Prompt 服务
from app.services.rate_limiter import PerModelRateLimiter  # 限流器
from app.services.trace_store import TraceStore  # Trace 仓库


@pytest.fixture
def prompt_dir(tmp_path):
    """创建一个带 summarize@1.0.0 的临时模板目录。"""
    folder = tmp_path / "summarize"  # 模板名目录
    folder.mkdir()  # 创建目录
    # 写入简单模板，含两个变量
    (folder / "1.0.0.txt").write_text(
        "摘要不超过 {{max_words}} 字：{{text}}",
        encoding="utf-8",
    )
    return tmp_path  # 返回根目录（PromptService 的 prompts_dir）


@pytest.fixture
def prompt_service(prompt_dir):
    """指向临时目录的 PromptService。"""
    return PromptService(prompts_dir=str(prompt_dir))


@pytest.fixture
def limiter():
    """全新限流器，Pro=2 / Flash=3，便于测 429。"""
    lim = PerModelRateLimiter()  # 新建实例
    lim._limits = {"deepseek-v4-pro": 2, "deepseek-v4-flash": 3}  # 调低限额
    return lim


@pytest.fixture
def traces():
    """空的 TraceStore。"""
    return TraceStore()
