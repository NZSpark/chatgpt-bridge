# ChatGPTBridge 项目分析与改进建议

> 审查基线：本地 main 分支当前工作区。
>
> 目标：在保持现有 OpenAI-compatible、Pi/Codex CLI 和 Playwright 桥接能力的前提下，提高安全性、稳定性、协议兼容性、可维护性和长期演进能力。

## 1. 总体判断

当前项目已经从简单的 Playwright 自动化脚本演进成一个小型 AI Gateway，主要由以下层组成：

text
OpenAI / Pi / Codex client
 |
 v
FastAPI compatibility layer
 /v1/chat/completions
 /v1/responses
 /v1/models
 |
 +-- session routing / task snapshot
 +-- prompt translation
 +-- simulated tool calling
 |
 v
ChatGPTWebDriver
 |
 +-- page pool / session locks
 +-- persistent browser profile
 +-- DOM polling / generation detection
 |
 v
ChatGPT Web UI


整体架构方向是合理的，API、prompt、toolcalls、responses、session/page pool、driver 已经基本分层；测试也已经覆盖配置、Markdown、状态、路由、流式输出、工具调用和部分 E2E 场景。

最大的长期风险并不在 FastAPI，而在于 Web UI 属于不稳定上游：DOM、selector、生成结束状态、浏览器 profile 和本地持久化状态都可能发生变化。因此后续重点应从“继续增加特殊情况”转向“建立可靠边界”。

适用范围判断：

- 适合个人本机、低并发、Pi/Codex bridge 使用。
- 不建议直接作为公网未认证服务。
- 不宜把当前的 OpenAI compatibility 理解为完整语义等价。

---

## 2. P0：安全边界必须先明确

### 2.1 非 loopback 暴露时不能只告警

当前默认监听 127.0.0.1，本机使用场景合理；但服务设计上没有统一 API authentication。若用户把 HOST 改成 0.0.0.0，就可能形成未认证的 ChatGPT 代理。

主要风险：

- 未授权客户端消耗 ChatGPT 会话和额度。
- 任意 session key 都可能造成 page/session churn。
- 本地文件编辑能力会扩大攻击面。
- debug/health 信息可能暴露内部运行状态。

建议：

1. 增加统一 Bearer token / API token 认证。
2. HOST 非 loopback 且未配置认证时，启动直接失败。
3. /healthz 分为 public readiness 与 authenticated detailed health。
4. /debug/* 默认只允许认证和/或 loopback。
5. 增加 request body size limit。
6. 增加基础 rate limit 和全局 concurrency limit。

### 2.2 本地工具必须有独立 policy boundary

edit_markdown 虽然不是任意 shell 执行，但本质仍是模型驱动的本地文件修改。建议至少采用：

text
READ_ONLY
DRY_RUN
WRITE_REQUIRES_CONFIRMATION
WRITE


并限制 ALLOWED_EDIT_ROOTS，使用 canonical path 检查，拒绝 .. traversal、symlink escape、.env、凭据目录和系统目录。

---

## 3. P0：配置系统需要 single source of truth

项目已有 pyproject.toml、.env.example 和 chatgpt_web/config.py，但当前配置维护成本已经偏高，selector 和运行参数容易出现漂移。

建议改成统一的 typed Settings：

text
Settings
 -> defaults
 -> environment/.env
 -> validation
 -> runtime


建议同时：

- 生成或自动校验 .env.example。
- 对 selector、timeouts、session limits 做启动校验。
- 启动时输出非敏感配置摘要。
- 密钥、token 等敏感值只显示 ***。
- 用测试保证 .env.example 与实际配置字段同步。

特别是当前 requires-python、README 环境说明以及本地 Python 运行环境应统一，避免文档和构建元数据互相矛盾。

---

## 4. P0/P1：session/task persistence 需要真正的并发语义

当前设计已经有 session bucket、task snapshot 和锁，但 JSON 文件状态在未来并发增加后会成为薄弱点。

潜在问题：

- 同时读写造成 lost update。
- task snapshot 写入顺序不稳定。
- persistence 异常如果被静默忽略，会产生“请求成功但状态丢失”。
- 多进程/多 worker 后 JSON 文件模型更脆弱。

短期建议：

text
temporary file
 -> flush
 -> fsync
 -> os.replace()


并对写操作增加进程级/线程级 lock，所有 persistence error 至少记录 warning/error。

中期建议直接使用 SQLite，保存：

text
sessions
session_tasks
session_events


这样可以同时解决 atomic update、locking、TTL cleanup 和后续 metrics 查询问题。

---

## 5. P1：task snapshot 的并发边界需要修正

请求在真正获得 session lock 之前就更新 task state 时，会产生典型 race condition：两个请求都读取旧版本，然后一个更新覆盖另一个更新。

建议：

1. tasks.record() 放到 session concurrency boundary 内；或改成 transaction/CAS。
2. 区分长期 goal 与短期 recent_context。
3. 不要每轮都把最后一条 user message 当作新的长期目标。
4. 对 task state 增加 TTL 和容量限制。

建议的数据结构：

python
TaskState(
 task_id=...,
 goal=...,
 status=...,
 created_at=...,
 updated_at=...,
 recent_context=...,
)


---

## 6. P1：并发模型应显式分成 session、page、global 三层

当前 page pool 和 bucket lock 已经是正确基础，但多个 page 并不代表 ChatGPT 上游可以无限并行。

建议明确：

text
GlobalSemaphore
 |
 +-- SessionLock[A]
 +-- SessionLock[B]
 +-- SessionLock[C]


增加：

- MAX_CONCURRENT_REQUESTS
- MAX_CONCURRENT_PAGES
- QUEUE_TIMEOUT
- REQUEST_QUEUE_SIZE
- 每个 session 的独占执行限制

超出容量时应明确返回 429 或 503 upstream_busy，而不是让请求继续排队到不可控的 timeout。

另外需要限制 session 创建速率，否则客户端可以通过制造大量 session key 影响 page pool 和持久化状态。

---

## 7. P1：Web UI driver 应升级为明确的 Adapter

当前项目把 selector 放在配置中是正确的，但业务逻辑仍然很容易逐渐依赖具体 DOM 结构。

建议定义稳定接口：

python
class ChatGPTWebAdapter:
 find_input()
 start_message()
 get_current_response()
 is_generating()
 is_ready()
 detect_context_limit()
 start_new_chat()


业务层只调用 adapter，不直接知道 CSS selector。

进一步可把 selector 集合版本化：

text
chatgpt_ui_profile = v1
chatgpt_ui_profile = v2


每个 profile 配套最小 smoke test。这样 ChatGPT UI 改版后可以快速判断是“adapter profile 失效”还是上层业务逻辑失效。

---

## 8. P1：生成结束检测应从纯 polling 向事件驱动演进

当前通过 polling、文本稳定、stop button、stall/timeout 等 heuristics 判断完成，已经比固定 sleep 更可靠，但仍然容易受到 DOM 改动影响。

推荐：

text
MutationObserver / page-side event
 |
 +-- response changed
 +-- generation state changed
 +-- node replaced
 |
 v
 backend timeout fallback


保留现有 polling 作为 fallback，而不是唯一机制。

---

## 9. P1：prompt 截断不能继续采用简单的字符串中间截取

对于自然语言，前后拼接尚可；对于 JSON、tool arguments、Markdown fence、patch、shell command 和代码，这种策略可能直接破坏语义。

建议按结构处理：

text
messages
 -> remove old low-priority context
 -> compact/summarize
 -> truncate large fields independently
 -> validate tool envelopes
 -> serialize


如果仍然超限，应返回明确的 context_length_exceeded，不要静默删除中间数据。

---

## 10. P1：工具调用应建立 lexical + schema + policy 三层验证

当前 TOOL_CALL: {...} 是很实用的 simulated tool calling，但它不是 native function calling，模型输出本身不能视为可信结构化数据。

建议分成三层：

text
Layer 1: lexical
 只接受规定位置的 TOOL_CALL:

Layer 2: schema
 JSON parse
 tool name validation
 arguments schema validation

Layer 3: policy
 tool allowed?
 current session allowed?
 current path allowed?


内部统一为：

python
ToolInvocation(
 id=...,
 name=...,
 arguments=...,
 source='model_text',
 validated=True,
)


这样未来接入真正的 native tool calling 时，上层接口不需要重写。

---

## 11. P1：OpenAI compatibility 需要显式矩阵

目前“OpenAI compatible”更准确地说是协议形状兼容，而不是完整语义兼容。尤其 extra="allow" 可以避免客户端因未知字段返回 422，但同时可能让用户误以为字段已经真正生效。

建议在文档中维护 compatibility matrix：

| Feature | Status |
|---|---|
| /v1/models | implemented |
| Chat Completions | implemented |
| Chat streaming | implemented |
| Responses | partial/implemented subset |
| Tool calling | simulated |
| temperature | accepted / ignored if not applied |
| top_p | accepted / ignored if not applied |
| Images/files | explicit unsupported status |
| Parallel tool calls | explicit compatibility status |

关键点：不要静默丢失用户认为重要的输入。 例如 Responses input 中不支持的 item type，应返回明确错误，而不是把它默默跳过。

---

## 12. P1：session recovery 应显式区分“继续”和“重建”

当前 Web conversation 失效时，通过 seed/task snapshot 重新建立上下文是合理的 fallback，但它不是等价恢复。

可能丢失：

- 原始 conversation 状态
- hidden UI state
- attachment context
- 原始工具执行上下文
- 不可见 metadata

建议定义：

text
Level 1: original conversation available
 -> continue

Level 2: conversation unavailable
 -> seed/task recovery


并在响应或 debug metadata 中记录 recovery_mode，方便诊断上下文是否发生过软重启。

---

## 13. P1：可观测性需要结构化

长期运行时，单纯 traceback/print 很难回答：哪个 session 失败、为什么 timeout、selector 是否失效、tool parse failure 有多少。

建议统一记录：

text
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


session key 不要原样打印，建议：

text
sha256(session_key)[:10]


同时增加至少这些 counters：

text
bridge_requests_total
bridge_request_errors_total
chatgpt_timeout_total
chatgpt_rotation_total
tool_call_parse_failures_total
session_bucket_count
page_pool_busy


不一定马上引入 Prometheus；先把日志字段和 counter 定义稳定下来更重要。

---

## 14. P1：测试应从函数测试扩展到协议 contract 测试

现有测试体系已经覆盖配置、Markdown、状态、路由、流式和 toolcalls，这一点很好。下一步最有价值的是完整链路测试，而不是继续堆更多字符串单测。

建议增加四层：

### A. Pure unit

配置、prompt、token estimation、session key、tool schema、Markdown safety。

### B. Protocol contract

固定请求验证：

text
POST /v1/chat/completions
POST /v1/responses
stream=false
stream=true


重点检查 OpenAI response shape、SSE 事件顺序、finish reason、tool call 拼装。

### C. Fake browser integration

建立 FakeChatGPTPage，模拟：

- typing
- generation
- timeout
- context limit
- DOM replacement
- selector failure
- stop button

这会比大量真实 E2E 更稳定、更快。

### D. Real browser smoke

保持目前 E2E 的真实浏览器测试，但定位为 optional/manual 或 nightly：

text
CHATGPT_SMOKE_TEST=1


联网测试只承担单测无法覆盖的真实 UI 验证。

---

## 15. P2：Prompt 层继续拆分

prompting.py 已经承担越来越多职责，包括消息提取、历史重建、工具指令、seed prompt 和 token estimation。

建议长期拆成：

text
message_normalizer.py
history_compactor.py
prompt_serializer.py
tool_protocol.py
token_estimator.py


内部统一 message representation，再分别生成：

text
DeltaPrompt
SeedPrompt
ToolPrompt
TaskRecoveryPrompt


这样未来更换模型、客户端或 Web adapter 时，边界更清晰。

---

## 16. P2：依赖与构建应可复现

当前项目使用版本范围而非严格锁定，这对于 Playwright + FastAPI + Pydantic 组合存在长期漂移风险。

建议采用以下任一种方案：

text
requirements.in + requirements.lock


或：

text
uv.lock


并让 CI 固定使用锁定版本。

同时统一：

- pyproject.toml 的 Python 要求
- README 安装要求
- 实际 CI 支持版本

不要让三者出现不同结论。

---

## 17. P2：文档需要从开发笔记升级为运维文档

建议至少补齐：

text
doc/architecture.md
doc/security.md
doc/compatibility.md
doc/operations.md
doc/troubleshooting.md


README 应明确写出：

> This bridge provides protocol compatibility for selected clients. It is not a semantic replacement for the OpenAI API.

同时清晰区分：

- supported
- partially supported
- accepted but ignored
- unsupported

这样能显著减少后续用户把“字段接受”误解成“功能实现”的问题。

---

## 18. P2：工程化基础设施

建议逐步补齐：

text
LICENSE
CONTRIBUTING.md
CHANGELOG.md
.github/workflows/ci.yml


最低 CI：

text
ruff
pytest
mypy/pyright
import smoke test


长期可增加：

- dependency vulnerability scan
- secret scan
- pip-audit
- CodeQL

---

## 19. 推荐实施顺序

### Phase 1：安全 + 状态一致性

1. 非 loopback + 无 auth 时禁止启动。
2. 加统一 API authentication。
3. 增加 body size / session rate / concurrency limits。
4. 修正 .env.example 与运行配置漂移。
5. persistence 改为 atomic write。
6. 修正 task snapshot concurrency boundary。
7. 清理 silent persistence failures。

### Phase 2：协议正确性

1. 建立 compatibility matrix。
2. 工具调用增加 schema + policy validation。
3. 明确 unsupported input 的错误行为。
4. 避免结构化内容被简单字符串截断。
5. 增加 Chat / Responses contract tests。

### Phase 3：Web driver 稳定性

1. 建立 ChatGPTWebAdapter。
2. selector/profile 版本化。
3. MutationObserver + polling fallback。
4. recovery mode 可观测化。
5. fake browser integration test。

### Phase 4：工程化

1. 锁定依赖。
2. 完善 CI。
3. 完善安全、运维、兼容性文档。
4. 再考虑 SQLite、metrics 和更复杂的 session lifecycle。

---

## 20. 最终建议

当前项目已经具备继续发展的结构，不建议推倒重写。最值得做的不是继续增加更多 selector、retry 或 parser 特例，而是优先建立三个边界：

text
1. API boundary
 auth / limits / compatibility

2. State boundary
 session / task / persistence / concurrency

3. Web boundary
 ChatGPTWebAdapter / DOM / recovery


这三个边界稳定以后，再扩展 Responses、tool calling、更多客户端兼容能力，维护成本会明显下降。
