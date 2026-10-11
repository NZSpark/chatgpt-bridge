# 会话绑定（`/link`）移植指南 —— 给 DeepseekBridge / GeminiBridge

> 参考实现：ChatGPTBridge `a29e22a feat: add session linking support`（18 文件，+1269/−11）。
> 本文只讲**要移植什么、在两个目标项目里分别落到哪个函数**，以及移植时最容易踩的坑。

## 1. 问题与解法（一段话）

网页版有时会**自行开一条新的网页会话**（ChatGPT 会把当前标签跳到新对话），桥这一侧
看到的现象就是「句柄消失 / 上下文不在原来的会话里」。原有设计在句柄失效时只会
**新开一条空白会话 + 播种重放历史**：上下文不丢，但用户真正想要的是「继续在那条
指定会话里发」。于是加一条用户可控的路径：**把一个会话桶绑定到用户给出的网页会话
URL**，此后页面漂移、句柄失效重建、轮转、超时恢复、重启都回到这条会话。

## 2. 对外语义（三个项目保持一致，客户端不用改）

| 输入（整条消息就是这一行） | 语义 |
| --- | --- |
| `/link https://<站点>/<会话路径>` | 把**当前会话桶**绑定到该会话；默认认为它**已有此前上下文** → 之后只发增量（`has_history=True`） |
| `/link <URL> --seed` | 绑定，但下一轮把**完整历史播种**进去（那条会话其实是空的时用；代价是历史在网页端重复一遍） |
| `/link` | 查看当前绑定 |
| `/unlink` | 解除绑定，恢复「轮转 + 播种」的默认行为 |

* 命令**在 HTTP 边缘执行，不发给网页版**：句柄全丢、页面漂移时它必须还能用。
* 链接白名单 `LINK_ALLOWED_HOSTS`（逗号或 `||` 分隔，子域自动允许，`*` = 不限制）。
* HTTP 版：`POST /session/link?url=&session=&seed=`、`POST /session/unlink?session=`，
  复用 `RESET_TOKEN`（`X-Reset-Token`）。
* `/healthz` 的 `buckets.<key>` 里多一个 `linked_url`。

## 3. 五条不变量（移植时不要破坏）

1. **绑定只决定「页面落到哪里」**。命令本身**不碰页面**（可以在浏览器没起时执行），
   导航交给发送前的对齐步骤与建页路径。
2. **唯一被持久化的 URL 是用户显式给的那一条**（`linked_url`）。自动探测到的会话
   URL 依旧不入库（否则就是老问题：桥自作主张回到一条它猜的会话）。
3. **命令在 HTTP 边缘**：适配层/路由里短路，`prompt_tokens=0`，不计上游用量。
4. **不放宽任何终止条件**：导航次数每次请求有界（发送前 ≤1 次 + 建页 ≤1 次 +
   轮转 ≤ 重试次数），`PAGE_REBUILD_MAX=1` 不变，重建不消耗重试阶梯。
5. **未绑定的桶行为完全不变**（默认桶、分桶、轮转、播种全部照旧）——移植后旧用例必须全绿。

## 4. 改动清单（按职责，与站点无关）

| 职责 | 参考实现位置 | 内容 |
| --- | --- | --- |
| 命令识别 / 校验 / 应答 | `chatgpt_web/linking.py`（新，285 行） | `parse_command`、`canonical_conversation_url`、`allowed_hosts`、`same_conversation`、`latest_user_text`、`handle_command` |
| 状态字段 | `session/schema.py` | `SessionState.linked_url: Optional[str]`，进 `_SESSION_FIELDS` 与 `from_payload`（空白 → `None`） |
| 状态写入 | `session/store.py` | `to_payload()` 带 `linked_url`；`linked_url()` / `link_session()` / `unlink_session()` |
| 落点：建页 | `page_pool.py::_open_bucket_page` | 绑定桶**新页面直接落在那条会话**（不再 `goto(HOME)` + 新建对话） |
| 落点：导航助手 | `page_pool.py::_enter_linked_session` | `goto(target)` → 等就绪 → **落地校验**（`page.url` 不在目标上就告警 + 计数）→ 选思考模式 → `_touch_page` |
| 落点：发送前对齐 | `page_pool.py::_ensure_linked_target` | 用 `page.url`（同步属性，无往返）和绑定地址比对，不一致就重新导航 |
| 发送前挂载 | `chat_io.py::_send_chat_with_ladder`（`send_chat` 的内部阶梯），`await self._ensure_page(bucket)` 之后 | `await self._ensure_linked_target(bucket)` |
| 轮转改向 | `completion/lifecycle.py::_start_new_session` | 绑定桶**不真轮转**：重新打开绑定会话（上下文还在，不播种），只清轮数/体积/到顶 |
| 超时恢复改向 | `completion/lifecycle.py::_recover_session` | 绑定桶回到绑定会话并返回 `True`（不换新会话） |
| 启动恢复 | `completion/lifecycle.py::_restore_session_on_startup` | 默认桶有绑定就落回那条会话，直接 `return` |
| 读 URL 的小工具 | `errors.py::page_url(page)` | 读不到一律空串（`page_lost_reason` 也改用它） |
| 指标 | `metrics.py` | 计数器 `session_link_navigation_total`（导航次数）+ `session_link_miss_total`（导航后没落到目标） |
| 配置 | `config` | `LINK_ALLOWED_HOSTS`（默认只允许官方域名）+ 进 `ConfigBundle` |
| 短路：chat 流式/非流式 | `api/chat_adapter.py` | `handle_command` 在 `send_chat` 之前；流式直接吐 SSE + `[DONE]` |
| 短路：responses | `api/responses_adapter.py::run_chat` | `handle_command` 提到函数第一行，`return reply, [], [], reply` |
| HTTP 端点 | `server.py` | `POST /session/link`、`POST /session/unlink` + `/` 索引 |
| 文档/样例 | `README.md`、`.env.example`、`doc/update.md` | 用法、白名单、真机现象 |

**最小可用路径**（建议的落地顺序）：
① `linking.py` + `SessionState.linked_url` + `link_session/unlink_session` →
② `_ensure_linked_target` 挂在发送阶梯里（目标项目就是 `send_chat`，参考实现是它的内部阶梯方法；这一步就已经解决「下次发送发到用户给的链接」）→
③ 建页落点 → ④ 轮转 / 超时 / 启动三条改向 → ⑤ HTTP 端点 → ⑥ 文档与测试。

## 5. 站点适配点（`linking.py` 里唯一需要按项目改的地方）

| 项目 | 会话 URL 形态 | `LINK_ALLOWED_HOSTS` 默认值 |
| --- | --- | --- |
| ChatGPTBridge（参考） | `https://chatgpt.com/c/<id>`（自定义 GPT 为 `/g/<gpt>/c/<id>`） | `chatgpt.com,chat.openai.com` |
| DeepseekBridge | `https://chat.deepseek.com/a/chat/s/<id>`（已有 `config.SESSION_URL_RE`） | `chat.deepseek.com` |
| GeminiBridge | `https://gemini.google.com/app/<id>`（`config.WEBSITE` 同前缀） | `gemini.google.com` |

要改的只有三处：

1. `_CONVERSATION_SEGMENT` / `conversation_id()`：按上面路径取会话 ID 段。
   DeepSeek 是 `a/chat/s/<id>`（多段），Gemini 是 `app/<id>`（单段）。
2. `canonical_conversation_url()` 里拼回标准地址的那一行。
3. 会话 ID 的形态正则：参考实现用 `(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+`
   —— 也就是「**一个合法 URL 路径段**」，外加长度 8–256。**不要**写死成 UUID，也**不要**
   缩成 `[A-Za-z0-9_-]`：ID 是站点（或客户端）自己定的不透明字符串，真机就有
   `.../c/local-chatgpt%3A<uuid>` 这种（冒号被 `encodeURIComponent` 转义成 `%3A`）——
   按字母数字校验会把用户手里那条地址直接拒掉。宁可宽松：真正的守门人是
   「主机 + 路径段」，形态只用来挡明显不是会话地址的东西（空 / 空格 / 非法 `%` 转义 /
   过短 / 过长）；**客户端临时 ID 另由第 5 条按前缀拒掉**（那与「形态」无关，是语义问题）。
   别把最小长度去掉：`/c/new`、`/c/short` 那类要么是「空白新对话」、
   要么根本不是会话 ID。参考实现里这三个常量叫 `_CONVERSATION_ID_RE`、
   `_CONVERSATION_ID_MIN_CHARS`、`_CONVERSATION_ID_MAX_CHARS`。
4. 地址**原样保留**（不要 `unquote` 后再拼）：`%3A` 与 `:` 在路径段里等价，解码重建
   有可能拼出站点不认的地址。要归一化的是**比对**：`same_conversation()` 比 ID 前先
   `unquote()`（只解码、**不**大小写归一——站点真区分大小写时，把两条不同会话判成
   同一条的代价是「这一轮发进错误的会话」，比多导航一次严重得多）。
5. **解码后按前缀拒掉客户端临时 ID**（`local-chatgpt:` / `WEB:`，大小写不敏感）。它们是网页版
   在新会话拿到服务端 ID **之前**的地址栏写法（约 1 秒后就被替换成正式 ``/c/<uuid>``），
   从别处（包括同一 profile）新导航过去一律被**重定向回首页**——绑定看似成功，之后每一轮却都发进
   一条陌生新会话（真机：`user_data/Default/History` 里成对出现临时地址 → 正式地址；当轮消息
   实际创建了一条新对话）。错误文案必须给出正确取法（先发一条消息、等地址栏变成正式 ID 再复制）。
   参考实现里这个常量叫 `_CLIENT_PLACEHOLDER_PREFIXES`。

`same_conversation()` 优先比对**会话 ID**（比对前先还原百分号转义）、其次比对「去查询串/尾斜杠」
的整串；两侧都取不到 ID 时一律返回 `False`（判不准就重新导航，代价只有一次跳转）。

**加分项：落地校验**。`_enter_linked_session()` 在 `goto` 之后比一次 `page.url`：不在目标上
就 `logger.warning` + `metrics.inc("session_link_miss_total")`。站点把不存在 / 已失效的会话地址
重定向回首页时，绑定会**静默**失效（之后每轮都发进一条陌生会话），这条日志是唯一的线索；
不抛错是因为「绑定 = 那条会话已有上下文」的判据在 `link_session`，导航不该推翻它。

## 6. 两个目标项目的具体落点

### 6.1 通用（两边都要做）

* `session_store.py`：`SessionState` 加 `linked_url`，并在 `to_payload()` / `from_payload()`
  里带上它（**这三个项目都是同一份 dataclass 风格**）。加 `linked_url()` /
  `link_session(url, key, seed)` / `unlink_session(key)` 三个方法，实现照抄参考实现。
* `link_session` 的副作用是**有意的**：`has_history = not seed`、`cap_hit = False`、
  `pending_rotation = False`、`last_error = None`（一次全新绑定就是一次干净开始）。
* `unlink_session` 的副作用同样关键：`linked_url = None` + `has_history = False` +
  `pending_rotation = True`，下一轮才会「开新会话 + 播种」，而不是把增量发进那条
  已经不归我们管的会话。
* `page_pool.py`：加 `_enter_linked_session()` 与 `_ensure_linked_target()`；建页路径
  里若 `state.linked_url` 存在就直接导航过去并 `return`。
* `errors.py`：加 `page_url(page)`（参考实现里 `page_lost_reason` 也改用它）。
* `chat_io.py::send_chat`：`await self._ensure_page(bucket)` 之后调用
  `await self._ensure_linked_target(bucket)`。
* `server.py` / 适配层：命令短路 + 两个 HTTP 端点（复用现有 `RESET_TOKEN` 校验）。
* 配置：`LINK_ALLOWED_HOSTS`（按属性读 `config.X`，便于测试 `patch.object`）。

### 6.2 DeepseekBridge 专属（**它已经有一套 URL 恢复，冲突点在这里**）

DeepSeek 项目本来就把「上次的会话 URL」存进 `SessionState.url`，并会回到它。
移植时**不是从零加**，而是给 `linked_url` 更高的优先级，并避免两套 URL 打架：

| 位置 | 现在 | 改成 |
| --- | --- | --- |
| `page_pool.py::_bucket_target()` | `HOME_URL if cap_hit else (state.url or HOME_URL)` | 先看 `state.linked_url`，有就返回它（**优先级最高，压过 `cap_hit`**：到顶也不该把用户指定的会话丢掉） |
| `page_pool.py::_create_bucket_page()` | 末尾按 `_current_session_url()` 重算 `has_history` | 绑定桶**不要覆盖** `has_history`（`--seed` 语义要求它在下一轮为 `False`，落点正确不代表已经有历史） |
| `page_pool.py::_ensure_default_page()` | 重建时 `goto(_bucket_target(...))` 并重算 `has_history` | 同上两条（走 `_bucket_target` + 绑定桶不重算） |
| `session_store.py::_start_new_session()` | 轮转 = `goto(HOME)`，`_save_session_state(clear_url=True)` | 绑定桶**不轮转**：改回绑定会话（见 §4 轮转改向），并**不要**用 `clear_url=True` 把 `linked_url` 一起清掉（它只清自动 URL） |
| `completion.py::_recover_session()` | 按 `_saved_session_url()` 重新进入 | 绑定桶优先用 `linked_url`（`_saved_session_url` 只返回 `config.SESSION_URL_RE` 命中的地址，会漏掉用户新给的那条） |
| `chat_io.py::send_chat` 重试阶梯 | 中间级调 `_recover_session` | 不变；绑定桶只是让 `_recover_session` 走新分支 |
| `tests/test_multi_session.py` 等 | 断言 `goto(HOME_URL)` / `_bucket_target` | 绑定相关的新用例单独放 `tests/test_session_linking.py`，**不要**改旧用例的断言（未绑定的桶行为必须逐字不变） |

另外注意：DeepSeek 的 `_current_session_url()` 依赖 `SESSION_URL_RE`，它**只认 UUID
形态**；而 `/link` 的校验故意更宽松（见 §5）。两者用途不同，不要合并。

### 6.3 GeminiBridge 专属（最接近参考实现）

Gemini 项目**没有**任何 URL 持久化（`SESSION_URL_RE`、`state.url` 都不存在），
`completion.py` 里集中了三条生命周期路径。所以它的移植 ≈ 参考实现的搬移：

* `session_store.py`：加 `linked_url`（这个项目 `SessionState` 与 `SessionStoreMixin`
  在同一个文件里，一次改完）。
* `completion.py`：改三处 —— `_start_new_session()`（轮转改向）、
  `_recover_session()`（超时恢复改向）、`_restore_session_on_startup()`（启动落回）。
  参考实现里前两个在 `completion/lifecycle.py`、第三个同文件，逻辑同构。
* `page_pool.py::_ensure_page()`：现在无条件 `goto(HOME_URL)` + `_open_new_chat()` +
  `has_history=False`；绑定桶改为 `goto(linked_url)`（**不要点「新建对话」**，
  那正是会把用户指定的会话丢掉的按钮），并保留绑定决定的 `has_history`。
* 默认桶：Gemini 的默认桶就是 `self.page`，`_ensure_page` 对默认桶直接 `return`。
  启动恢复已经覆盖了「默认桶落回绑定会话」，无需额外改。

## 7. 命令短路放在哪（两个目标项目的落点）

两个项目都是「路由算好 `prompt` / `seeded_prompt` → 流式或非流式调 `driver.send_chat`」：

* `server.py::chat_completions`（流式与非流式共用一个入口）
* `responses.py::run_chat`（`/v1/responses` 的流式与非流式都汇到这里）

**建议比参考实现更早一步**：在 `session_key` 解析出来之后、`tasks.record(...)` 与
`build_prompt(...)` **之前**短路。参考实现把命令检查放在适配器里（两个协议汇合处），
位置偏后：`/link` 会先被写进任务快照，可能成为「任务目标」。按本节的位置放，命令
**零副作用**——不写任务快照、不算用量、不碰页面。

流式分支要么在路由里直接返回一个只吐命令应答的 SSE 生成器（复用现成的
`_chunk_text` + `_sse`），要么在 `streaming._stream_chat_completion` 开头同样短路。
无论哪种，**两条路径都要覆盖**，否则会出现「非流式能用、Pi 的流式用不了」。

## 8. 测试移植（参考实现 `tests/test_session_linking.py`，41 例）

按分组照搬改名即可，每组至少 1 例：

1. `ConversationUrlTests`：用户真正会粘贴的几种形态、站点自定义（非 UUID）ID 要能过、非法 `%`
   转义 / 空格 / 超长 / 过短要拒、**客户端临时 ID（`.../c/local-chatgpt%3A<uuid>`、`/c/WEB:<uuid>`）
   必须拒且给出正确取法**、外部域名默认拒绝、配置后允许、`*` 不限制、分享链接/非会话链接拒绝、
   `same_conversation` 忽略查询串、尾斜杠与百分号转义。
2. `CommandParsingTests`：整行才算命令、`--seed` 两个位置都认、
   `/link` 单条 = 状态、非法形态要**报错而不是吞掉**、普通正文不是命令、
   只看最后一条 user 消息（历史里的旧命令绝不能重放执行）。
3. `LinkCommandHandlingTests`：绑定成功应答、**命令不碰页面**、`--seed` 变体、
   状态/解除、坏链接不绑定只报错、非命令交给模型、绑定能跨状态重载存活。
4. `LinkedPageTargetingTests`（判别性最强的一组）：句柄丢失重建后落在绑定会话、
   发送前对齐漂移的页面、已在该会话时不导航、未绑定桶行为不变、
   新页面仍开空白对话、轮转回到绑定会话、启动恢复落在绑定会话、关掉的页面重建到绑定会话。
5. `LinkEndpointTests`：`/session/link` 绑定并回统计、坏 URL → 400、`RESET_TOKEN` 生效、`/session/unlink` 清除。
6. `BridgeCommandShortCircuitTests` / `LinkRouteTests`：chat 流式与非流式、
   responses 流式与非流式，都由桥应答且**不碰页面**、`prompt_tokens=0`。

**判别性验证（不看覆盖率，看用例会不会挂）**：用 pytest 插件临时注入变异、不改文件 ——
① 置空 `_enter_linked_session` → 第 4 组目标类用例应全挂；
② 置空 `_ensure_linked_target` → 漂移用例挂；
③ 让 `from_payload` 丢掉 `linked_url` → 持久化/启动恢复用例挂。
三个变异都不挂，说明用例在骗自己。

## 9. 验收与已知坑

**单测之外的真机验收**（隔离实例 + 临时 profile，别占用正在跑的实例）：

1. `/link <URL>` 经 `/v1/chat/completions` → 200 + 桥的确认回复，**日志里没有任何上游调用**（`prompt_tokens=0`）；
2. 随后一条普通消息：新开的页面**直接落在那条会话**（日志 `已打开绑定的网页会话 …`）；
3. 改绑到另一条会话：下一条消息日志出现 `（发送前对齐）`，请求确实发往新地址；
4. `/healthz` 暴露 `buckets.<key>.linked_url`；状态文件里有 `linked_url`；
5. **重启后**日志出现「已按绑定的会话 URL 恢复默认会话」。

**改后真机复测（2026-10-10，ChatGPTBridge，profile 副本 + headless、未发送任何消息）**：干净加载的登录
会话上 `input_candidates()` 只给出 1 个候选（`form` 内的 ProseMirror、`role=textbox`、
`aria-label="Ask ChatGPT"`），`_fill_prompt()` 读回正常、`_clear_input` 清空草稿成功 —— 新的首选
选择器在**当天的真实 DOM** 上有效。**但「同页多个 ProseMirror」的退化态未再复现**（触发过程未知）：
移植方要按第 6 条实现候选枚举并保留单测，别把这条路当成已验证过的分支。全量回归：`pytest -q` →
612 passed / 17 skipped / 81 subtests passed；改动文件的 `ruff` 全绿。

**坑**：

* `--seed` 会把历史在网页端重复一遍；默认（不播种）的前提是那条会话里真的有上下文。
* 会话 ID 是站点自定义的不透明字符串：放宽到了「一个合法 URL 路径段」，但**地址本身是否存在
  只能由站点回答**。绑定后看日志有没有「导航后页面停在别处」/ `session_link_miss_total` 增长。
* **临时 ID 不要放行**：`local-chatgpt:` / `WEB:` 前缀是客户端在新会话拿到服务端 ID 之前的
  地址栏写法（约 1 秒后就被替换）；放行它们等于让之后每一轮都发进一条陌生新会话
  （真机后果与取法文案见 §5 第 5 条）。
* **输入层要按候选枚举**（与 `/link` 无关，但同一轮真机暴露）：composer 已无 `#prompt-textarea`
  id，且页面 DOM 退化时同页会出现多个 ProseMirror；只取「第一个 attached」会把 prompt 填进
  点不动的僵尸节点。参考实现：`dom_adapter.input_candidates()` + `browser_input._fill_prompt()`
  逐个试填校验，外加 `ChatGPTComposerError`（可恢复：新开页面后重发一次，共用 `PAGE_REBUILD_MAX`）。
* 命令只认「整条消息 = 这一行」，且只看最后一条 user 消息。
* 绑定按**会话桶**生效（发 `/link` 的那个 `X-*-Session`）。
* 运行中的旧实例必须**重启**才生效；有头模式下重启会复用同一个 profile。
* 登录态下的「模型确实在该会话里正常答复」需要独占 `user_data` 的登录会话，别在
  主实例上做这项验收。
* 白名单放宽要谨慎：绑定后 prompt 会真的发到那个站点的会话里。
