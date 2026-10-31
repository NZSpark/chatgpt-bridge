# 多会话桶（Multi-chat / Multi-bucket）设计方案

目标：让多个 AI 编程客户端、Agent 或独立任务共享一个 ChatGPT 网页登录态，同时保持各自的对话上下文隔离。

本文参考 `DeepseekBridge/doc/multiple_chat.md` 的设计思路，并以本项目 `chatgpt_web/` 的实际代码为依据。本文区分**当前已经实现的能力**和**建议增强的能力**，不把参考项目的实现误写成本项目现状。

## 1. 目标与基本概念

多个客户端或任务共用同一条网页对话，会造成上下文污染。例如，同一 Pi 实例在两个仓库执行任务、Cline 和 Cursor 共用服务、同一客户端运行多个 Agent，都可能让不同任务的 prompt 进入同一条对话。

本方案中的“桶”（bucket）是桥接层的上下文隔离单位：一个稳定的桶键对应一份独立会话状态、一个逻辑网页对话上下文，以及需要时分配的一条独立 Playwright Page。

必须区分以下概念：

| 概念 | 含义 | 生命周期 |
| --- | --- | --- |
| Bucket / session key | 客户端任务的稳定标识字符串 | 不因页面关闭而改变 |
| SessionState | 轮数、预算、轮转标记、绑定设置等状态 | 可持久化，页面关闭后仍保留 |
| Page | Playwright 标签页对象 | 可创建、重建、回收或 LRU 淘汰 |
| Web conversation | ChatGPT 网页中的一条对话 | 由网页站点管理 |
| Browser context | 持有登录态、Cookie 和持久化 profile 的 context | 整个 Driver 生命周期共享 |

**分桶不等于并发。** 分桶负责上下文隔离；是否允许多个桶同时驱动网页，由 `PARALLEL_BUCKETS` 决定。默认应保持串行，以降低网页风控和资源竞争风险。

## 2. 本项目当前实现

本项目已经实现多桶的核心能力，应在现有架构上演进，不应照搬参考项目重新建立一套 Driver。

### 2.1 组件与职责

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| HTTP 路由和桶解析 | `chatgpt_web/server.py` | `_session_key()`、Chat Completions / Responses 路由 |
| 浏览器生命周期 | `chatgpt_web/browser/driver.py` | Playwright 启动、持久化 context 和关闭 |
| Driver 组装 | `chatgpt_web/driver.py` | 组合各 mixin，维护桶级容器 |
| 页面池和锁 | `chatgpt_web/page_pool.py` | 建页、判活、回收、LRU、按桶锁和活跃登记 |
| 会话状态 | `chatgpt_web/session/schema.py`、`session/store.py` | `SessionState`、schema、加载、保存、轮转和绑定 |
| 发送与播种 | `chatgpt_web/chat_io.py` | 发送、轮询、重试、历史播种和状态更新 |
| 任务快照 | `chatgpt_web/tasks.py` | 按桶保留任务目标和近期消息，供轮转续接 |
| 桥内命令 | `chatgpt_web/bridge_commands.py` | 查询和管理当前桶 |
| Chat API | `chatgpt_web/api/chat_adapter.py` | Chat Completions 的非流式执行 |
| Responses API | `chatgpt_web/api/responses_adapter.py` | Responses 转换、执行和 SSE |

Driver 当前主要桶级容器：

```python
self.page                          # 默认桶 Page，保留旧接口
self.lock                          # 默认锁；串行模式下所有桶共用
self._page_lock                    # 保护建页、回收和淘汰
self._sessions                     # bucket -> SessionState
self._pages                        # bucket -> Page（默认桶除外）
self._page_ids                     # bucket -> 页面诊断 ID
self._page_last_used                # bucket -> monotonic timestamp
self._locks                        # bucket -> asyncio.Lock
self._active_counts                 # bucket -> 可重入活跃计数
self._active_buckets                # 活跃桶集合视图
self._last_prompts                  # bucket -> 最近实际发送的 prompt
self._last_speeds                   # bucket -> 最近速度样本
self._speed_samples_by_request_id   # request_id -> 请求级速度记录
```

默认桶继续使用 `self.page` 和 `self.lock`，以兼容旧调用和测试；非默认桶使用 `_pages` 和按需创建的桶级锁。

### 2.2 当前桶键解析

当前 `server.py::_session_key()` 的优先级为：

1. 配置指定的请求头，默认 `X-ChatGPT-Session`；
2. OpenAI 请求体的 `user` 字段；
3. 当 `SESSION_SCOPING_BY_CWD=true` 时，按受控规则提取工作目录并生成 cwd 桶；
4. 当 `SESSION_SCOPING_BY_UA=true` 时，按 User-Agent 识别客户端；
5. 都没有则返回 `None`，由 Driver 使用默认桶。

cwd 和 UA 自动分桶均为显式标识的兜底，不覆盖 header 或 user。cwd 默认关闭。

当前 `_client_from_ua()` 识别 `roo-code`、`windsurf`、`cline`、`cursor`、`aider`、`copilot`、`continue`、`antigravity`、`openai`、`anthropic` 等常见客户端。已知名称使用词边界匹配；未命中时尝试提取 UA 第一个 product token。自动生成的键使用 `ua:` 前缀。

桶键会进行字符白名单替换并按 `SESSION_KEY_MAX_LEN` 截断。该处理可限制非法字符和长度，但简单截断存在不同原始键碰撞的可能，后续应增加碰撞测试或更严格的唯一性策略。

**cwd 自动分桶代码已实现，但默认关闭。** 开启 `SESSION_SCOPING_BY_CWD` 后，同一客户端在不同工作目录运行任务时，可在缺少显式 header/user 的情况下按工作目录生成独立桶。真实客户端是否发送受支持的环境元数据格式，仍需按 M2.6 进行验证。

### 2.3 状态持久化

当前状态 schema version 为 2，定义在 `chatgpt_web/session/schema.py`。`SessionState` 保存控制下一轮行为所需的元数据，不保存完整聊天正文。主要字段包括：

- `has_history`、`turns`、`est_tokens`：历史状态和预算；
- `cap_hit`、`cap_failures`：会话到顶情况；
- `pending_rotation`、`pending_reseed`：下一轮轮转或重播标记；
- `linked_url`：用户显式绑定的网页会话 URL；
- `think_mode`、`save_files`：桶级偏好；
- `speed_*`：速度统计的数值聚合；
- `last_error`、`updated_at`：诊断元数据。

状态文件默认是 `./user_data/.chatgpt_state`。默认桶状态兼容地保存在顶层；其它桶存入 `sessions[bucket]`。保存过程通过状态文件锁保护 read-modify-write，并使用临时文件和原子替换。

重要区别：普通网页会话 URL **不会被当作可靠的自动恢复依据持久化**。页面重建、应用重启或会话轮转时，本项目通常新建一条网页会话，再用客户端提供的完整历史播种。只有用户通过 `/link` 明确绑定的 `linked_url` 是例外。

### 2.4 页面池和生命周期

- 一个 Driver 使用一个持久化 browser context，所有桶共享同一登录态。
- 默认桶使用 `driver.page`；其它桶使用 `driver._pages[bucket]`。
- `_ensure_page()` 按需建页，并在 `_page_lock` 内再次检查，避免并发重复建页。
- 新页面通常导航到 ChatGPT 首页并新建对话，设置页面就绪状态和 Think 模式。
- 如果桶配置了 `linked_url`，则导航到绑定的会话。
- 页面失效时从页面池移除，下次请求重新创建。
- `BUCKET_IDLE_TTL_S` 控制空闲页面回收；达到页面上限时通过 LRU 淘汰非活跃页面。
- 页面回收只关闭 Page，不应删除 SessionState、磁盘状态或任务快照。

`MAX_SESSION_BUCKETS` 限制的是**非默认桶的打开页面数**，不是磁盘上可保存的桶数量。默认桶不计入上限。

### 2.5 锁和活跃请求

`PARALLEL_BUCKETS=false` 时，所有桶共享 `driver.lock`，上下文隔离但网页操作串行。开启后，非默认桶使用各自 `_locks[bucket]`，不同桶可并行，同一个桶仍互斥；默认桶始终使用全局锁。

`send_chat()` 从请求开始就登记活跃桶，`_session_lock()` 持锁阶段也登记。使用可重入计数而非简单集合，确保内层释放锁时不会提前清除外层请求的保护。页面回收和 LRU 淘汰必须跳过活跃桶。

`BUCKET_LOCK_TIMEOUT_S` 控制等待桶锁的最长时间；Responses SSE 路径的排队或快速失败还受 `BUCKET_LOCK_QUEUE` 影响。

### 2.6 桶级副作用

桶键不只用于 Driver。它还必须贯穿：

- `tasks.record()` / `tasks.resume_block()` 任务快照；
- Chat Completions 和 Responses 生成路径；
- `/bridge` 当前桶命令；
- 本地工具执行和工具 ledger；
- 最近实际发送的 prompt；
- 速度统计和请求级关联；
- 日志上下文及健康检查诊断。

Chat Completions 路由当前会记录任务快照。Responses 路由当前解析桶键并交给 `handle_responses()`；应在后续一致性测试中明确其任务快照行为，不应假定两条协议路径的所有旁路功能天然相同。

## 3. 目标架构和隔离边界

### 3.1 请求流

```text
HTTP 请求
  |
  +-- server._session_key(request, header, user_agent)
  |      +-- 稳定 bucket key；None 表示默认桶
  |
  +-- 设置 request_id 和日志 session_key
  |
  +-- Chat Completions
  |      +-- tasks.record / tasks.resume_block
  |      +-- prompt / seeded prompt
  |      +-- chat_adapter 或 streaming_adapter
  |
  +-- Responses API
  |      +-- Responses -> Chat 请求转换
  |      +-- 保留相同 bucket key
  |      +-- responses_adapter
  |
  +-- handle_command(messages, driver, bucket)
  |      +-- 只操作当前桶
  |
  +-- driver.send_chat(..., key=bucket)
         +-- mark bucket active
         +-- ensure page
         +-- load SessionState(bucket)
         +-- acquire bucket lock
         +-- 选择增量 prompt 或 seeded prompt
         +-- 操作该桶 Page
         +-- 更新桶状态、prompt 和 speed
         +-- unmark bucket active
```

**桶键必须在 HTTP 入口确定后贯穿整个请求生命周期。** 中间层不得重新按 UA、消息正文或随机值推断桶键。

### 3.2 隔离矩阵

| 资源/数据 | 同一桶 | 不同桶 |
| --- | --- | --- |
| SessionState | 共享 | 隔离 |
| Page | 复用该桶自己的 Page | 独立 Page |
| 桶锁 | 同一把锁 | 并行模式下不同锁；串行模式下共用全局锁 |
| 任务快照 | 同一份 | 独立文件 |
| rotation / reseed | 共享当前桶标记 | 隔离 |
| Think / save-files 偏好 | 共享 | 隔离 |
| prompt / speed 临时记录 | 桶级或请求级关联 | 不得串数据 |
| browser context / 登录态 | 共享 | 共享 |

桶只是上下文隔离单位，不是账号或安全隔离边界。当前服务不提供 API 鉴权，能访问服务的客户端都可能使用同一个 ChatGPT 登录态。部署必须继续优先使用 `HOST=127.0.0.1`，远程访问采用 SSH 隧道或 VPN 等外部手段。

## 4. 桶标识设计

### 4.1 显式标识优先

推荐客户端为一个逻辑任务提供稳定的 header：

```http
X-ChatGPT-Session: pi:repo-a:task-42
```

同一任务的每轮请求使用同一个值；不同任务使用不同值。不要每次 HTTP 请求随机生成桶，也不要使用消息 ID 或 prompt 内容作为桶键，否则会破坏跨轮上下文。

建议采用可读且有命名空间的值，例如：

- `pi:repo-a:task-42`
- `codex:repo-b:agent-1`
- `cline:workspace-a`

客户端不能自定义 header 时，可用 OpenAI `user` 字段。UA 自动分桶只应作为兜底，不足以隔离同一客户端中的多个 Agent。

### 4.2 建议的扩展优先级

保留当前优先级，并增加可选 cwd 级别：

1. 显式 session header；
2. 请求体 `user`；
3. 可选工作目录桶；
4. User-Agent 自动桶；
5. 默认桶。

显式标识优先于自动推断。关闭 cwd/UA 自动推断不应影响显式 header/user。

### 4.3 cwd 自动分桶（已实现，默认关闭）

代码已实现 cwd 自动分桶的候选文本收集、明确格式识别、路径规范化和摘要桶键生成。HTTP 入口位于 `server.py::_session_key()`，纯函数位于 `chatgpt_web/session_scoping.py`。代码和单元测试完成不代表已验证任意真实客户端；真实客户端验证仍由任务 M2.6 跟踪。

目标是解决同一客户端、相同 User-Agent 在多个工作目录并行运行任务时仍落入同一桶的问题。

候选输入严格限定为：

- system / developer 消息里的环境元数据；
- Responses API 的 `instructions`；
- 首条 user 消息中的环境元数据（Responses `input` 同样只取首条 user 输入）。

不扫描任意后续 user 消息。用户正文里贴出另一个目录，不应导致桶漂移。多处候选按来源顺序取第一个有效路径：Responses `instructions`、system/developer 消息、首条 user 内容；无效或相对路径继续查找后续候选。

当前只识别以下固定内置格式：

1. `<cwd>...</cwd>`；
2. 独立一行的 `Working Directory: /path`；
3. 独立一行的 `Current Workspace Directory (/path)`。

不接受仅在自然语言中提到 working directory 的文本。只接受绝对 POSIX 路径或带盘符的绝对 Windows 路径；相对路径不会使用桥接服务进程的 cwd 解析，以免把客户端路径错误地映射到服务端目录。Windows 路径统一分隔符并按 Windows 规则规范化大小写；POSIX 路径保留大小写。原始 cwd 只在本次请求内参与 SHA-256 计算，不写入桶键、SessionState 或日志。

桶键形如 `cwd:repo-<digest>`，摘要基于完整规范化路径而非目录名。`SESSION_CWD_HASH_LEN` 默认 `12`，最终键受 `SESSION_KEY_MAX_LEN` 限制。cwd 自动分桶通过 `SESSION_SCOPING_BY_CWD` opt-in，默认关闭；优先级为 header > user > cwd > UA > default。

### 4.4 桶键规范化与冲突

保留当前白名单和长度上限，但必须明确：

- 空值 trim 后进入下一优先级，而不是生成空桶；
- 非法字符替换为 `_`；
- 显式键与自动键应有前缀命名空间；
- 桶键不得直接作为不受限制的文件路径；
- 日志避免暴露完整原始工作目录或敏感标识；
- 对消毒、截断后的碰撞增加测试，必要时使用摘要后缀或拒绝冲突。

### 4.5 命令请求

`/bridge ...` 的桶由 HTTP 入口已经确定的 session key 决定，命令正文不能重新推断桶。`/session/reset` 是独立管理端点，通过 `session` 参数显式指定目标桶；未指定时为默认桶。不要将它与当前桶命令混为一谈。

## 5. SessionState 和任务快照

### 5.1 状态字段原则

SessionState 只保存决定下一轮行为的元数据，不存整段对话正文。

| 字段 | 用途 |
| --- | --- |
| `has_history` | 当前网页会话是否已有可用上下文，决定增量发送或播种 |
| `turns` / `est_tokens` | 会话预算和轮转判定 |
| `cap_hit` / `cap_failures` | 到顶检测和连续失败控制 |
| `pending_rotation` | 下一轮先轮转 |
| `pending_reseed` | 下一轮强制重新播种，必须落盘 |
| `linked_url` | 用户显式绑定的会话 URL |
| `think_mode` / `save_files` | 桶级偏好；None 表示跟随全局默认 |
| `speed_*` | 速度统计数值聚合，不保存 prompt / 回复正文 |
| `last_error` / `updated_at` | 诊断信息 |

新增字段必须同步更新 `SessionState`、schema 白名单、`from_payload()` 校验/默认逻辑、迁移和测试。

### 5.2 持久化格式

继续使用 schema v2 的兼容布局，不另起一套桶文件结构：

```json
{
  "schema_version": 2,
  "has_history": false,
  "turns": 0,
  "est_tokens": 0,
  "sessions": {
    "pi:repo-a:task-42": {
      "has_history": true,
      "turns": 12,
      "est_tokens": 125000
    },
    "ua:cline": {
      "has_history": false,
      "pending_reseed": true
    }
  }
}
```

顶层是默认桶；其它桶存放在 `sessions`。真实文件还包含 schema 定义的其它字段。

持久化要求：

- 默认桶顶层格式保持兼容；
- 写一个桶不得丢失默认桶和其它桶状态；
- 整段 read-modify-write 受同一状态文件锁保护；
- 临时文件 + 原子替换；
- 异步路径通过 `asyncio.to_thread()` 执行同步文件 I/O；
- 文件损坏或 schema 不支持时必须有可见 warning；
- 将来多进程共用状态目录时，进程内锁不够，需文件锁或每桶独立文件。

### 5.3 任务快照

`tasks.py` 与 SessionState 分工不同：SessionState 保存会话控制状态，任务快照保存任务目标和近期消息，用于轮转后的任务续接。

要求：

- 使用规范化 bucket key；
- 通过 `TASK_NAMESPACE` 分目录，避免多个项目共用任务目录时覆盖；
- 每个桶独立快照；
- 使用临时文件替换；
- 不保存 Cookie、令牌、完整图片数据或不必要的敏感环境信息；
- 普通页面回收不删除快照；
- 明确任务结束/桶清理时的快照保留策略。

需要单独核对 Chat Completions 和 Responses 的快照行为，不要假设两条路径天然完全一致。

### 5.4 内存缓存淘汰

`MAX_SESSION_STATE_CACHE` 限制内存 SessionState 缓存。状态已落盘，非活跃桶的内存副本可淘汰，后续按需加载。

淘汰规则：

- 默认桶永不淘汰；
- 活跃桶或持锁桶不得淘汰；
- 不修改磁盘状态和任务快照；
- 清理关联的纯内存 prompt 和请求速度临时数据。

**待审计项：** `_locks`、`_page_ids`、`_last_prompts`、`_last_speeds` 和请求级速度映射是否与状态缓存同步清理。高基数桶长期运行可能让这些字典增长。建议增加大量随机桶键 churn 测试，明确安全删除锁对象的条件（未持有锁且桶不活跃）。

## 6. Page 生命周期和页面池

### 6.1 惰性创建

请求到达时不为所有桶预建 Page。非默认桶的建页流程：

1. 检查 `_pages[bucket]` 是否存在且存活；
2. 页面不存在或失效时获取 `_page_lock`；
3. 在锁内再次检查，避免并发重复建页；
4. 检查 `MAX_SESSION_BUCKETS`；
5. 先回收过期空闲页面，再淘汰 LRU 非活跃页面；
6. 创建 Page，登记页面 ID 和最后使用时间；
7. 导航到新对话或桶的 `linked_url`；
8. 无历史的新会话必须由本轮 seeded prompt 播种。

`_page_lock` 只保护建页、回收和淘汰的短临界区，不得覆盖整段模型生成时间。

### 6.2 页面与状态分离

页面是临时资源，SessionState 是桶的逻辑状态。关闭/淘汰页面时只删除页面池映射、Page、页面 ID 和 last-used 记录；必须保留 SessionState、磁盘状态与任务快照。下次使用该桶时重建页面并播种。

不能仅凭 Page 对象仍在字典里就认定页面可用。`is_closed()` 只能作为快路径检查；实际发送失败仍需强制重建路径。

### 6.3 页面上限和淘汰

`MAX_SESSION_BUCKETS` 限制非默认桶打开的 Page 数。达到上限时：

1. 先回收超过 `BUCKET_IDLE_TTL_S` 的空闲页面；
2. 再选最久未使用且不活跃的页面做 LRU 淘汰；
3. 所有候选页面都活跃时，拒绝建页并返回可操作错误；
4. 不淘汰默认桶或正在处理请求的桶。

`MAX_SESSION_BUCKETS=0` 的语义为**严格限制模式**：禁止创建额外桶，非默认 key 请求会得到可操作错误，且不会自动合并到默认桶。需要所有请求共用默认会话时，应设置 `SESSION_SCOPING=false`。设置为正数时，页面池按本节规则回收空闲页面并淘汰 LRU 非活跃页面。

### 6.4 失效恢复和 linked URL

普通桶页面失效后通常创建新会话并播种，不自动恢复普通网页 URL。显式 `linked_url` 是例外：

- 新页面导航到绑定 URL；
- 检查实际落点是否仍属于目标会话；
- 重定向到首页或其它会话时记录 warning 和指标；
- 不得静默声称绑定恢复成功；
- unlink 后恢复新会话 + 播种策略。

### 6.5 默认桶兼容

- 默认桶 Page 继续使用 `driver.page`；
- 默认桶始终使用 `driver.lock`；
- 默认桶不计入非默认页面上限；
- 空闲回收和 LRU 不主动淘汰默认桶；
- 默认页面失效时必须可重建，不能让旧客户端全部失败。

## 7. 锁、并发和活跃登记

### 7.1 默认串行

`PARALLEL_BUCKETS=false` 时所有桶共用 `driver.lock`。这是推荐生产默认配置：多桶隔离上下文，但一次只驱动一个网页请求。

### 7.2 按桶并发

`PARALLEL_BUCKETS=true` 时，非默认桶使用独立锁。必须保证：

- 同一桶同一时刻只操作自己的 Page；
- 不同桶只操作各自 Page；
- 默认桶仍使用全局锁；
- 页面池创建/淘汰使用 `_page_lock`；
- 状态文件写入另有文件锁；
- 活跃桶不被回收或淘汰；
- prompt、速度、request ID 均按桶或请求级关联。

并行会增加网页风控、CPU、内存和恢复复杂度。没有真实网页测试和负载数据前，不应把它作为默认模式。

### 7.3 锁等待和取消

`BUCKET_LOCK_TIMEOUT_S` 控制锁等待期限。超时应作为忙碌/可重试错误处理，不要误报为模型超时。Responses SSE 的 `BUCKET_LOCK_QUEUE` 要覆盖排队与快速失败行为，快速失败应返回合适的 HTTP 503，避免空 200 SSE 引起客户端立即重试。

客户端断开、任务取消、发送异常和页面重建失败时，必须通过 `finally` 释放锁和活跃计数。

## 8. API 和桥内命令契约

### 8.1 Chat Completions

`POST /v1/chat/completions` 在入口确定 session key 后，将同一值传入：

- 任务快照；
- prompt 和 seeded prompt 流程；
- 非流式 `run_chat_completion()`；
- 流式 `stream_chat_completion()`；
- `driver.send_chat(key=...)`；
- 本地工具执行和工具 ledger；
- 日志上下文和速度统计。

### 8.2 Responses API

`POST /v1/responses` 使用相同桶解析规则。Responses 请求转换、流式执行、工具续轮均须保留同一个 session key。协议输出格式可以不同，但不能出现桶归属差异。

需要专门覆盖 Chat 与 Responses 的：

- header/user/UA 解析优先级；
- `/bridge` 命令当前桶语义；
- Driver 发送时的 key；
- 工具 ledger 的 session key；
- 桶锁、忙碌错误、请求取消；
- 任务快照和轮转播种行为。

### 8.3 `/bridge` 命令

`/bridge session status|reset|reseed|link|unlink` 只作用于当前请求桶。命令必须是整条消息本身，不能从历史或普通正文中执行。命令在网页发送之前由桥本地处理，因此页面失效时仍可查询和恢复当前桶。

### 8.4 `/session/reset`

这是独立管理端点，通过 `session` 参数明确指定桶；不提供时重置默认桶。它与 `/bridge session reset` 的“当前请求桶”语义不同。若保留此端点，应维持可选 `RESET_TOKEN`，并继续限制服务只监听回环地址。

## 9. 配置项

当前主要配置：

| 配置 | 当前默认/示例 | 职责 |
| --- | --- | --- |
| `SESSION_KEY_HEADER` | `X-ChatGPT-Session` | 显式桶键请求头 |
| `SESSION_SCOPING` | `true` | 总分桶开关；关闭后共用默认桶 |
| `SESSION_SCOPING_BY_UA` | `true` | header/user 缺失时按 UA 分桶 |
| `SESSION_KEY_MAX_LEN` | `64` | 桶键长度上限 |
| `MAX_SESSION_BUCKETS` | 代码默认 `8`，示例文件 `3` | 非默认打开 Page 数上限；默认桶不计入。0 = 禁止创建额外桶，非默认 key 请求报错且不合并到默认桶 |
| `MAX_SESSION_STATE_CACHE` | `64` | 内存状态缓存上限；0 表示不限制 |
| `BUCKET_IDLE_TTL_S` | `900` | 空闲页面回收时间；0 关闭回收 |
| `PARALLEL_BUCKETS` | `false` | 是否允许非默认桶独立并发 |
| `BUCKET_LOCK_TIMEOUT_S` | `120` | 桶锁等待时限；0 表示一直等待 |
| `BUCKET_LOCK_QUEUE` | `false` | 流式遇到忙碌桶时是否排队 |
| `SESSION_FILE` | `./user_data/.chatgpt_state` | 状态文件路径 |

cwd 分桶使用固定内置安全格式规则，不开放可由环境变量任意覆盖的正则表达式，以降低误分桶风险。

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `SESSION_SCOPING_BY_CWD` | `false` | 是否按工作目录自动分桶；真实客户端验证前保持关闭 |
| `SESSION_CWD_HASH_LEN` | `12` | 完整规范化路径 SHA-256 摘要长度 |

新增配置必须同时接入 `config/_core.py`、类型化 `SessionConfig`、`.env.example`、配置测试和配置漂移测试。不能只添加模板变量而未接入运行时。

## 10. 客户端使用建议

### 10.1 显式 header（推荐）

```bash
curl http://127.0.0.1:8002/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-ChatGPT-Session: pi:repo-a:task-42' \
  -d '{
    "model": "chatgpt-chat",
    "messages": [{"role": "user", "content": "继续当前任务"}],
    "stream": false
  }'
```

同一个任务的后续请求继续使用 `pi:repo-a:task-42`；不同仓库或独立 Agent 使用不同 key，例如 `pi:repo-b:task-17`。

### 10.2 `user` 字段

客户端不能设置 header 时，可以使用 OpenAI 兼容请求的 `user` 字段。必须保证同一任务每轮请求传入相同值。

### 10.3 UA 自动分桶

无需配置即可按已识别客户端隔离，但同一客户端的多个 Agent 仍可能共用一个 UA 桶。多仓库任务优先使用显式 header 或稳定的 `user` 值；也可在确认客户端确实发送受支持的 cwd 元数据后，显式启用 `SESSION_SCOPING_BY_CWD=true` 作为兜底。该选项默认关闭。

## 11. 必须保持的不变量

1. 同一逻辑任务始终使用同一个桶键；不同隔离任务使用不同桶键。
2. 桶键由 HTTP 入口确定，后续不能重新推断或改变。
3. 默认桶兼容 `driver.page` 和无 key 的旧调用。
4. 同一桶同一时刻最多一个请求操作其 Page。
5. 页面回收不能删除 SessionState、磁盘状态或任务快照。
6. 页面没有历史时必须播种；不能仅凭状态文件有记录就认为网页上下文仍存在。
7. 普通网页 URL 不是自动恢复依据；显式 `linked_url` 单独处理。
8. 活跃桶不能被空闲回收或 LRU 淘汰。
9. 多桶并发落盘不能互相覆盖。
10. prompt、speed、request ID、工具 ledger 和任务快照不得跨桶串数据。
11. `SESSION_SCOPING=false` 必须恢复默认桶旧行为。
12. 分桶不是鉴权或账号隔离。

## 12. 建议补强项

### P0：桶键和请求路径契约

- 为 header > user > cwd（启用时）> UA > default 优先级补齐 API 级测试；
- 对消毒/截断后的桶键增加碰撞测试，考虑摘要后缀或拒绝碰撞；
- 对比 Chat Completions 与 Responses 的任务快照、命令和工具续轮路径；
- 确保所有工具续轮始终使用原请求桶；
- 验证 `/bridge session reset` 和 `/session/reset` 不会重置错桶。

### P1：cwd 自动分桶（代码实现完成，真实客户端验收待完成）

- 已实现受控的 cwd 候选文本收集函数；
- 只从 system/developer、Responses instructions 和首条 user 环境块提取；
- 已实现路径规范化和摘要桶键；
- 已增加配置开关和路径格式单元测试；
- 已覆盖自然语言误命中、后续 user 消息路径和同名目录场景；
- `SESSION_SCOPING_BY_CWD` 默认关闭；真实客户端请求样本和网页隔离证据仍由 M2.6 跟踪，证据充分前不评估默认启用。

### P1：桶级内存资源清理

- 审计 `_sessions`、`_locks`、`_page_ids`、`_last_prompts`、`_last_speeds` 和请求级速度记录；
- 仅在桶未活跃、锁未持有时释放锁对象；
- 增加大量不同桶键 churn 测试；
- 确认页面池淘汰不造成其它桶状态丢失。

### P2：上限与可观测性

- `/healthz` 保持只读，不创建 SessionState 或 Page；
- 桶诊断只暴露必要元数据，不输出 Cookie、token、prompt 或完整敏感路径；
- 为建页、回收、重建、锁超时和上限拒绝提供可检索日志/指标。

### P2：谨慎评估并发

- 默认保持 `PARALLEL_BUCKETS=false`；
- 先测量串行吞吐、页面资源和站点错误率；
- 并发测试覆盖取消、崩溃、状态同时落盘和不同桶同时播种；
- 只有真实网页证据支持时，才为特定部署启用并发。

## 13. 测试与验收

### 13.1 桶键解析

| 测试 | 预期 |
| --- | --- |
| header 与 user 同时存在 | 使用 header |
| 只有 user | 使用 user |
| 无 header/user 且已知 UA | 使用 `ua:<client>` |
| 未知 UA 有合法 product token | 使用安全 fallback |
| UA 只包含已知名称子串 | 不误识别 |
| 空 header / 空 user | 继续 UA 或默认桶 |
| `SESSION_SCOPING=false` | 所有请求走默认桶 |
| `SESSION_SCOPING_BY_UA=false` | 显式标识仍生效，不做 UA 自动分桶 |
| 非法字符和超长 key | 安全规范化，不路径穿越 |
| 两个 key 规范化后相同 | 有明确碰撞策略 |
| 两个 cwd | 不同规范化路径生成不同桶 |
| 后续 user 正文包含路径 | 不触发桶漂移 |
| `SESSION_SCOPING_BY_CWD=false` | 不尝试 cwd 分桶，继续走 UA/default |
| header/user 与 cwd 同时存在 | 使用显式 header/user |

### 13.2 状态和页面生命周期

| 测试 | 预期 |
| --- | --- |
| 默认桶状态写入 | 顶层兼容格式保持不变 |
| 多个非默认桶保存 | `sessions` 中互不覆盖 |
| 并发保存 | read-modify-write 不丢状态 |
| 首次使用新桶 | 创建独立 Page 和状态 |
| 页面关闭后再次请求 | 重建 Page 并播种历史 |
| 活跃桶达到页面上限 | 不淘汰活跃桶；没有候选时给可操作错误 |
| 空闲 TTL 回收 | 只关 Page，状态和快照保留 |
| LRU 淘汰 | 只关 Page，状态和快照保留 |
| linked URL 页面重建 | 导航并检查真实落点 |
| linked URL 已失效 | warning/指标可见 |
| 大量桶 churn | 桶级临时映射有明确生命周期 |

### 13.3 并发与 API 路径

| 测试 | 预期 |
| --- | --- |
| 默认串行模式不同桶同时到达 | 共享锁，网页操作不重叠 |
| 并行模式不同桶同时到达 | 各自 Page 和锁 |
| 同一桶同时到达 | 互斥，锁超时符合配置 |
| 请求尚未拿锁时页面回收 | 活跃登记阻止回收 |
| 请求取消或异常 | finally 释放活跃计数与锁 |
| Chat 与 Responses 使用相同 key | 共享同一桶状态 |
| 不同桶速度统计 | 请求级样本不互相覆盖 |
| `/bridge session reset` | 只重置当前桶 |
| `/session/reset?session=...` | 只重置指定桶 |
| 本地工具续轮 | 沿用原始桶键 |

### 13.4 真实网页 E2E

单元测试不能证明网页端上下文真正隔离。上线前至少使用两个独立工作目录和两个任务执行真实测试：

1. 在 A、B 两个目录分别启动 Agent，指定或解析不同桶；
2. 交替发送不同记忆标记，检查 A 只记得 A，B 只记得 B；
3. 让 A 触发轮转，确认 B 的上下文保持不变；
4. 关闭 A 的页面，再请求 A，确认只重建 A 并通过播种恢复；
5. 重启桥接服务，再分别请求 A、B，验证新网页会话与客户端历史播种策略；
6. 默认模式验证串行；只有打开并发时才验证不同桶同时生成；
7. 保存桶映射、日志、网页截图/录屏和关键响应作为证据。

没有执行的 E2E 用例必须标记为 `BLOCKED` 或 `NOT RUN`，不能以单元测试结果代替真实网页结果。

## 14. 分阶段实施路线

### 阶段 0：冻结现有契约

- 补齐桶键优先级和默认桶兼容测试；
- 核对 Chat / Responses 传递 session key 的完整性；
- 已明确 `MAX_SESSION_BUCKETS=0` 的严格限制语义，并与 `SESSION_SCOPING=false` 区分；
- 审计桶级字典的清理策略。

交付：基线测试通过，不改变默认行为。

### 阶段 1：cwd 自动分桶（代码实现完成）

- 已实现受控 cwd 提取、规范化和摘要纯函数；
- 已加入默认关闭的配置开关；
- 已增加内置格式 fixture、优先级和误命中单元测试；
- 专项测试与完整 pytest 已通过；
- 真实客户端格式和网页会话隔离仍需按 M2.6 验证，验证完成前保持 opt-in。

交付：代码层面支持同一 UA 下按不同工作目录生成稳定桶键；真实客户端隔离效果尚待验收。

### 阶段 2：桶级资源生命周期

- 清理长期不活跃桶的锁和临时统计映射；
- 统一页面上限和缓存上限语义；
- 增加高基数桶键压力测试。

交付：页面数量和桶级临时内存有明确边界。

### 阶段 3：API 一致性

- 统一 Chat / Responses 的桶级任务快照、命令、工具 ledger 和错误行为；
- 验证工具续轮、请求取消、流式断连与锁超时。

交付：协议格式不同，但桶归属与会话状态一致。

### 阶段 4：并发能力评估

- 保持默认串行；
- 使用独立测试 profile 执行并发 E2E；
- 测量站点错误、风控挑战、页面资源和响应时间；
- 有证据后才为特定部署开启按桶并发。

交付：有量化数据支撑的并发配置建议。

## 15. 设计结论

本项目已具备多桶核心架构：请求级桶键、桶级 SessionState、每桶 Page、页面池、回收和 LRU、活跃计数、按桶锁、状态持久化、任务快照、桥内命令及健康检查。默认桶兼容和页面/状态分离是必须长期维护的设计不变量。

当前项目已实现按工作目录自动分桶的代码能力，但该功能默认关闭，且尚未完成真实客户端验收。同一个客户端在多个仓库同时运行 Agent 时，现阶段最可靠的隔离方式仍是显式设置 `X-ChatGPT-Session` 或稳定的 `user` 值；确认客户端发送受支持的环境元数据后，才建议 opt-in 启用 cwd 分桶。

后续重点是完成 M2.6 真实客户端验证，并继续补齐请求路径和资源生命周期的不变量测试，最后评估真正的按桶并行。方案继续以单个登录态、单个持久化 browser context 和默认串行为基线；多桶不代表多账号隔离，也不提供 API 鉴权。
