# 一键验证脚本：跑完全部 pytest，作为作业六大功能的可验证证据入口
import subprocess  # 调起 pytest
import sys  # 退出码
from pathlib import Path  # 定位项目根


def main() -> int:
    """在项目根执行 pytest -q，返回 pytest 退出码。"""
    root = Path(__file__).resolve().parents[1]  # scripts/ 的上一级 = 项目根
    cmd = [sys.executable, "-m", "pytest", "-q"]  # 安静模式跑测试
    print("Running:", " ".join(cmd))  # 打印命令
    print("CWD:", root)  # 打印工作目录
    completed = subprocess.run(cmd, cwd=str(root))  # 执行
    if completed.returncode == 0:  # 全绿
        print("\nVERIFY OK — streaming / structured / prompts / observability / retry / rate-limit covered.")
    else:  # 有失败
        print("\nVERIFY FAILED — see pytest output above.")
    return completed.returncode  # 透传退出码


if __name__ == "__main__":
    raise SystemExit(main())  # 脚本入口
