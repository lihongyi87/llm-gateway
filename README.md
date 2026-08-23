# LLM Gateway

厂商无关的统一 LLM 调用网关：对外只认自有协议，OpenAI Responses / Anthropic Messages 差异封在 Adapter 内。

## 功能

- 按 `model` 动态路由到不同 Adapter
- 流式 SSE（`stream=true`）
- 结构化输出（`output_format` + 本地校验）
- Prompt 模板版本管理（`prompt_template`）
- 可观测性（Token 分类 + TTFT/总延迟，`GET /v1/traces/{id}`）
- 统一错误码 + 指数退避重试（最多 3 次）+ 按模型限流（429）

## 平台模型

| model | 上游协议 |
|-------|----------|
| `deepseek-v4-pro` | OpenAI Responses API |
| `deepseek-v4-flash` | Anthropic Messages API |

## 启动

```bash
cd llm-gateway
python -m venv .venv

# Windows
.\.venv\Scripts\pip.exe install -e ".[dev]"
copy .env.example .env
# 编辑 .env 填入 API Key

.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000 --reload
```

- 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/v1/health

## curl 示例

### 1. 非流式（Pro）

```bash
curl -s http://127.0.0.1:8000/v1/invoke ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-v4-pro\",\"messages\":[{\"role\":\"user\",\"content\":\"用一句话介绍 FastAPI\"}]}"
```

### 2. 流式（Flash）

```bash
curl -N http://127.0.0.1:8000/v1/invoke ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-v4-flash\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"数到 5\"}]}"
```

### 3. 结构化输出

```bash
curl -s http://127.0.0.1:8000/v1/invoke ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-v4-pro\",\"messages\":[{\"role\":\"user\",\"content\":\"情感分析：今天真开心\"}],\"output_format\":{\"type\":\"json_schema\",\"json_schema\":{\"name\":\"sentiment\",\"schema\":{\"type\":\"object\",\"properties\":{\"sentiment\":{\"type\":\"string\"},\"confidence\":{\"type\":\"number\"}},\"required\":[\"sentiment\",\"confidence\"]}}}}"
```

### 4. Prompt 模板引用

```bash
curl -s http://127.0.0.1:8000/v1/invoke ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-v4-flash\",\"prompt_template\":{\"name\":\"summarize\",\"version\":\"1.0.0\",\"variables\":{\"text\":\"这是一篇很长的文章……\",\"max_words\":\"50\"}}}"
```

### 5. 查 Trace

```bash
curl -s http://127.0.0.1:8000/v1/traces/tr_你的trace_id
```

响应中关注：`usage.input_tokens` / `usage.output_tokens`、`latency.ttft_ms` / `latency.total_ms`、`retry_count`、`prompt_name` / `prompt_version`。

### 6. 触发限流（Pro 默认 60/min，可把 .env 调小后连发）

```bash
# PowerShell 示例：连发直到出现 429
1..80 | ForEach-Object {
  curl -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8000/v1/invoke `
    -H "Content-Type: application/json" `
    -d "{\"model\":\"deepseek-v4-pro\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
}
```

## 验证（六大功能）

不依赖真实上游（使用 FakeAdapter）：

```bash
.\.venv\Scripts\python.exe scripts\verify_all.py
# 或
.\.venv\Scripts\python.exe -m pytest -q
```

| 能力 | 测试文件 |
|------|----------|
| 双模型调用 | `tests/test_models_and_http.py` |
| 流式 | `tests/test_streaming.py` |
| 结构化输出 | `tests/test_structured.py` |
| 模板引用 | `tests/test_prompts.py` |
| 可观测 | `tests/test_observability.py` |
| 重试 | `tests/test_retry.py` |
| 限流 | `tests/test_rate_limit.py` |

## 目录结构

```
app/
  adapters/          # 厂商协议翻译（invoke/stream）
  api/               # HTTP 路由
  core/              # 错误码
  prompts/           # 版本化模板
  schemas/           # 对外/对内契约
  services/          # 限流/重试/Prompt/Trace/Router/Gateway
  main.py
tests/
scripts/verify_all.py
docs/PROJECT_MEMORY.md
```
