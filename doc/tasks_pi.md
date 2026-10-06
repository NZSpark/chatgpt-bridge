# ChatGPTBridge 改进任务分解

> 来源：doc/update_pi.md 
> 日期：2026-10-06 
> 目标：将分析建议转化为可执行、可验收、可分阶段提交的工程任务。

## 0. 任务规则

### 状态

- TODO：尚未开始。
- DOING：正在实施。
- DONE：代码、测试、文档均完成。
- BLOCKED：依赖未满足或存在外部阻塞。
- DEFERRED：暂缓，不阻塞主线。

### 优先级

- P0：核心可靠性、状态正确性、网页适配风险。
- P1：重要工程能力与回归防护。
- P2：长期架构、性能和部署优化。

### 默认完成定义

除特别说明外，每项实现任务必须满足：

1. 新增或更新对应测试；
2. pytest -q 无新增失败；
3. git diff --check 通过；
4. 必要时同步更新 README / doc/update.md / 本文件；
5. 不破坏 Chat Completions、Responses、streaming、tool calling、session isolation。

---

# 1. P0：统一任务状态和协议

## PI-001 统一任务状态机

状态：DONE｜优先级：P0｜依赖：无

### 目标

把“是否调用工具、是否继续等待、是否最终完成、是否重试”从分散的布尔判断改成显式状态机。

### 建议状态

text
RECEIVED
PROMPT_BUILT
MODEL_GENERATING
TOOL_CALL_DETECTED
TOOL_EXECUTING
TOOL_RESULT_RETURNED
COMPLETED
FAILED
TIMEOUT
CONTEXT_LIMIT
UPSTREAM_BUSY
SESSION_RECOVERY


### 实施

1. 新建 chatgpt_web/task_state.py。
2. 定义状态枚举、状态对象和合法迁移。
3. chat_io.py、prompting.py、toolcalls.py、completion.py 逐步接入。
4. 保持当前 API 行为不变。

### 验收

- 工具执行后的最终纯文本不会触发新 prompt。
- 首轮未调用工具仍按当前策略执行 nudge。
- COMPLETED 不允许回到 MODEL_GENERATING。
- timeout / context limit / busy 进入明确终态。

## PI-002 状态机不变量测试

状态：DONE｜优先级：P0｜依赖：PI-001

增加状态迁移测试，并固化以下不变量：

- MODEL_GENERATING → TOOL_CALL_DETECTED 合法；
- TOOL_EXECUTING → TOOL_RESULT_RETURNED 合法；
- TOOL_RESULT_RETURNED → MODEL_GENERATING 合法；
- MODEL_GENERATING → COMPLETED 合法；
- COMPLETED → TOOL_EXECUTING 非法；
- COMPLETED → MODEL_GENERATING 非法；
- 已执行工具后的 final plain text 不得自动 nudge。

---

## PI-003 建立统一 Bridge Event Model

状态：DONE｜优先级：P0｜依赖：PI-001

定义内部事件：

text
GenerationStarted
AssistantTextDelta
AssistantTextFinal
ToolCall
ToolResult
GenerationFinished
GenerationFailed


### 实施

1. 新建 chatgpt_web/events.py。
2. 定义强类型事件。
3. 浏览器层只产生内部事件。
4. API 层只负责把事件转成 Chat / Responses 格式。

### 验收

同一个浏览器任务在 Chat Completions 与 Responses 中具有一致的内部事件序列。

## PI-004 Chat / Responses 协议 Adapter

状态：DONE｜优先级：P0｜依赖：PI-003

将 server.py、responses.py、streaming.py 的协议转换逻辑收敛到薄 adapter。

### 验收

- 非流式 Chat 行为不变；
- Chat SSE 行为不变；
- Responses 非流式行为不变；
- Responses streaming 行为不变；
- tool/function call 语义一致。

---

# 2. P0：DOM / Playwright 适配层

## PI-005 创建 DOM Adapter

状态：DONE｜优先级：P0｜依赖：无

新建 chatgpt_web/dom_adapter.py，统一提供：

text
find_input()
find_new_chat()
find_assistant_messages()
find_stop_button()
find_think_mode()
extract_latest_reply()


### 实施

1. 集中 selector 与 fallback。
2. 迁移 chat_io.py 输入框 / 回复节点逻辑。
3. 迁移 completion.py New Chat / Think Mode 逻辑。
4. 上层禁止直接依赖 selector 字符串。

### 验收

- selector 主要只出现在配置和 adapter；
- DOM diagnostics 仍可用；
- selector 相关现有测试全部通过。

## PI-006 DOM 改版黄金回归

状态：BLOCKED｜优先级：P0｜依赖：PI-005

增加真实网页检查：

1. New Chat；
2. Composer；
3. Think Mode；
4. Assistant reply；
5. generation / stop；
6. code block extraction。

### 验收

DOM 改版时，diagnostics 能指出具体失败层，而不是只能等待总超时。

当前进展：`tests/e2e/test_dom_probe.py` 已覆盖上述探测层，并能把 Cloudflare / profile 占用识别为明确环境阻塞；本机真实回归目前被 ChatGPT Cloudflare challenge 阻断，headed 模式另受 `user_data` 被现有 Chromium 实例占用影响，因此不能宣称真实 DOM 黄金回归已通过。

---

# 3. P1：Tool Runtime

## PI-007 ToolCall 强类型对象

状态：DONE｜优先级：P1｜依赖：PI-003

把 TOOL_CALL: {...} 解析结果统一转换为：

text
ToolCallRequest
 ├── id
 ├── name
 ├── arguments
 ├── source_span
 └── raw_text

已增加不可变 `ToolCallRequest` 及 `parse_tool_call_requests()` typed boundary；保留 `parse_tool_calls()` / dict facade 以兼容现有 Chat、Responses、streaming 和 edit_markdown 调用方，并让 `to_tool_call_models()` 同时支持 typed request。

## PI-008 ToolCall 标准流水线

状态：DONE｜优先级：P1｜依赖：PI-007

固定流程已建立：

text
parse → validate → normalize → deduplicate → policy check → execute → serialize result

实现了 typed `ToolCallRequest` 边界、阶段化异常（parse / validation / policy / execution / serialization）、工具参数 validator、调用 ID 去重、allow-list policy、注入式 executor 与 JSON serialization boundary；保留现有 `parse_tool_calls()` dict facade 以兼容旧调用方。

### 验收

- parse error、policy error、execution error、serialization error 均有明确独立异常类型；
- 同一 `tool_call_id` 在 pipeline 内只保留首次调用；
- tool allow-list 在执行前检查；
- executor 通过显式注入边界调用；
- 结果在返回前经过 JSON serialization 校验；
- `tests/test_toolcalls.py` 新增 pipeline 回归覆盖，现有 tool calling 测试全部通过。

## PI-009 Tool execution ledger

状态：TODO｜优先级：P1｜依赖：PI-008

记录：

text
(session_key, tool_call_id, tool_name, normalized_arguments)


并保存执行时间、成功状态、错误类型、结果 hash。

### 验收

- 相同 tool_call_id 不重复执行；
- 不同 session 不错误去重；
- 重复执行有结构化日志。

## PI-010 ToolPolicy

状态：TODO｜优先级：P1｜依赖：PI-008

定义：

text
allowed_tools
allowed_paths
write_enabled
network_enabled
max_output_chars
max_runtime_s
confirmation_policy


第一阶段只覆盖已有 edit_markdown；不立即增加 shell 执行能力。

## PI-011 edit_markdown 安全回归

状态：TODO｜优先级：P1｜依赖：PI-010

测试：绝对路径、..、root 外路径、symlink 越界、只读模式、write flag、backup 失败、超大文件。

### 验收

所有越界 / 未授权写入都稳定拒绝，不发生实际修改。

---

# 4. P1：异常治理

## PI-012 建立领域异常层

状态：TODO｜优先级：P1｜依赖：PI-005、PI-008

建议增加：

text
BrowserLookupError
BrowserInteractionError
ReplyExtractionError
ToolParseError
ToolExecutionError
SessionStateError
ConfigurationError


### 要求

- 关键路径减少 except Exception；
- Playwright 可预期错误与程序 bug 分开；
- fallback 必须记录结构化日志。

## PI-013 宽泛异常审计

状态：TODO｜优先级：P1｜依赖：PI-012

重点检查：

- chat_io.py；
- completion.py；
- server.py；
- responses.py；
- toolcalls.py；
- tasks.py；
- page_pool.py；
- session_store.py。

### 验收

每个剩余 except Exception 都要说明为什么必须 catch，以及失败后的可观测性。

---

# 5. P1：配置治理

## PI-014 配置分层

状态：TODO｜优先级：P1｜依赖：无

拆分：

text
ServerConfig
BrowserConfig
SessionConfig
CompletionConfig
ToolConfig
StorageConfig
DebugConfig


### 验收

- .env 保持兼容；
- 类型明确；
- 默认值 / 非法值 / 环境覆盖均有测试；
- 可输出当前有效配置摘要。

## PI-015 统一 RequestLimits

状态：TODO｜优先级：P2｜依赖：PI-014

集中管理：

text
max_request_bytes
max_prompt_chars
max_tool_result_chars
max_seed_chars
max_task_goal_chars
max_response_chars
max_code_block_chars


### 验收

Chat / Responses / task snapshot / tool result 使用统一限制策略。

---

# 6. P1：Session 与并发

## PI-016 Session schema versioning

状态：TODO｜优先级：P1｜依赖：PI-014 可并行

加入：

json
{
 "schema_version": 2,
 "session_key": "...",
 "turns": 10,
 "estimated_tokens": 12345,
 "cap_hit": false,
 "task_goal": "..."
}


实现 v1 → migrate_v2 → validate。

### 验收

旧状态文件可以安全启动；损坏状态进入明确错误路径。

## PI-017 Session 并发竞争测试

状态：TODO｜优先级：P1｜依赖：PI-016 可并行

测试矩阵：

text
same session + 2 requests
same session + 10 requests
2 sessions + 10 requests
session eviction during request
reset while request running
context rotation while second request arrives
browser restart while request waits


### 验收

不串会话、不串 tool result、不重复执行；lock timeout 返回 upstream_busy；eviction 不破坏运行中的请求。

## PI-018 Session 状态不变量

状态：TODO｜优先级：P1｜依赖：PI-017

验证：

- 不同 session 历史不污染；
- 同 session 连续轮次保持历史；
- session rotation 后目标仍存在；
- cap_hit 行为确定；
- reset 不造成重复执行。

---

# 7. P1：可观测性

## PI-019 health / readiness / diagnostics

状态：TODO｜优先级：P1｜依赖：PI-005

建议：

text
/healthz
/readiness
/diagnostics


Readiness 至少检查：

text
browser_ready
chatgpt_page_ready
authenticated
composer_ready
new_chat_ready


## PI-020 基础 metrics

状态：TODO｜优先级：P1｜依赖：PI-019

增加 counters：

text
request_total
request_success_total
request_error_total
request_timeout_total
request_retry_total
request_context_limit_total
session_recovery_total
session_rotation_total
browser_selector_miss_total
reply_extraction_failure_total
tool_call_total
tool_parse_failure_total
tool_execution_failure_total
tool_duplicate_total


并记录：

text
request_latency
browser_generation_latency
reply_extraction_latency
tool_execution_latency
session_recovery_latency


第一阶段不要求 Prometheus，可先做内存统计。

## PI-021 日志 correlation

状态：TODO｜优先级：P2｜依赖：PI-020

统一记录：

text
request_id
session_key
tool_call_id
attempt_id
page_id


### 验收

能够从入口日志追到 browser、tool、retry 和最终结果。

---

# 8. P1：测试体系

## PI-022 五个黄金 E2E

状态：TODO｜优先级：P1｜依赖：PI-005、PI-003

### E1 新会话

text
启动 → 登录态 → New Chat → Think mode → 输入 → 回复


### E2 普通回复

text
单轮纯文本 → 正确返回


### E3 单工具

text
TOOL_CALL → result → final


### E4 多工具

text
TOOL_CALL → result → TOOL_CALL → result → final


### E5 任务收尾

text
已经用过工具 → final plain text → 不再自动 nudge


E5 必须检查输入框最后一次被填充的内容，确保 bridge 没有追发第二条 prompt。

## PI-023 Property / invariant tests

状态：TODO｜优先级：P1｜依赖：PI-007、PI-016、PI-017

至少覆盖：

1. normalize(parse(x)) 对普通噪声不会崩溃；
2. 被接受的 edit path 永远位于 root；
3. session 历史不污染；
4. 自动 nudge 最多一次；
5. 执行阶段 final plain text 不 nudge；
6. 同一 tool_call_id 不重复执行。

## PI-024 API contract tests

状态：TODO｜优先级：P2｜依赖：PI-003、PI-004

### Chat Completions

- 非流式；
- SSE；
- role / content；
- tool_calls；
- finish_reason；
- usage；
- errors。

### Responses

- output_text；
- function_call；
- function_call_output；
- streaming events；
- errors。

这些测试不依赖真实 Playwright，应进入普通测试套件。

---

# 9. P2：模块拆分

## PI-025 拆分 toolcalls.py

状态：TODO｜优先级：P2｜依赖：PI-007、PI-008、PI-010

建议：

text
tool_schema.py
tool_parser.py
tool_runtime.py
tool_format.py


toolcalls.py 保留兼容 facade，逐步迁移调用方。

## PI-026 拆分 chat_io.py

状态：TODO｜优先级：P2｜依赖：PI-005、PI-012

建议：

text
browser_input.py
reply_extractor.py
reply_waiter.py


分别负责输入、回复提取、生成等待。

## PI-027 Mixin → Service Composition

状态：TODO｜优先级：P0/P1｜依赖：PI-005、PI-003

目标：

text
ChatGPTWebDriver
 ├── BrowserSessionManager
 ├── ChatIO
 ├── CompletionManager
 ├── SessionManager
 ├── PagePool
 └── ToolRuntime


### 实施要求

每次只迁移一个 Mixin，保留 facade，不做一次性大重构。

### 验收

- mypy 的 attr-defined override 明显减少；
- service 可以独立测试；
- 公共 driver API 保持兼容。

---

# 10. P2：重试、浏览器抽象与限制

## PI-028 RetryPolicy

状态：TODO｜优先级：P2｜依赖：PI-001、PI-012

统一：

text
max_attempts
retryable_errors
backoff
session_recovery_allowed
duplicate_request_policy


### 验收

Chat 与 Responses 对同一种失败拥有一致的 retry 语义。

## PI-029 Browser Backend Protocol

状态：TODO｜优先级：P2｜依赖：PI-005、PI-003

定义最小 backend：

python
class ChatBackend(Protocol):
 async def new_conversation(...): ...
 async def send(...): ...
 async def wait_response(...): ...
 async def get_response(...): ...


### 验收

核心业务逻辑可以通过 fake backend 测试，不依赖真实 Playwright。

## PI-030 RequestLimits 集中实施

状态：TODO｜优先级：P2｜依赖：PI-015

将所有入口统一接到集中 limiter，避免各模块自行截断导致语义差异。

---

# 11. P2：部署安全

## PI-031 Shared Mode

状态：TODO｜优先级：P2｜依赖：PI-010、PI-020

定义：

text
LOCAL_MODE
SHARED_MODE


### LOCAL_MODE

保持当前默认：loopback、无 API key、零配置。

### SHARED_MODE

启用：

- API key；
- rate limiting；
- request size limit；
- 更严格 ToolPolicy。

### 验收

默认配置行为不变；只有显式启用 shared mode 才增加这些约束。

---

# 12. P2：性能

## PI-032 Performance Baseline

状态：TODO｜优先级：P2｜依赖：PI-020

场景：

text
cold start
warm request
session rotation
large prompt
large tool result
large tool result + stream
10 concurrent sessions
same session contention


记录：

- 首 token 时间；
- 完整响应时间；
- prompt 字符数；
- tool result 字符数；
- browser memory；
- session bucket 数；
- page 数。

### 验收

形成可重复的 baseline 报告，后续重构有明确性能对比依据。

---

# 13. 推荐执行顺序

## Phase 1：状态与协议固化

text
PI-001 → PI-002
PI-003 → PI-004


### Exit Criteria

- 状态机测试通过；
- Chat / Responses 内部事件一致；
- “执行后不再 nudge”回归保持通过。

## Phase 2：DOM Adapter

text
PI-005 → PI-006


### Exit Criteria

- selector 集中；
- DOM 失败可诊断；
- E1 / E2 通过。

## Phase 3：Tool Runtime

text
PI-007
 ↓
PI-008
 ↓
PI-009 + PI-010
 ↓
PI-011


### Exit Criteria

- parse / validate / execute 分层；
- 重复执行受控；
- edit_markdown 安全不回退；
- E3 / E4 / E5 通过。

## Phase 4：Error / Session / Observability

text
PI-012 → PI-013
PI-014 → PI-016
PI-017 → PI-018
PI-019 → PI-021


### Exit Criteria

- 领域异常清晰；
- session schema 可迁移；
- 并发测试稳定；
- readiness / metrics 可用。

## Phase 5：结构化重构

text
PI-025
PI-026
PI-027
PI-028
PI-029


### Exit Criteria

- 公共 API 不破坏；
- mypy 豁免明显减少；
- 模块职责清晰；
- 每个拆分保持独立 commit 和回归测试。

## Phase 6：长期工程化

text
PI-023
PI-024
PI-030
PI-031
PI-032


最终建立：

text
unit tests
property / invariant tests
API contract tests
concurrency tests
E2E golden tests
performance baseline


---

# 14. 推荐 Git Commit 拆分

建议一个 commit 只表达一个主题：

text
feat: add explicit task state machine

test: add task state transition invariants

feat: introduce internal bridge event model

refactor: add Chat and Responses event adapters

refactor: isolate ChatGPT DOM adapter

test: add DOM regression coverage

feat: add typed tool call runtime

feat: add tool execution ledger and policy

test: harden tool security boundaries

refactor: introduce layered configuration

feat: add readiness and runtime metrics

test: add session concurrency coverage

refactor: split tool runtime from tool parsing

refactor: split browser input and reply extraction

refactor: replace driver mixins with service composition

test: add API contract and invariant coverage

perf: establish browser request baseline


---

# 15. 汇总任务表

| ID | 任务 | 优先级 | 依赖 | 状态 |
| --- | --- | --- | --- | --- |
| PI-001 | 统一任务状态机 | P0 | - | TODO |
| PI-002 | 状态机不变量测试 | P0 | PI-001 | TODO |
| PI-003 | Bridge Event Model | P0 | PI-001 | DONE |
| PI-004 | Chat / Responses Adapter | P0 | PI-003 | DONE |
| PI-005 | DOM Adapter | P0 | - | TODO |
| PI-006 | DOM 黄金回归 | P0 | PI-005 | TODO |
| PI-007 | ToolCall 强类型 | P1 | PI-003 | TODO |
| PI-008 | ToolCall pipeline | P1 | PI-007 | TODO |
| PI-009 | Execution ledger | P1 | PI-008 | TODO |
| PI-010 | ToolPolicy | P1 | PI-008 | TODO |
| PI-011 | edit_markdown 安全回归 | P1 | PI-010 | TODO |
| PI-012 | 领域异常层 | P1 | PI-005/008 | TODO |
| PI-013 | 宽泛异常审计 | P1 | PI-012 | TODO |
| PI-014 | 配置分层 | P1 | - | TODO |
| PI-015 | RequestLimits | P2 | PI-014 | TODO |
| PI-016 | Session schema versioning | P1 | PI-014 | TODO |
| PI-017 | Session 并发测试 | P1 | PI-016 | TODO |
| PI-018 | Session 不变量 | P1 | PI-017 | TODO |
| PI-019 | health / readiness / diagnostics | P1 | PI-005 | TODO |
| PI-020 | metrics | P1 | PI-019 | TODO |
| PI-021 | 日志 correlation | P2 | PI-020 | TODO |
| PI-022 | 五个黄金 E2E | P1 | PI-005/003 | TODO |
| PI-023 | 性质 / 不变量测试 | P1 | PI-007/016/017 | TODO |
| PI-024 | API contract tests | P2 | PI-003/004 | TODO |
| PI-025 | 拆分 toolcalls.py | P2 | PI-007/008/010 | TODO |
| PI-026 | 拆分 chat_io.py | P2 | PI-005/012 | TODO |
| PI-027 | Mixin → composition | P0/P1 | PI-005/003 | TODO |
| PI-028 | RetryPolicy | P2 | PI-001/012 | TODO |
| PI-029 | Browser Backend Protocol | P2 | PI-005/003 | TODO |
| PI-030 | 集中 RequestLimits 实施 | P2 | PI-015 | TODO |
| PI-031 | Shared Mode | P2 | PI-010/020 | TODO |
| PI-032 | Performance Baseline | P2 | PI-020 | TODO |

---

# 16. 长期共同验收原则

> Bridge 不应该创造新的用户意图。

因此所有后续实现必须遵守：

- 模型已经完成，bridge 不主动重新生成新的 prompt；
- 已执行工具后，final plain text 不自动解释为“必须继续调用工具”；
- retry 必须有明确的可重试错误和次数上限；
- session recovery 必须保证任务目标和上下文不丢失；
- tool execution 必须可追踪、可去重、可拒绝；
- DOM fallback 可以容错，但不能静默吞掉程序错误；
- API adapter 不能改变核心任务状态语义。

以上原则是 PI-001 ～ PI-032 的共同验收基线。
