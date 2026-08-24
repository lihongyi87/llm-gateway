# LLM Gateway

厂商无关的统一 LLM 调用网关：对外只认自有协议，厂商差异封在 Adapter 内。

## 功能

- 按 `model` 动态路由到不同 Adapter
- 流式 SSE（`stream=true`）
- 结构化输出（`output_format`，兼容作业原文 `response_format` + 本地校验）
- Prompt 模板版本管理（`prompt_template`）
- 可观测性（Token 分类 + TTFT/总延迟，`GET /v1/traces/{id}`）
- 统一错误码 + 指数退避重试（最多 3 次）+ 按模型限流（429）

## 平台模型（说真话）

平台名是网关自己的路由键，**不等于**实际上游厂商或模型 ID。默认接线如下（可用 `.env` 改 Key / Base URL / 上游 ID / 协议）：

| 平台 model | 实际上游（默认） | 实际协议 | Adapter | 备注 |
|------------|------------------|----------|---------|------|
| `deepseek-v4-pro` | `glm-4.6` | OpenAI **Responses** | `OpenAIResponsesAdapter` | 走智谱 Coding Plan Responses 端点 `open.bigmodel.cn/api/v1`（官方 quick-start 表）；`PRO_PROTOCOL=openai_chat_completions` 可切回 Chat 协议，零改码 |
| `deepseek-v4-flash` | `MiniMax-M3` | Anthropic **Messages** | `AnthropicMessagesAdapter` | 环境变量仍叫 `DEEPSEEK_FLASH_*` |
| `glm-responses` | `glm-4.6` | OpenAI **Responses** | `OpenAIResponsesAdapter` | Responses 显式槽（不随 PRO_PROTOCOL 切换） |

双协议 = **Responses（pro）+ Messages（flash）**，与作业要求一致，两端点均真跑通（`/api/v1/responses` 与 MiniMax Anthropic 端点，实测见 verify.py）。协议由配置驱动：`PRO_PROTOCOL` 切换时健康检查的路由表自动跟随（表从 ModelRouter 派生，不硬编码）。

> 端点辨析（实测踩坑）：智谱 Coding Plan 三协议端点各自独立——Chat=`/api/coding/paas/v4`、Responses=`/api/v1`、Anthropic=`/api/anthropic`；在 coding/paas/v4 下拼 `/responses` 是 404，按量付费 base `/api/paas/v4` 才是扣余额的端点。

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

## 验证（作业交付：每个 case 打 PASS/FAIL + 证据）

```bash
.\.venv\Scripts\python.exe verify.py            # 主套件 8 case：双模型调用/流式TTFT/结构化(含修复)/模板/trace/错误码（真上游）
.\.venv\Scripts\python.exe verify.py ratelimit  # 限流演示：压到 3/分钟，第 4 次必 429
.\.venv\Scripts\python.exe verify.py retry      # 重试演示：坏上游 3 次指数退避后 502
.\.venv\Scripts\python.exe -m pytest -q         # 32 项单元测试（FakeTransport·不烧 API）
```

实测快照：main 8/8 PASS（流式 9 块 TTFT≈5.1s 拼出 "1 2 3 4 5"、结构化 `{"sentiment":"positive","confidence":0.99}` 走 Schema 注入修复路径）+ 限流 `[200,200,200,429]` + 重试 502 耗时 11.2s 含退避。

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
| 双模型调用 / HTTP SSE / 健康检查映射 | `tests/test_models_and_http.py` |
| 流式 + 中途错误帧 | `tests/test_streaming.py` |
| 结构化输出 / `response_format` 别名 | `tests/test_structured.py` |
| Adapter 翻译合同 | `tests/test_adapter_translate.py` |
| 模板引用 | `tests/test_prompts.py` |
| 可观测 | `tests/test_observability.py` |
| 重试（含 TypeError/4xx 不重试） | `tests/test_retry.py` |
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
