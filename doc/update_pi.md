# 项目分析与改进建议

> 分析日期：2026-10-06
>
> 分析对象：当前 ChatGPTBridge 工作区（main 分支，工作区在分析开始时干净）。
>
> 分析重点：架构、可靠性、工具调用、会话生命周期、网页 DOM 依赖、API 兼容性、测试、可观测性、安全与长期维护成本。

## 1. 当前项目总体评价

项目目前已经从“能把 ChatGPT 网页包装成 API”发展到一个具有较完整工程化能力的本地桥接层：包含 Chat Completions、Responses API、流式传输、会话分桶、上下文轮转、任务快照、工具调用解析、本地 edit_markdown、DOM 诊断以及大量单元测试。

最近两次提交已经解决了一个很关键的边界问题：工具任务已经执行过以后，模型用普通文本收尾时，bridge 不再自行追发纠偏 prompt。这一行为应该继续保持为核心协议语义。

当前最大的风险已经不是“功能太少”，而是系统复杂度正在集中到少数几个高复杂度模块，同时底层依赖 ChatGPT 网页 DOM，导致维护成本和线上回归风险较高。

建议未来工作目标从“继续堆功能”转向：

1. 明确并固化请求 → 浏览器交互 → 工具事件 → 响应的状态机；
2. 降低 Mixin 与超大模块的耦合；
3. 把工具调用、任务完成、重试、会话轮转等隐式状态改成显式状态；
4. 提高真实网页 E2E 的稳定性与诊断能力；
5. 减少宽泛异常捕获导致的静默失败；
6. 逐步建立性能、可靠性和兼容性的长期回归基线。

---

## 2. P0：优先改进的架构问题

### 2.1 建立统一的“任务状态机”

目前“是否已调用工具”“是否正在生成”“是否应该继续等待”“是否应该重试”“是否应该结束”分散在多个模块中：

- chat_io.py 处理网页输入、等待、结束检测和纠偏；
- prompting.py 判断历史中是否出现工具；
- toolcalls.py 负责解析和工具执行；
- completion.py 负责会话恢复 / 轮转；
- streaming.py 与 responses.py 又分别处理不同 API 的输出语义。

这已经导致近期需要专门修复“任务已经结束仍追发 prompt”的状态歧义。

建议引入明确的内部状态，例如：

text
RECEIVED
 ↓
PROMPT_BUILT
 ↓
MODEL_GENERATING
 ├─ TOOL_CALL_DETECTED
 │ ↓
 │ TOOL_EXECUTING
 │ ↓
 │ TOOL_RESULT_RETURNED
 │ └──────────────→ MODEL_GENERATING
 │
 └─ PLAIN_TEXT_FINAL
 ↓
 COMPLETED


并增加异常状态：

text
FAILED
TIMEOUT
CONTEXT_LIMIT
UPSTREAM_BUSY
SESSION_RECOVERY


这样“有没有工具调用”和“这次是不是最终文本”不再依靠布尔条件拼接推断，而是由任务状态机决定。

优先级：P0。

---

### 2.2 统一 Chat Completions 与 Responses 的内部事件模型

当前两个入口虽然共用浏览器驱动，但仍存在明显的协议层分叉：

- server.py 处理 Chat Completions；
- responses.py 负责 Responses 转换；
- streaming.py 负责 SSE；
- 工具调用又要在不同 OpenAI 格式之间转换。

长期看容易出现一种问题：同一个浏览器事件在两个入口得到不同处理结果。

建议内部统一成一种 bridge event，例如：

text
AssistantText(delta)
AssistantText(final)
ToolCall(name, arguments, call_id)
ToolResult(call_id, content)
GenerationStarted
GenerationFinished
GenerationFailed


然后做两个非常薄的适配器：

text
Bridge Events
 ├── Chat Completions Adapter
 └── Responses Adapter


这样浏览器层和 API 协议层彻底解耦。

优先级：P0。

---

### 2.3 尽量减少 Mixin 架构

目前 ChatGPTWebDriver 通过多个 Mixin 拼装能力，例如 completion / chat I/O / page pool / session store 等。这种方式短期方便，但从类型检查和依赖关系来看存在明显成本：当前 pyproject.toml 中已经需要针对多个 Mixin 模块关闭 attr-defined。

这说明静态类型系统无法完整理解真实对象模型。

建议逐步从：

text
ChatGPTWebDriver + 多个 Mixin


迁移为：

text
ChatGPTWebDriver
 ├── BrowserSessionManager
 ├── ChatIO
 ├── CompletionManager
 ├── SessionManager
 ├── PagePool
 └── ToolRuntime


通过显式对象组合代替隐式宿主属性。

这样可以：

- 删除部分 mypy 豁免；
- 降低模块间隐藏依赖；
- 更容易做单元测试和依赖注入；
- 后续替换 Playwright 层更简单。

不建议一次性重构；应该采用“一个 Mixin → 一个 service object”的渐进方式。

优先级：P0/P1。

---

## 3. P0：网页 DOM 依赖是最大外部风险

项目的核心不稳定因素仍然是 ChatGPT 网页本身发生改版。

目前已经有：

- RESPONSE_SELECTORS；
- INPUT_SELECTORS；
- READY_SELECTOR；
- NEW_CHAT_SELECTOR；
- Think 模式 fallback；
- DOM debug endpoint；
- selector 命中率诊断；
- end detection。

这是正确方向，但仍建议进一步把 DOM 适配层独立成一个模块，例如：

text
chatgpt_web/dom_adapter.py


统一提供：

text
find_input()
find_new_chat()
find_assistant_messages()
find_stop_button()
find_think_mode()
extract_latest_reply()


上层代码不应直接知道 selector 字符串。

目前 chat_io.py 和 completion.py 都包含大量 selector / Playwright 容错逻辑，因此网页改版时修改面较大。

优先级：P0。

---

## 4. P1：减少 except Exception，避免“假成功”

当前代码中 server.py、completion.py、chat_io.py、toolcalls.py、tasks.py 等存在较多宽泛的：

python
except Exception:


这种方式对于 Playwright 的脆弱网页环境有一定现实意义，但过多使用会产生两个问题：

1. 真正的程序错误被当成“网页没找到”处理；
2. 错误被吞掉以后，最终只表现为 timeout / empty reply，定位非常困难。

建议把异常按领域分组：

text
BrowserLookupError
BrowserInteractionError
ReplyExtractionError
ToolParseError
ToolExecutionError
SessionStateError
ConfigurationError


然后只在确实需要降级的位置 catch 它们。

对于兜底分支可以继续保留 Exception，但必须：

- logger.debug/exception() 留下完整 traceback；
- 统计发生次数；
- 不把“程序 bug”伪装成“正常降级”。

优先级：P1。

---

## 5. P1：把工具调用从“文本协议”逐步升级为内部结构化协议

当前工具调用的核心契约是：

text
TOOL_CALL: {...}


这对于网页端确实实用，因为网页模型本身并不能直接执行本地工具。

但这种协议有几个天然风险：

- 模型可能在前后增加解释文字；
- JSON 可能不完整；
- shell 命令中的引号、换行、控制字符需要容错；
- Markdown code block 与工具指令可能混在一起；
- 同一响应可能出现多个工具调用；
- 模型可能重复调用完全相同的工具。

建议保留当前 TOOL_CALL: 作为“外部模型协议”，但内部立即转换为强类型对象：

python
ToolCallRequest(
 id=...,
 name=...,
 arguments=...,
 source_span=...,
)


解析之后，后续代码禁止再操作原始字符串。

另外增加一个显式的：

text
parse → validate → normalize → deduplicate → execute


流水线。

这样可以把“解析错误”和“执行错误”严格区分。

优先级：P1。

---

## 6. P1：工具执行应增加幂等与去重层

近期项目已经处理了“空工具输出 → 重复发同一命令”的问题，因此下一步建议把重复调用治理从 prompt 层进一步下沉到工具 runtime。

建议维护一个短期 execution ledger：

text
(session_key, tool_call_id, tool_name, normalized_arguments)


至少记录：

- 是否执行过；
- 执行时间；
- 是否成功；
- 结果摘要 hash。

遇到相同 tool_call_id 时直接拒绝重复执行；遇到完全相同的 normalized command 时可按策略阻止高频重复调用。

这样即使模型、客户端或网络层重复提交，也不会再次执行危险操作。

优先级：P1。

---

## 7. P1：工具安全边界继续收紧

edit_markdown 已经有项目根目录沙箱、拒绝绝对路径、拒绝 .. 等边界，这一点是正确的。

但从整体架构看，未来工具数量一旦增加，不能只依赖每个工具自行做安全检查。

建议建立统一 ToolPolicy：

text
ToolPolicy
 ├── allowed_tools
 ├── allowed_paths
 ├── write_enabled
 ├── network_enabled
 ├── max_output_chars
 ├── max_runtime_s
 └── confirmation_policy


特别是 shell 类工具，要区分：

text
read-only command
safe mutation
high-risk mutation


以后如果添加 bash / python / git push 一类工具，建议默认 deny，需要显式开启。

优先级：P1。

---

## 8. P1：配置项已经较多，需要配置分层

当前 .env 里已经包含：

- 服务配置；
- 会话配置；
- end detection；
- tool nudge；
- Responses API；
- task snapshot；
- edit_markdown；
- DOM selectors；
- output retention。

配置数量继续增长以后，.env.example 会成为第二套“配置 API”，维护成本会越来越高。

建议把配置逻辑分成几个 dataclass：

text
ServerConfig
BrowserConfig
SessionConfig
CompletionConfig
ToolConfig
StorageConfig
DebugConfig


外部 .env 仍保持兼容，但 config.py 最终输出一个不可变总配置对象。

好处：

- 参数更容易测试；
- 类型更清晰；
- 避免模块直接读取几十个全局变量；
- 能更容易打印“当前有效配置摘要”。

优先级：P1。

---

## 9. P1：增加真正的“可观测性指标”

目前日志和 debug endpoint 已经不少，但主要仍是文本日志。

建议增加结构化指标，至少记录：

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


并记录耗时：

text
request_latency
browser_generation_latency
reply_extraction_latency
tool_execution_latency
session_recovery_latency


可以先不引入 Prometheus，直接做内存 counters + /healthz / /metrics JSON 即可。

优先级：P1。

---

## 10. P1：健康检查需要区分“进程活着”和“真正可用”

目前 /healthz 已经返回 browser/session/cluster 信息，这是很好的基础。

建议进一步区分：

text
process_ready
browser_ready
chatgpt_page_ready
chatgpt_authenticated
composer_ready
new_chat_ready
tool_runtime_ready


例如浏览器进程存在，但 ChatGPT 被 Cloudflare challenge 阻塞时，不应简单视为“browser_ready = healthy”。

可以定义：

text
/healthz
 → process / lightweight health

/readiness
 → 页面、登录态、composer 都可用

/diagnostics
 → selector / session / task / browser 详细信息


优先级：P1。

---

## 11. P1：会话状态建议采用显式版本化 schema

目前 session/task 状态已经持久化，未来字段继续增加时容易遇到旧状态文件兼容问题。

建议状态 JSON 包含：

json
{
 "schema_version": 2,
 "session_key": "...",
 "turns": 10,
 "estimated_tokens": 12345,
 "cap_hit": false,
 "task_goal": "..."
}


加载时执行：

text
v1 → migrate_v2 → validate


而不是直接读取字典字段。

优先级：P1。

---

## 12. P1：补充并行请求的竞争测试

项目已经有 bucket lock、LRU、session scoping、parallel buckets 等机制，但这些特性最容易在真实并发下出错。

建议增加测试矩阵：

text
same session + 2 requests
same session + 10 requests
2 sessions + 10 requests
session eviction during request
reset while request running
context rotation while second request arrives
browser restart while request is waiting


尤其应该断言：

- 不会串会话；
- 不会把一个请求的 tool result 注入另一个请求；
- 不会重复执行同一工具；
- lock timeout 能稳定返回 upstream_busy；
- session eviction 不会破坏正在执行的请求。

优先级：P1。

---

## 13. P1：建立真实网页 E2E 的“最小黄金场景”

目前大量单测已经覆盖逻辑，但项目真正的高风险仍在真实 ChatGPT DOM，因此建议不要把 E2E 全部视为重量级测试。

可以定义 5 个最小黄金场景：

### E1 新会话

text
启动 → 登录态有效 → New Chat → Think 模式 → 输入 → 回复


### E2 普通回复

text
用户问题 → 单轮纯文本 → 正确返回


### E3 单工具调用

text
模型 → TOOL_CALL → tool result → 模型最终文本


### E4 多工具任务

text
TOOL_CALL → result → TOOL_CALL → result → final


### E5 任务收尾

text
已经调用工具 → 最终纯文本


E5 对当前项目尤其重要，因为这正是最近修复的回归点。

优先级：P1。

---

## 14. P1：测试数量很多，但建议增加“性质测试”

当前已有大量单元测试，说明项目测试意识很强。

下一步不应只是继续添加几十个例子，而是测试不变量。

例如 toolcalls.py 可以增加 property-based tests：

### 解析器不变量

text
normalize(parse(x)) 不应该抛异常


### 路径安全不变量

text
任何被接受的 edit_markdown path 都必须位于 root 内


### 会话隔离不变量

text
不同 session key 的历史不能互相污染


### 重试不变量

text
最多只能有一次自动 nudge


### 结束不变量

text
任务已经进入执行阶段后，final plain text 不得触发自动 nudge


这样比继续堆具体例子更能防止未来重构破坏核心语义。

优先级：P1。

---

## 15. P2：进一步降低 toolcalls.py 与 chat_io.py 的复杂度

从当前代码规模和职责来看，toolcalls.py、chat_io.py 已经明显偏大。

建议拆分：

### toolcalls.py

text
tool_schema.py
 schema / names / validation

tool_parser.py
 text → ToolCallRequest

tool_runtime.py
 execution / timeout / dedupe / policy

tool_format.py
 Chat / Responses serialization


### chat_io.py

text
browser_input.py
 fill / clear / submit

reply_extractor.py
 DOM → plain text / code blocks

reply_waiter.py
 generating / quiet window / end detection


拆分以后测试会明显更容易。

优先级：P2。

---

## 16. P2：重试策略需要统一抽象

目前 retry/recovery 行为分散在 driver / chat_io / completion / responses 等位置。

建议统一成：

text
RetryPolicy
 ├── max_attempts
 ├── retryable_errors
 ├── backoff
 ├── session_recovery_allowed
 └── duplicate_request_policy


这样能够避免：

- 某个入口重试 2 次；
- 另一个入口重试 3 次；
- 某种错误在 chat 路径可恢复，但 Responses 路径直接失败。

优先级：P2。

---

## 17. P2：考虑增加 API contract tests

项目声称兼容 OpenAI API，因此建议添加一组独立于 Playwright 的 contract tests，固定验证：

### Chat Completions

- 非流式；
- SSE；
- role / content；
- tool_calls；
- finish_reason；
- errors；
- usage。

### Responses

- output_text；
- function_call；
- function_call_output；
- streaming events；
- errors。

这些测试不需要启动浏览器，只验证协议层输出，因此速度快、适合每次 commit 和 CI 执行。

优先级：P2。

---

## 18. P2：增加性能基线

目前项目更关注功能正确性，建议开始建立最小性能指标：

text
cold start
warm request
session rotation
large prompt
large tool result
large tool result + stream
10 concurrent sessions
same session contention


至少记录：

- 首 token 时间；
- 完整响应时间；
- prompt 字符数；
- tool result 字符数；
- 浏览器内存；
- session bucket 数量；
- 页面数量。

由于这是网页自动化桥，真正的瓶颈大概率不是 FastAPI，而是 Playwright + ChatGPT 页面等待，因此性能分析应该优先围绕 browser wait time，而不是只看 API handler 时间。

优先级：P2。

---

## 19. P2：加强日志中的 request / session / tool correlation

项目已经有 request ID，这是很好的基础。

建议让下面几个 ID 全部出现：

text
request_id
session_key
tool_call_id
attempt_id
page_id


这样面对类似：

text
请求 A → session X → tool 1 → retry → session recovery


的问题时，可以通过一条 log query 完整追踪。

优先级：P2。

---

## 20. P2：安全策略可以进一步从“提醒”升级为“部署模式”

当前设计默认只监听 loopback，并且当 HOST 非 loopback 时告警，这符合零配置定位。

长期可以增加两个明确模式：

text
LOCAL_MODE
 HOST=127.0.0.1
 no auth

SHARED_MODE
 explicit API key required
 rate limiting
 request size limit
 tool policy locked down


这样即便未来有人把 bridge 放到局域网，也不会完全依赖 README 提醒。

可以保持默认零配置，不破坏当前使用方式。

优先级：P2。

---

## 21. P2：限制请求体和工具结果大小的策略应集中

项目已经存在：

- PROMPT_MAX_CHARS；
- TOOL_RESULT_MAX_CHARS；
- SEED_MAX_CHARS；
- TASK_GOAL_MAX_CHARS。

这说明大输入已经是一个明确风险。

建议进一步统一成：

text
RequestLimits
 ├── max_request_bytes
 ├── max_prompt_chars
 ├── max_tool_result_chars
 ├── max_seed_chars
 ├── max_task_goal_chars
 ├── max_response_chars
 └── max_code_block_chars


所有入口统一调用同一个 limiter，避免 Chat / Responses / tool runtime 各自截断导致语义差异。

优先级：P2。

---

## 22. P2：考虑浏览器层抽象，降低未来替换成本

目前项目已经明显形成：

text
OpenAI API
 ↓
Bridge
 ↓
Playwright
 ↓
ChatGPT Web


未来如果 ChatGPT Web 变得无法稳定自动化，当前所有上层业务都会同时受影响。

建议在 driver 上方定义最小浏览器抽象：

python
class ChatBackend(Protocol):
 async def new_conversation(...)
 async def send(...)
 async def wait_response(...)
 async def get_response(...)


当前 Playwright 是唯一实现，但这样未来可以测试一个 fake backend，也可以逐步加入其他后端。

优先级：P2。

---

## 23. 对当前项目最值得优先执行的 10 项工作

按收益 / 风险比排序：

1. P0：建立统一任务状态机。
2. P0：统一 Chat / Responses 的内部 event model。
3. P0：把 DOM selector 与 Playwright 操作抽成独立 adapter。
4. P1：把 Mixin 逐步迁移为显式 service composition。
5. P1：工具 runtime 增加 execution ledger / dedupe。
6. P1：减少关键路径上的 except Exception。
7. P1：增加 5 个最小真实网页黄金 E2E。
8. P1：增加并发、session isolation、reset/eviction 测试。
9. P1：建立 metrics / request correlation。
10. P2：统一配置、retry、request limits，并拆分超大模块。

---

## 24. 推荐的重构顺序

不要同时做大规模重构，建议分 5 个阶段：

### Phase 1：状态和协议固化

- 引入内部事件模型；
- 引入任务状态枚举；
- 保留现有 API；
- 把最近已经修复的“执行后不再 nudge”写成核心不变量测试。

### Phase 2：工具 runtime

- parse → validate → normalize → dedupe → execute；
- ToolPolicy；
- execution ledger；
- tool timeout / output limit。

### Phase 3：DOM adapter

- selector 集中化；
- input / new chat / reply / stop / think mode 统一入口；
- DOM 变化只影响 adapter。

### Phase 4：Driver composition

- Mixin → service composition；
- 收紧 mypy；
- 删除不必要的 attr-defined override。

### Phase 5：工程化

- metrics；
- contract tests；
- concurrency tests；
- E2E golden suite；
- performance baseline；
- CI。

---

## 25. 最终判断

当前项目已经具备继续作为个人 / 本地 AI coding agent bridge 使用的工程基础，最近的工具调用收尾修复也解决了一个真实且严重的行为问题。

下一阶段最大的价值不是继续添加更多功能，而是降低状态复杂度和网页依赖带来的不可预测性。

尤其要记住一个核心设计原则：

> bridge 的职责是准确转发、等待、解析和执行，不应该自行创造新的用户意图。

这条原则应该继续贯穿：

- tool nudge；
- retry；
- session recovery；
- task snapshot；
- response completion；
- future agent/tool orchestration。

只要 bridge 不主动把“已经完成的事情”重新变成“新的命令”，并且所有重试、工具执行、会话轮转都有明确状态和可追踪 ID，后续扩展的风险会显著下降。
