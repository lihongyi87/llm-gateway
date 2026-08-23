# llm-gateway 项目记忆（每步做完更新，新会话先读此文件）

> 仓库：https://github.com/lihongyi87/llm-gateway  
> 工作流：**每步做完 → commit → push → 停 → 用户说继续**

---

## 核心原则（不要忘）

1. **对外 API 是网关自己定义的**，不绑定 OpenAI/Anthropic 字段名
2. **厂商差异只在 Adapter 里翻译**（input_tokens、stop_reason、output_format 等）
3. **SDK 设 max_retries=0**，重试只在 `app/services/retry.py` 一层
4. **每行代码要有注释**（用户要求）
5. 路由：`POST /v1/invoke`（不是 /v1/chat/completions）
6. Adapter 方法：`invoke`（非流式）+ `stream`（流式），不用 `complete`
7. 文件名与类名对齐：`model_adapter.py` ↔ `ModelAdapter`（snake_case ↔ PascalCase）

---

## 进度清单

| 步 | 内容 | 提交 | 状态 |
|----|------|------|------|
| 1 | 项目骨架 pyproject.toml / .env.example / app 包 | ff29d1a | ✅ |
| 2 | config.py + schemas 分包（厂商无关 API） | 125b38a | ✅ |
| 3 | 错误码 + 重试 + 按模型限流 | 83dabd9 | ✅ |
| 4 | Adapter 基类 + OpenAI Responses + Anthropic Messages | 621b155 | ✅ |
| 5 | Prompt 版本管理 | 6d27791 | ✅ |
| 6 | 可观测性 Trace 存储 | 46041c8 | ✅ |
| 7 | Gateway 编排 + Router | c1d402b | ✅ |
| 8 | FastAPI routes + main | 682cb3c | ✅ |
| 9 | 测试 + README + 验证脚本 | 6363a51 | ✅ |
| 10 | 对抗审查修复：诚实路由/别名/重试分类/SSE错误帧/深校验 | （本步） | ✅ |

---

## 第 2 步：schemas 分层（厂商无关）

```
app/schemas/
├── api_request.py   → InvokeRequest, Message, OutputFormat, PromptTemplateRef
├── api_response.py  → InvokeResponse, StreamChunk（扁平，无 choices/object）
├── internal.py      → InternalRequest/Response, StreamEvent（Adapter 边界）
├── common.py        → UsageInfo(input_tokens/output_tokens), LatencyInfo
├── errors.py        → ErrorDetail, ErrorResponse
└── trace.py         → TraceRecord
```

**对外 vs 内部**：客户端只认 Invoke*；Adapter 只吃 Internal*。

---

## 第 3 步：韧性三件套

### 文件

| 文件 | 职责 |
|------|------|
| `app/core/errors.py` | ErrorCode 常量 + GatewayError 异常 + is_retryable_http_status |
| `app/services/retry.py` | compute_backoff_seconds + retry_async |
| `app/services/rate_limiter.py` | PerModelRateLimiter + 单例 rate_limiter |

### 调用顺序（Gateway 将来这样用）

```
请求 → rate_limiter.acquire(model) → retry_async(lambda: adapter.invoke(...)) → 写 Trace
```

### Adapter 方法命名（厂商无关，对齐 InvokeRequest）

| 方法 | 用途 | 返回 |
|------|------|------|
| `invoke` | 非流式调用 | `InternalResponse` |
| `stream` | 流式调用 | `AsyncIterator[StreamEvent]` |

禁止用 `complete`（易与 OpenAI Completions 绑定）。

### ErrorCode 一览

- unknown_model / unknown_prompt_template / missing_prompt_variable
- rate_limit_exceeded (429, retryable=True) — **网关自己的限流**
- upstream_error (502) — 重试耗尽
- schema_validation_failed / invalid_request / internal_error

### 重试规则

- 最多 3 次（config.max_retry_attempts）
- 退避：`min(8, 0.5 * 2^attempt) + uniform(0, 0.2)`
- 可重试：上游 429、5xx、名字/文案像网络故障的异常
- 不可重试：TypeError/ValueError 等编程错误、400/401、无状态码的未知异常、GatewayError.retryable=False、RATE_LIMIT_EXCEEDED
- `is_retryable_http_status(None)` 现在是 **False**；无状态码必须走 `is_retryable_exception`

### 限流规则

- pro: 60 req/min，flash: 120 req/min（.env 可配）
- 滑动窗口 60 秒，按 model 独立计数
- 未知 model 不限流（交给路由层报 unknown_model）

---

## 模型路由（config）

| 平台名 | 实际协议（现网） | 默认上游 ID | 配置项 |
|--------|------------------|------------|--------|
| deepseek-v4-pro | OpenAI Chat Completions | glm-4.6 | upstream_model_pro / DEEPSEEK_PRO_* |
| deepseek-v4-flash | Anthropic Messages | MiniMax-M3 | upstream_model_flash / DEEPSEEK_FLASH_* |

`OpenAIResponsesAdapter` = 可选实现，**未接入 ModelRouter**。

---

## 第 4 步：Adapter（厂商翻译层）

| 文件 | 职责 |
|------|------|
| `adapters/model_adapter.py` | `ModelAdapter`：`invoke` + `stream` |
| `adapters/translate.py` | usage/stop_reason 数字与字符串归一 |
| `adapters/openai_chat_completions_adapter.py` | `OpenAIChatCompletionsAdapter`：现网 Pro → Chat Completions |
| `adapters/openai_responses_adapter.py` | `OpenAIResponsesAdapter`：可选，默认未接线 |
| `adapters/anthropic_messages_adapter.py` | `AnthropicMessagesAdapter`：Flash → Messages API |

翻译对照：

| 网关 | OpenAI Responses | Anthropic Messages |
|------|------------------|-------------------|
| input_tokens | input_tokens 或 prompt_tokens | input_tokens |
| output_tokens | output_tokens 或 completion_tokens | output_tokens |
| stop_reason | status 等 | stop_reason（end_turn→stop） |
| output_format | text.format | extra_body.output_format |

SDK 一律 `max_retries=0`。

## 第 5 步：Prompt 版本管理

| 文件 | 职责 |
|------|------|
| `services/prompt_service.py` | `PromptService`：加载 / 提取变量 / 受限渲染 / hash |
| `prompts/{name}/{version}.txt` | 模板存储，示例 summarize@1.0.0 / 1.1.0 |

规则：
- 路径：`app/prompts/{name}/{version}.txt`
- 变量：仅 `{{var_name}}`，无表达式、无 eval
- 缺变量 → `missing_prompt_variable`
- 模板不存在 → `unknown_prompt_template`
- 渲染结果带 `content_hash`（sha256）供 Trace

## 第 6 步：可观测性 Trace

| 文件 | 职责 |
|------|------|
| `services/trace_store.py` | `TraceStore`：生成 id、组装、save、get、list_recent |
| `schemas/trace.py` | `TraceRecord` 字段定义 |

能力：
- `new_trace_id()` → `tr_<uuid>`
- `build_record(...)` + `save(...)` 写入
- `get(trace_id)` 查询；不存在 → `unknown_trace` (404)
- 记录：model / resolved_upstream_model / usage(input/output) / latency(ttft/total) / retry_count / prompt_* / status
- **不存** Prompt 原文与消息正文（只存 hash）

## 第 7 步：Gateway + Router

| 文件 | 类 | 职责 |
|------|-----|------|
| `services/model_router.py` | `ModelRouter` | platform model → Adapter + upstream_model |
| `services/gateway_service.py` | `GatewayService` | 整条编排 |

编排顺序（invoke）：
```
限流 → 校验 output_format → 路由 → 渲染 Prompt → InternalRequest
→ retry_async(adapter.invoke) → 本地 JSON/Schema 校验 → 写 Trace → InvokeResponse
```

流式：同样前置，但 `adapter.stream` 不做整段重试；Gateway 测 TTFT 写 Trace。

## 第 8 步：HTTP 入口

| 文件 | 职责 |
|------|------|
| `main.py` | FastAPI app + GatewayError → ErrorResponse |
| `api/routes.py` | 路由定义 |

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/health` | 健康检查 |
| POST | `/v1/invoke` | 统一调用（stream 开关） |
| GET | `/v1/traces/{trace_id}` | 查观测 |

启动：
```bash
cd llm-gateway
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

## 第 9 步：验收

- `README.md`：启动 + curl 示例
- `scripts/verify_all.py`：一键跑 pytest
- `tests/`：流式 / 结构化 / 模板 / 观测 / 重试 / 限流 / 双模型 / health

```bash
python scripts/verify_all.py
```

作业六大功能均有自动化证据（FakeAdapter，不依赖真实 Key）。

## 第 10 步：对抗审查修复

审查结论：作业六项能交差，但经不起「文档/验收字段/重试误伤/流式 HTTP/校验深度」追问。本步按 5 条修：

1. **说真话**：README / PROJECT_MEMORY / `GET /v1/health` 写清平台名、实际上游、实际协议；Responses Adapter 标成 optional。
2. **`response_format` 别名**：`InvokeRequest.output_format` 同时接受作业原文字段，避免助教按原文验收挂。
3. **重试先分类**：`TypeError` / 4xx / 缺 Key 不重试；修掉 `status_code is None → 一律重试`。
4. **流式错误帧 + HTTP SSE 测试**：中途失败 yield `error_code` 终态帧；`tests/test_models_and_http.py` 走 TestClient SSE。
5. **结构化深校验**：必填 + 类型 + strict 多余字段；流结束也验；`tests/test_adapter_translate.py` 锁翻译合同。

顺带：`SchemaDefinition.schema` 改名为 `schema_body`（JSON 别名仍是 `schema`）；限流加锁。
