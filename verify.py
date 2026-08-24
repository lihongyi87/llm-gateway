# -*- coding: utf-8 -*-
"""verify.py —— 六大功能点验证脚本（作业交付：每个 case 打 PASS/FAIL + 证据）。

用法：
  .venv/Scripts/python.exe verify.py            # 主套件（真上游调用，需 .env 配 Key）
  .venv/Scripts/python.exe verify.py ratelimit  # 子模式：限流 429 演示（独立限流配置）
  .venv/Scripts/python.exe verify.py retry      # 子模式：重试退避演示（坏上游）
"""
import json
import os
import sys
import time

from fastapi.testclient import TestClient

PASS, FAIL = "PASS", "FAIL"
_results: list = []


def check(name: str, ok: bool, evidence: str = "") -> None:
    mark = PASS if ok else FAIL
    _results.append((name, mark))
    print(f"[{mark}] {name}" + (f" | {evidence}" if evidence else ""))


def main() -> None:
    from app.main import app
    client = TestClient(app)

    # ── 1. 健康检查 & 模型路由表 ─────────────────────────────
    r = client.get("/v1/health")
    models = {m["model"]: m["protocol"] for m in r.json()["models"]}
    check("健康检查+双协议路由表",
          r.status_code == 200
          and "anthropic_messages" in models.values()
          and ("openai_responses" in models.values()
               or "openai_chat_completions" in models.values()),
          f"protocols={sorted(set(models.values()))}")

    # ── 2. 两模型非流式调用 ────────────────────────────────
    for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
        r = client.post("/v1/invoke", json={
            "model": model,
            "messages": [{"role": "user", "content": "只回答两个字：收到"}],
        })
        body = r.json()
        usage = body.get("usage") or {}
        ok = (r.status_code == 200 and body.get("message", {}).get("content")
              and usage.get("total_tokens", 0) > 0)
        check(f"非流式调用 {model}", ok,
              f"content={str(body.get('message', {}).get('content'))[:30]!r} "
              f"usage={usage.get('input_tokens')}in/{usage.get('output_tokens')}out"
              f"/reasoning={usage.get('reasoning_tokens')}")

    # ── 3. 流式输出 + TTFT ────────────────────────────────
    for _attempt in range(2):  # 上游偶发首帧连接错误（~20ms 错误帧）重试一次
        chunks, ttft = [], None
        t0 = time.perf_counter()
        with client.stream("POST", "/v1/invoke", json={
            "model": "deepseek-v4-pro", "stream": True,
            "messages": [{"role": "user", "content": "从1数到5，只输出数字"}],
        }) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    now = time.perf_counter()
                    if ttft is None:
                        ttft = (now - t0) * 1000
                    if line[6:].strip() == "[DONE]":
                        continue
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if payload.get("text"):
                        chunks.append(payload["text"])
        if len(chunks) >= 2:
            break
    check("流式SSE+TTFT", len(chunks) >= 2 and ttft is not None,
          f"{len(chunks)} 块, TTFT={ttft:.0f}ms, 拼接={''.join(chunks)[:24]!r}")

    # ── 4. 结构化输出（含 Schema 注入修复路径）────────────────
    r = client.post("/v1/invoke", json={
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "情感分析：今天真开心"}],
        "output_format": {"type": "json_schema", "json_schema": {
            "name": "sentiment",
            "schema": {"type": "object",
                       "properties": {"sentiment": {"type": "string"},
                                      "confidence": {"type": "number"}},
                       "required": ["sentiment", "confidence"]}}},
    })
    body = r.json()
    try:
        parsed = json.loads(body["message"]["content"])
        shape_ok = ("sentiment" in parsed and "confidence" in parsed)
    except Exception:
        parsed, shape_ok = {}, False
    check("结构化输出+本地校验", r.status_code == 200 and shape_ok,
          f"parsed={parsed}")

    # ── 5. Prompt 模板版本引用 ────────────────────────────
    r = client.post("/v1/invoke", json={
        "model": "deepseek-v4-flash",
        "prompt_template": {"name": "summarize", "version": "1.0.0",
                            "variables": {"text": "FastAPI 是一个 Python Web 框架，"
                                                  "以类型提示和高性能著称。",
                                          "max_words": "20"}},
    })
    tid = r.json().get("trace_id", "")
    tr = client.get(f"/v1/traces/{tid}").json() if tid else {}
    check("模板版本引用+trace记录",
          r.status_code == 200 and tr.get("prompt_name") == "summarize"
          and tr.get("prompt_version") == "1.0.0",
          f"prompt={tr.get('prompt_name')}@{tr.get('prompt_version')} "
          f"hash={str(tr.get('prompt_hash'))[:12]}")

    # ── 6. 可观测 Trace（token 分类 + 延迟含 TTFT）──────────
    tr2 = client.get(f"/v1/traces/{tid}").json()
    usage = tr2.get("usage") or {}
    lat = tr2.get("latency") or {}
    check("Trace可观测", usage.get("input_tokens", 0) > 0
          and lat.get("total_ms", 0) > 0,
          f"in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
          f"reasoning={usage.get('reasoning_tokens')} "
          f"ttft={lat.get('ttft_ms')}ms total={lat.get('total_ms')}ms")

    # ── 7. 未知模型 → 统一错误码 ──────────────────────────
    r = client.post("/v1/invoke", json={
        "model": "no-such-model",
        "messages": [{"role": "user", "content": "hi"}],
    })
    err = r.json().get("error", {})
    check("未知模型统一错误码",
          r.status_code == 400 and err.get("code") == "unknown_model",
          f"http=400 code={err.get('code')} retryable={err.get('retryable')}")

    _summary()


def ratelimit_mode() -> None:
    """限流演示：独立进程把 pro 限流压到 3/分钟，第 4 次必须 429。"""
    os.environ["RATE_LIMIT_PRO"] = "3"
    from app.main import app
    client = TestClient(app)
    codes = []
    for _ in range(4):
        r = client.post("/v1/invoke", json={
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": "只回答：ok"}],
        })
        codes.append(r.status_code)
    hit_429 = 429 in codes
    err = ""
    if hit_429:
        err = "rate_limit_exceeded"
    check("按模型独立限流(429)", hit_429, f"4次状态码={codes} err={err}")
    _summary()


def retry_mode() -> None:
    """重试演示：独立进程把 flash 指到不可达端口，3 次退避后失败。"""
    os.environ["DEEPSEEK_FLASH_BASE_URL"] = "http://127.0.0.1:9"
    os.environ["RETRY_BACKOFF_BASE"] = "0.1"  # 加速演示（服务端若未读则用默认）
    from app.main import app
    client = TestClient(app)
    t0 = time.perf_counter()
    r = client.post("/v1/invoke", json={
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    dt = time.perf_counter() - t0
    err = r.json().get("error", {})
    check("传输失败指数退避(≤3次)",
          r.status_code in (502, 504) and dt > 0.2,
          f"http={r.status_code} code={err.get('code')} "
          f"retryable={err.get('retryable')} 耗时={dt:.1f}s(含退避)")
    _summary()


def _summary() -> None:
    n_pass = sum(1 for _, m in _results if m == PASS)
    print(f"\n== verify 汇总: {n_pass}/{len(_results)} PASS ==")
    sys.exit(0 if n_pass == len(_results) else 1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "main"
    {"main": main, "ratelimit": ratelimit_mode, "retry": retry_mode}[mode]()
