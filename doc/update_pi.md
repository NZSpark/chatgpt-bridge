# chatgpt-bridge 项目整体分析与改进建议

> 审查基线：`NZSpark/chatgpt-bridge` `main` 分支当前公开代码（2026-10-05）。
>
> 目标：在保持现有 `OpenAI-compatible` / Pi / Codex CLI 接入能力的前提下，提高稳定性、兼容性、安全性、可维护性以及长期演进能力。

## 1. 总体结论

这个项目已经不是一个简单的“Playwright 自动输入网页”的脚本，而是一个由以下几层组成的小型 AI Gateway：

```text
OpenAI / Pi / Codex client
        |
        v
FastAPI compatibility layer
  /v1/chat/completions
  /v1/responses
  /v1/models
        |
        +---- session routing / task snapshot
        |
        +---- prompt translation
        |
        +---- simulated tool calling
        |
        v
ChatGPTWebDriver
        |
        +---- page pool / per-session locks
        +---- Playwright persistent browser
        +---- DOM polling / generation detection
        |
        v
ChatGPT Web UI
```

架构思路是合理的，尤其是把 API 层、prompt 构造、会话分桶、Responses API、工具调用解析和 Playwright driver 分开，已经具备继续扩展的基础。

但它同时存在一个根本性约束：**项目不是在调用稳定的模型 API，而是在调用一个随时可能变化的网页 UI**。因此当前很多复杂逻辑实际上是在补偿上游 Web UI 的不稳定性。随着客户端数量、并发量和工具调用复杂度提高，系统最容易先出问题的不是 FastAPI，而是“浏览器状态 + DOM 状态 + 本地持久化状态”三者逐渐失配。

综合判断：

- **当前适合**：个人本地使用、单机 Pi/Codex bridge、低并发实验环境。
- **当前不适合**：公网服务、多个独立进程共同运行、较高并发、对 OpenAI Responses API 语义要求严格的生产集成。
- **最值得投入的方向**：先建立可靠性边界，再扩展 API 兼容性；不要继续通过增加更多 DOM selector 和 parser 特例来“堆功能”。

---

## 2. P0：必须优先解决的问题

### 2.1 API 默认没有真正的认证边界

服务默认绑定 `127.0.0.1`，这在本机使用时比较安全；但代码本身没有 API token / bearer authentication，README 也允许通过配置把服务暴露到其它地址。若误配置为 `0.0.0.0`，API 实际上就会变成一个未认证的 ChatGPT 代理。

风险包括：

- 未授权用户可以消耗 ChatGPT 会话和额度。
- 用户可以创建大量不同的 session key，造成 session/page churn。
- 若启用本地 `edit_markdown`，攻击面进一步扩大。
- `/healthz` 在服务公开时会泄露 session、cluster 等内部运行状态。
- `/session/reset` 虽然支持 `RESET_TOKEN`，但只有 reset endpoint 有额外保护，整个 API 并未建立统一认证模型。

### 建议

增加统一认证中间件：

```text
API_TOKEN / Authorization: Bearer <token>
```

并采用以下规则：

1. `HOST` 为 loopback 时允许本地开发模式。
2. `HOST` 不是 loopback 时，如果没有明确配置认证，则**启动失败，而不是仅打印 warning**。
3. `/healthz` 分成 public readiness 和 authenticated detailed health 两种。
4. `/debug/*` 永远需要认证，并最好只允许 loopback。
5. 给请求 body 设置最大尺寸，例如 1–4 MiB；不要只依赖后面的 prompt clamp。
6. 加入基础 rate limit / concurrency limit。

这是项目从“个人工具”变成“可以安全放进局域网”之前最重要的一步。

---

### 2.2 `.env.example` 与 `config.py` 存在明显配置漂移

这是当前最容易造成“代码本身正确，但刚部署就失效”的问题之一。

配置默认值在 `config.py` 中已经使用了较新的 ChatGPT DOM selector，但 `.env.example` 仍包含旧的 `RESPONSE_SELECTORS`、`INPUT_SELECTORS`、`READY_SELECTOR` 等配置。由于 `.env` 会覆盖 Python 默认值，用户按 `.env.example` 复制后，反而可能重新启用已经过时的 selector。

同时还有多个默认值不一致，例如：

- `MAX_SESSION_BUCKETS`
- `STABLE_POLLS`
- `LEN_STABLE_POLLS`
- `BUCKET_LOCK_TIMEOUT_S`
- `EDIT_MARKDOWN_LOCAL`

### 建议

把配置体系改成单一事实源（single source of truth）：

```text
config.py
    |
    +-- typed defaults
    +-- validation
    +-- generated .env.example
    +-- startup config dump
```

理想方案：用 `pydantic-settings` 或一个明确的 `Settings` 对象，而不是模块级变量 + 手写 `.env` parser。

同时增加启动日志：

```text
Configuration:
  host=127.0.0.1
  port=8002
  headless=false
  responses_api=true
  session_scoping=true
  max_session_buckets=8
  edit_markdown_local=false
```

敏感字段必须打 `***`。

---

### 2.3 会话持久化存在并发一致性风险

现在 session state 使用 JSON 文件保存，而且是整个 state file 的读/写模型。当前单进程模型下问题可能不明显，但一旦：

- 同一进程出现并发请求；
- reset/task snapshot 同时发生；
- 未来用多个 worker/process；
- 一个请求持锁，另一个请求更新 snapshot；

就容易出现 lost update。

更重要的是，目前部分持久化异常会被 `except Exception: pass` 静默吞掉。这会导致真正发生状态丢失时，日志还告诉用户“请求成功”。

### 建议

短期：

1. 使用临时文件写入。
2. `flush + fsync`。
3. `os.replace()` 原子替换。
4. 写文件时使用 lock。
5. 所有 persistence error 至少记录 warning/error。

中期：改成 SQLite：

```text
sessions
session_events
session_tasks
request_log(optional)
```

SQLite 更适合当前项目，因为它可以同时解决：

- atomic update
- locking
- query
- TTL cleanup
- per-session state
- future metrics

不建议继续扩展“越来越复杂的 JSON 文件状态机”。

---

### 2.4 task snapshot 在锁外记录，存在明确 race condition

`server.py` 在真正获取 session lock 前就调用 `tasks.record()`。这意味着同一个 bucket 的两个请求可能同时：

```text
A: read old snapshot
B: read old snapshot
A: write turns=n+1
B: write turns=n+1
```

最终一个更新会覆盖另一个更新。

此外，task snapshot 把“最后一条 user message”当作 goal。这个策略简单，但从语义上并不总正确：

```text
初始目标：重构整个项目
后续消息：先跑一下测试
```

此时系统可能把“先跑一下测试”误认为新的长期任务目标。

### 建议

第一步：把 `tasks.record()` 移到 session lock 内，或者让 task store 自己具备 compare-and-swap / transaction 语义。

第二步：把“任务目标”和“对话消息”分开：

```text
TaskState
  task_id
  goal
  status
  created_at
  updated_at
  recent_context
```

只有在明确的 task 创建/更新条件下修改 goal，而不是每轮直接覆盖。

第三步：给 snapshot 增加 TTL 和容量限制，避免 `user_data/.chatgpt_tasks` 无限增长。

---

### 2.5 `PARALLEL_BUCKETS` 的并发模型需要重新定义

当前 page pool 已经支持多个 bucket / page，这是很好的设计；但“多个 Playwright page 并行”并不等于“上游 ChatGPT 服务可以安全地并行”。

实际存在三个共享资源：

```text
Browser process
  └── persistent context
        ├── cookies/session
        ├── network / rate limits
        └── multiple pages
```

所以真正需要限制的是：

- 每个 bucket 的并发：必须 1。
- Browser context 总并发：有限。
- 全局上游并发：建议有限。
- 请求创建/销毁 page 的频率：有限。

### 建议

把 concurrency 显式建模为：

```text
GlobalSemaphore
        |
        +-- SessionLock[bucket A]
        +-- SessionLock[bucket B]
        +-- SessionLock[bucket C]
```

而不是只用 bucket lock 控制。

并提供：

- `MAX_CONCURRENT_REQUESTS`
- `MAX_CONCURRENT_PAGES`
- `QUEUE_TIMEOUT`
- `REQUEST_QUEUE_SIZE`

一旦超出，立即返回明确的 `503 upstream_busy`。

---

## 3. P1：可靠性与正确性

### 3.1 最大架构风险：DOM scraping 依赖过重

当前 driver 通过 selector、DOM text、stop button、class name、response node 等信号判断：

- 输入框在哪里；
- 回复在哪里；
- ChatGPT 是否正在生成；
- 回复何时结束；
- 上下文是否超限。

项目已经把 selector 放入配置，这是正确方向，但**单纯增加 selector 数量并不能解决根本问题**。

建议把 Web driver 抽象成一个真正的 adapter：

```text
ChatGPTWebAdapter
  find_input()
  start_message()
  get_current_response()
  is_generating()
  is_ready()
  detect_context_limit()
  start_new_chat()
```

这样业务层不再知道 CSS selector、class name、DOM workaround。

进一步可以把 selector 版本化：

```text
chatgpt_ui_profile = v1
chatgpt_ui_profile = v2
```

每个 profile 有独立的 smoke test。

---

### 3.2 回复结束检测仍然过度依赖轮询

现在主要通过：

- 固定 polling interval；
- 文本变化；
- node count；
- stop button；
- stable polls；
- stall polls；
- timeout。

这个策略已经比最初的“sleep N 秒”好很多，但仍存在两个问题：

1. UI 慢时会增加等待延迟。
2. UI DOM 改动时，多个 heuristics 可能同时失效。

### 建议

优先采用页面内 `MutationObserver` 或事件驱动的“变化通知 + 后端 timeout fallback”：

```text
MutationObserver
      |
      +-- response changed
      +-- generation state changed
      +-- node replaced

backend timeout
      |
      +-- final safety net
```

同时保留当前 polling 作为 fallback，而不是唯一机制。

---

### 3.3 `_clamp_prompt()` 的“截取中间内容”存在数据损坏风险

如果 prompt 太长，当前策略是保留前半和后半，删除中间内容。

这对普通自然语言勉强可接受，但对下面这些内容可能直接破坏语义：

- JSON
- tool arguments
- markdown code fence
- XML/DSML
- patch/diff
- shell command
- Python/JSON/YAML 代码

例如一个大 tool call 被截断后，可能看起来仍然是一个字符串，但已经不是合法 JSON。

### 建议

不要对已经序列化的 prompt 做“字符串中间截断”。应该按结构压缩：

```text
messages
  -> remove old low-priority context
  -> summarize / compact
  -> truncate individual large fields
  -> validate generated tool envelope
  -> serialize
```

如果仍然超长，宁可返回明确错误：

```text
context_length_exceeded
```

也不要静默丢掉中间数据。

---

### 3.4 会话 rotation 的恢复模型仍然只是“文本播种”

当前设计没有恢复原始 Web conversation，而是开一个新 Chat，再把历史重新压缩为 prompt seed。

这是工程上合理的 fallback，但必须明确它不是等价恢复。

它可能丢失：

- 原始 conversation 状态
- 特定 UI/model 状态
- hidden conversation metadata
- attachment 上下文
- 原始工具执行上下文
- Web UI 中不可见的状态

### 建议

将 session recovery 明确分为两级：

```text
Level 1: 原 conversation 仍可用
         -> 直接继续

Level 2: conversation 不可用
         -> 使用 seed / task snapshot 恢复
```

同时给客户端返回诊断信息，例如：

```json
{
  "session_recovered": true,
  "recovery_mode": "seeded"
}
```

这样上层应用才知道上下文发生过“软重启”。

---

## 4. P1：OpenAI / Responses API 兼容性

### 4.1 当前“兼容”更接近 API shape 兼容，而不是语义兼容

`ChatCompletionRequest` 和 `ResponsesRequest` 使用 `extra="allow"` 是为了防止 Pi/Codex 因未知字段 422，这对接客户端非常实用。

但它也带来副作用：

> 客户端发送了一个字段，服务端接受了它，但实际上没有实现该语义。

例如：

- `temperature`
- `top_p`
- `max_tokens`
- reasoning-related fields
- 部分 Responses API fields
- structured content / multimodal fields

### 建议

建立一个明确的 compatibility matrix：

| Feature | Status |
|---|---|
| messages | implemented |
| streaming | implemented |
| function tools | simulated |
| temperature | accepted, ignored |
| top_p | accepted, ignored |
| images | not supported / text only |
| files | not supported |
| native Responses state | partial |
| parallel tool calls | partial / not guaranteed |

API 响应也可以通过 header 提供 warning：

```text
X-Bridge-Warning: temperature ignored
```

这样不会破坏现有客户端，又能提高透明度。

---

### 4.2 Responses API 对非文本输入的降级需要明确

目前 Responses input 会把 `input_text` / `output_text` 等文本内容抽取出来，但未知 item 会直接跳过。

这种“宽松跳过”对兼容性友好，但对用户来说非常危险：

```text
客户端认为：图片已经传给模型
Bridge 实际上：静默把图片丢了
```

### 建议

区分：

```text
unsupported but optional
unsupported and required
malformed
```

对于重要但不支持的 input type，应返回：

```json
{
  "error": {
    "type": "unsupported_input_type"
  }
}
```

而不是静默忽略。

---

## 5. P1：工具调用体系需要升级

当前项目的 tool calling 是一个非常聪明的兼容方案：让模型输出固定格式的 `TOOL_CALL:` 文本，再由 parser 解析成 OpenAI 风格 tool call。

这使 Pi/Codex 可以在没有原生 function calling 的情况下继续工作。

但它的边界必须承认：**这是 simulated tool calling，而不是 native structured tool calling。**

### 当前主要风险

1. 模型仍然可能输出普通自然语言而不是 tool call。
2. Markdown fence 可能包装/改写 JSON。
3. parser 为了容错加入了大量 JSON repair/salvage 逻辑，复杂度越来越高。
4. 任意文本都有可能伪装成工具调用。
5. `extra="allow"` 和 parser 容错结合后，调用参数的类型保证很弱。
6. parallel tool call、tool-call id、调用顺序等语义不一定和真正的 OpenAI tool calling 相同。

### 建议：建立三层验证

```text
Layer 1: lexical
    只接受行首 TOOL_CALL:

Layer 2: schema
    解析 JSON
    校验 tool name
    校验 arguments JSON Schema

Layer 3: policy
    是否允许该工具
    是否允许当前 session 调用
    是否允许当前文件路径
```

而且推荐最终统一内部结构：

```python
ToolInvocation(
    id=..., 
    name=..., 
    arguments=..., 
    source="model_text",
    validated=True,
)
```

这样未来如果真的接入 native tool calling，可以复用同一内部接口，不再让 `parse_tool_calls()` 成为核心协议。

---

## 6. P1：`edit_markdown` 是目前最值得单独加强的本地工具

项目已经采取了几个正确做法：

- 默认应尽量 dry-run；
- write 前有备份；
- 修改后做结构验证；
- 使用结构化参数而不是让模型直接输出 shell 命令。

但它仍属于“模型驱动本地文件修改”。这意味着即使调用入口不是任意 shell，也必须建立 policy boundary。

### 建议

增加路径策略：

```text
ALLOWED_EDIT_ROOTS=/workspace/project,/workspace/docs
```

只允许 canonical path 落在允许目录内，并明确拒绝：

- `..` traversal
- symlink escape
- absolute paths outside roots
- system directories
- `.ssh`
- credentials / secret files
- `.env`

此外建议支持：

```text
READ_ONLY
DRY_RUN
WRITE_REQUIRES_CONFIRMATION
WRITE
```

四级 policy，而不是一个布尔值 `EDIT_MARKDOWN_LOCAL=true/false`。

---

## 7. P1：session key 设计需要防滥用

当前 session key 支持 header / user / User-Agent 自动分桶，这是很实用的能力。

但它也是一个资源分配入口：客户端控制 session key，就能影响 page pool 和持久化 state。

### 建议

增加：

- 每个 client 最大 session 数。
- bucket idle TTL。
- persisted state TTL。
- 每个 IP/client 的 session creation rate limit。
- 最大 persisted bucket 数。
- 超过限制时返回 `429` 或明确的 `503`。

此外不要让 User-Agent 成为强隔离机制；UA 更适合作为 fallback routing，而不是安全边界。

---

## 8. P1：错误处理和可观测性还不够结构化

现在大量地方使用 `print()` 和 traceback。个人开发阶段没问题，但长期运行后很难分析：

- 哪个 session 出错？
- 哪个 request 出错？
- 哪种 selector 失败？
- ChatGPT UI 是否变更？
- timeout 是 upstream 还是 local queue？
- tool parse failure 有多少？

### 建议

建立统一日志字段：

```text
request_id
session_key_hash
client
endpoint
phase
latency_ms
upstream_latency_ms
retry_count
page_id
recovery_mode
error_type
```

session key 不要原样打日志，可 hash：

```text
session=sha256(key)[:10]
```

日志建议使用标准 `logging`，JSON 格式可选。

同时增加 metrics：

```text
bridge_requests_total
bridge_request_errors_total
bridge_request_latency_seconds
chatgpt_timeout_total
chatgpt_rotation_total
tool_call_parse_failures_total
session_bucket_count
page_pool_busy
```

如果不想引入 Prometheus，至少先做内置 `/metrics` 或可供日志聚合的 counters。

---

## 9. P1：依赖版本与可复现构建

目前 `requirements.txt` 是宽泛依赖：`fastapi`、`uvicorn`、`playwright`、`pydantic`、`openai` 等没有完整 pin。

这对于一个高度依赖 Playwright + Pydantic 行为的项目风险很大，因为：

- FastAPI/Pydantic API 变化可能导致 model behavior 改变。
- Playwright 页面行为和 browser bundle 变化会影响稳定性。
- OpenAI client schema 变化可能影响兼容层。

### 建议

采用：

```text
requirements.in      # 人工声明直接依赖
requirements.lock    # CI/release 锁定版本
```

或者使用 `uv.lock` / Poetry lock。

同时把 Python 版本写入项目元数据：

```text
requires-python >= 3.11,<3.14
```

具体版本范围应基于 CI 实际验证。

---

## 10. P1：测试结构需要从“函数单测”升级到“协议级测试”

当前已经有较好的 `toolcalls`、`prompting`、`responses` 测试基础，这说明项目并不是没有测试意识。

但对于本项目，最有价值的测试不是单纯测试字符串函数，而是测试完整协议链：

```text
OpenAI request
  -> request normalization
  -> session routing
  -> prompt build
  -> simulated tool response
  -> parser
  -> OpenAI response
```

### 建议新增测试层级

#### A. Pure unit tests

测试：

- config parsing
- prompt building
- token estimation
- session key sanitization
- tool schema validation
- JSON repair
- markdown edit safety

#### B. Protocol contract tests

固定输入：

```json
POST /v1/chat/completions
```

验证完整输出格式。

同样测试：

```text
POST /v1/responses
stream=false
stream=true
```

#### C. Fake browser integration tests

不要直接连接 ChatGPT。

给 driver 一个 fake page：

```python
FakeChatGPTPage
```

模拟：

- typing
- generation
- stop button
- context limit
- timeout
- DOM replacement
- selector failure

#### D. Real browser smoke test

单独放在 optional CI/manual workflow：

```text
CHATGPT_SMOKE_TEST=1
```

只检查登录、发送、完成检测，不在每次 PR 强制执行。

---

## 11. P2：Prompt 与模型语义层建议进一步解耦

目前 `prompting.py` 同时负责：

- message extraction
- environment/harness 过滤
- history reconstruction
- tool instructions
- seed prompt
- token estimation

这已经开始成为“大杂烩”。

建议拆成：

```text
message_normalizer.py
history_compactor.py
prompt_serializer.py
tool_protocol.py
token_estimator.py
```

内部统一数据结构后，再分别生成：

```text
DeltaPrompt
SeedPrompt
ToolPrompt
TaskRecoveryPrompt
```

这样未来想换模型或换 Web UI 时，可以保持内部消息模型稳定。

---

## 12. P2：代码文件保存功能应该和主对话路径完全隔离

当前自动提取 Markdown code block 再保存文件的能力很方便，但它不应该和主 API request lifecycle 深度耦合。

建议设计成独立 pipeline：

```text
AssistantResponse
      |
      +---- API response
      |
      +---- tool parser
      |
      +---- artifact extractor
                 |
                 +---- save_files
```

这样：

- API 默认只负责返回数据。
- 用户显式请求 save 才执行。
- 保存失败不应该让主回答失败。
- 文件路径和命名策略集中管理。

---

## 13. P2：README / 文档需要明确“兼容边界”

现在项目功能很多，但用户很容易产生一个错误印象：

> “既然是 OpenAI compatible，那就是 OpenAI API 的完全替代品。”

实际上并不是。

README 应明确写：

```text
This bridge provides protocol compatibility for selected clients.
It is not a semantic replacement for the OpenAI API.
```

并列出：

- supported
- partially supported
- ignored
- unsupported

另外建议新增：

```text
doc/architecture.md
doc/security.md
doc/compatibility.md
doc/operations.md
doc/troubleshooting.md
```

目前 `design.md` 和 `tasks.md` 已经有设计资料，但随着项目功能增长，需要从“开发笔记”升级成正式运维文档。

---

## 14. P2：增加项目工程化基础

当前仓库还可以继续补齐：

```text
pyproject.toml
LICENSE
CONTRIBUTING.md
CHANGELOG.md
.github/workflows/ci.yml
```

### CI 最低要求

每个 PR 至少执行：

```text
ruff / formatter
pytest
mypy 或 pyright（至少核心模块）
import check
```

如果项目准备长期维护，还应该增加：

- dependency vulnerability scan
- secret scan
- `pip-audit`
- CodeQL（如果 GitHub Actions 可用）

公开仓库目前没有明显的 LICENSE 文件时，项目的再分发和第三方使用边界也不够清晰，应尽快补上合适的开源许可证。

---

## 15. 推荐的新架构

如果未来准备把这个项目从“个人工具”发展成稳定的 bridge，建议最终演进到下面的结构：

```text
                    +----------------------+
                    |     Client Layer     |
                    | Pi / Codex / OpenAI  |
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    |   API Compatibility  |
                    | Chat / Responses     |
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    |   Request Pipeline   |
                    | auth / limits / id   |
                    +----------+-----------+
                               |
                 +-------------+-------------+
                 |                           |
                 v                           v
        +----------------+          +----------------+
        | Session Router |          | Task / State   |
        | bucket / lock  |          | SQLite         |
        +-------+--------+          +----------------+
                |
                v
        +----------------+
        | Model Adapter  |
        | prompt/tool    |
        +-------+--------+
                |
                v
        +----------------+
        | Web Driver     |
        | ChatGPT UI     |
        +-------+--------+
                |
                v
        +----------------+
        | Playwright     |
        +----------------+
```

关键原则是：

> API compatibility layer 不应该知道 ChatGPT DOM；DOM adapter 也不应该知道 Pi/Codex 的 API schema。

这两个边界一旦建立，后续维护成本会明显下降。

---

## 16. 优先级路线图

### Phase 1 — 稳定性与安全（最高优先级）

1. 修正 `.env.example` / `config.py` 漂移。
2. 加 API authentication。
3. 非 loopback + 无 auth 时禁止启动。
4. 加 request body size limit。
5. session/task persistence 改为 atomic write。
6. `tasks.record()` 移到正确的 concurrency boundary。
7. 为 session/page 增加全局并发控制。
8. 清理所有 silent `except: pass`。

### Phase 2 — 协议正确性

1. 建立 compatibility matrix。
2. 不再静默忽略关键 unsupported input。
3. 增强 tool schema validation。
4. 统一 ToolInvocation 内部模型。
5. 解决 prompt structural truncation。
6. 增加 Responses API contract tests。

### Phase 3 — Web driver 稳定性

1. 建立 `ChatGPTWebAdapter`。
2. DOM selector/profile 版本化。
3. MutationObserver + polling fallback。
4. context rotation / seeded recovery 可观测化。
5. real browser smoke test。

### Phase 4 — 工程化

1. lock dependencies。
2. `pyproject.toml`。
3. CI。
4. logging + metrics。
5. security / compatibility / operations 文档。
6. LICENSE / CHANGELOG。

---

## 17. 建议新增的配置

建议最终形成类似以下配置项：

```ini
# Security
API_AUTH_ENABLED=true
API_TOKEN=...
ALLOW_REMOTE_BIND=false

# Limits
MAX_REQUEST_BODY_MB=4
MAX_CONCURRENT_REQUESTS=2
MAX_CONCURRENT_PAGES=3
QUEUE_TIMEOUT_S=15
SESSION_CREATION_RATE_LIMIT=20

# State
STATE_BACKEND=sqlite
STATE_TTL_S=86400
TASK_SNAPSHOT_TTL_S=86400

# Tool policy
TOOL_CALLS_ENABLED=true
TOOL_SCHEMA_VALIDATION=true
EDIT_MARKDOWN_POLICY=dry-run
ALLOWED_EDIT_ROOTS=./doc,./workspace

# Observability
LOG_FORMAT=json
LOG_LEVEL=INFO
METRICS_ENABLED=true

# Browser
CHATGPT_UI_PROFILE=auto
BROWSER_SMOKE_TEST=false
```

这些配置不必一次全部实现，但它们能让项目边界变得清晰。

---

## 18. 最值得立即修改的具体代码点

| 优先级 | 文件 | 修改方向 |
|---|---|---|
| P0 | `chatgpt_web/server.py` | 增加统一认证、请求大小/速率限制、远程 bind protection |
| P0 | `chatgpt_web/config.py` | 统一 typed config、校验、默认值 |
| P0 | `.env.example` | 与实际 defaults 完全同步 |
| P0 | `chatgpt_web/session_store.py` | atomic persistence / lock / error logging |
| P0 | `chatgpt_web/tasks.py` | transactional snapshot、TTL、独立 goal |
| P1 | `chatgpt_web/page_pool.py` | global concurrency semaphore / queue |
| P1 | `chatgpt_web/chat_io.py` | 改进结束检测、避免结构化 prompt 字符串截断 |
| P1 | `chatgpt_web/toolcalls.py` | schema validation + normalized ToolInvocation |
| P1 | `chatgpt_web/responses.py` | compatibility matrix、unsupported input 明确报错 |
| P1 | `chatgpt_web/prompting.py` | normalizer / compactor / serializer 解耦 |
| P1 | `requirements.txt` | pin / lock 依赖 |
| P2 | `tests/` | contract + fake-browser + integration test |
| P2 | `README.md` / `doc/` | security / compatibility / operations 文档 |

---

## 19. 最终评价

项目最大的优点不是“代码很多”，而是已经形成了比较完整的桥接思路：

- API compatibility
- session sharding
- browser pooling
- task recovery
- Responses API
- simulated tool calling
- local markdown editing
- streaming

这些能力已经足够支撑 Pi/Codex 等客户端使用。

最大的技术债则集中在同一个方向：**为了弥补 ChatGPT Web UI 本身的不稳定，项目逐渐堆积了大量 heuristics、持久化状态和兼容逻辑。**

下一阶段最重要的不是继续增加更多 endpoint，而是把“边界”定义清楚：

1. 哪些是可靠保证；
2. 哪些只是 best effort；
3. 哪些 API 参数被忽略；
4. 哪些 tool call 只是模拟；
5. 哪些情况下 session 会被重建；
6. 哪些本地文件操作允许模型执行；
7. 服务在什么条件下可以安全暴露给网络。

**如果只允许做三件事，我建议按这个顺序：**

> **① 安全边界 → ② 状态/并发一致性 → ③ Tool/API 语义正确性。**

这三项完成后，项目才值得继续投入更多 UI selector、Codex 特性或高级 tool capability。

