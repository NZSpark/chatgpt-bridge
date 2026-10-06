# ChatGPTBridge 任务分解与验收标准

> 来源：本文由 [`doc/update.md`](update.md)（2026-10-06 版）分解而来。每个任务的「来源」列都回指 update.md 的章节，便于双向追溯。
> 基线：`main @ 700bb67`，单测 `pytest -q` → **219 passed / 24 skipped**（24 个 skip = e2e 用例）。
> E2E 基线：真实登录 + 有头，**3 failed / 19 passed / 2 skipped**（C1、C2、D2 失败；2 个 Markdown 用例 skip）。

---

## 0. 使用说明

### 0.1 状态图例

| 标记 | 含义 |
| --- | --- |
| `TODO` | 未开始 |
| `DOING` | 进行中 |
| `BLOCKED` | 被前置任务阻塞 |
| `DONE` | 已通过验收标准 |
| `WONTFIX` | 明确不做（需在 §7 记录理由） |

### 0.2 全局约束（所有任务共同遵守）

1. **不得用削弱断言 / 加 skip / 吞异常的方式让检查变绿**。若必须改动测试，需在该任务的「风险/说明」里写明原因，并验证目标行为。
2. 单测套件基线 `219 passed / 24 skipped` 必须保持（注：2026-10-06 套件去重收敛后 e2e 用例 24 → 17，`skipped` 随之变为 17，属预期的用例合并，不是覆盖退化；见 doc/e2e_test_design.md §7）；允许新增测试使数字上升，不允许减少断言或降低断言强度。
3. 涉及真实网页的验收（`E2E_*`）必须在**有头**模式运行；`HEADLESS=1` 会被 Cloudflare 拦截，其结果无效。
4. 跑 e2e 前必须停掉占用 `user_data` 的 bridge（`pkill -f chatgpt_api_server.py`）；端口以 `.env` 的 `PORT=8002` 为准，**建议**显式带 `E2E_PORT=8002` 以免疫宿主 shell 自带的同名环境变量（历史事故见 update.md §2.3；T2.1 已按用户要求关闭）。
5. 提交/推送需用户明确授权；本文件只定义任务与验收，不授权任何发布的动作。
6. **不引入 API Key / Bearer 鉴权**：零配置免 key 是本项目的**核心设计需求**（客户端 `api_key` 随便填即可），详见 §9 非目标与 T3.2。任何「加鉴权」的改动都需先推翻该设计决定，而不是在实现时顺手加上。

### 0.3 验收命令速查

```bash
# 全量单测（无网络）
.venv/bin/python -m pytest -q

# 全量 E2E（真实访问，约 8–25 分钟；需先 pkill bridge）
CHATGPT_E2E=1 E2E_HEADED=1 E2E_FULL=1 E2E_PORT=8002 \
  .venv/bin/python -m pytest tests/e2e -v -s

# 单条 E2E（例：工具调用对等）
CHATGPT_E2E=1 E2E_HEADED=1 E2E_FULL=1 E2E_PORT=8002 \
  .venv/bin/python -m pytest tests/e2e/test_parity.py -k c1 -v -s

# DOM / 选择器校准（不依赖 bridge；含新建对话候选推断）
CHATGPT_E2E=1 E2E_HEADED=1 .venv/bin/python -m pytest tests/e2e/test_dom_probe.py -v -s

# 跑完确认无残留（历史 bug：harness 在 setUpModule 失败时不回收）
pgrep -fl 'uvicorn chatgpt_api_server'
```

---

## 1. 里程碑与依赖关系

```
M0 先行判定（必须先做，成本最低、结论决定 M1 的范围）
  T0.1 分桶隔离确认实验  —— WONTFIX（用户决定：同一任务上下文一致即可，跨桶隔离非验收项）
                                      │
                                      ▼
M1 P0 修复                          T1.2 跨桶隔离 —— WONTFIX（同上）
  T1.1 工具调用播种路径修复
  T1.3 edit_markdown 路径沙箱

M2 P1 harness 与一致性（与 M1 可并行，互相独立）
  T2.1 E2E 端口默认值与校验   —— WONTFIX（用户决定：PORT 已由 .env 固定 8002）
  T2.2 bridge 子进程失败清理
  T2.3 注入块标题常量化
  T2.4 _bucket_busy 语义修复

M3 P1/P2 可观测性与加固
  T3.1 日志体系与异常可见性
  T3.2 非回环暴露的告警与文档声明（不引入 API Key）
  T3.3 同步 I/O 转线程 + 原子写
  T3.4 选择器更新 + /healthz 自检（含探测脚本修复）

M4 P2 工程化与优化
  T4.1 pyproject + ruff/mypy + CI
  T4.2 结束判定状态机抽取 + _complete_text 优化
  T4.3 e2e 软失败指标化
  T4.4 文档死链修复 + 重建 doc/e2e_test_design.md
  T4.5 清理死代码与重复实现
  T4.6 asyncio.get_running_loop
  T4.7 E2E 稳定性验证（连跑 2–3 次）
```

**建议执行顺序**：`T1.1 / T1.3`，同时并行 `T2.2`（不修则后续任何 e2e 验收都不可信），再推进 M3、M4。T0.1 / T1.2 / T2.1 已 WONTFIX（见 §7 理由）。

**执行结果（2026-10-06）**：M1–M4 的代码任务已完成（T1.1 完成单测待 e2e、T3.4/T4.3 同理），T4.1 按用户决定「保留并合并」完成；**T4.7 按用户决定 `DEFERRED`（暂不连跑 e2e）**，因此所有依赖真实网页的验收保持未完成状态。

---

## 2. M0 — 先行判定

### T0.1 ~~分桶隔离确认实验~~（WONTFIX）

| 项 | 内容 |
| --- | --- |
| 状态 | `WONTFIX`（用户决定，2026-10-06） |
| 优先级 | ~~P0~~ |
| 来源 | update.md §2.2 |
| 预估 | — |
| 依赖 | 无 |

**关闭理由（用户原话）**：“同一个任务必须保持上下文一致，不存在串台。这个无须验证。”——既有验收口径只看**同一会话桶内的上下文一致性**（D1 多轮记忆、D3 reset 后播种），跨桶隔离不作为设计需求，因此不做判定实验、不新增验证。

**历史证据（留档，不结论）**：D2 断言失败（桶 B 看到了桶 A 的暗号 `Tiger-77`）；一次探测（`/tmp/iso_test.py`）中全新桶 `iso-b` 也回了 `iso-a` 的 `ZULU-99`，且 `/healthz.session_keys` 同时出现两个桶名（说明请求头已被采信）。后续不再追查（用户明确无须验证）。

**后续节**：观察性证据保留在 update.md §2.2；D2 用例保留原样、不删不削弱，但不计入验收门禁。

**背景/证据**：E2E `D2` 失败——桶 B（全新 `X-ChatGPT-Session`）如实返回了桶 A 的暗号 `Tiger-77`；而 `D1`（同桶多轮记忆）、`D3`（reset 后播种）均通过，说明「按桶留历史」本身工作，嫌疑指向**跨桶串台**。

**目标**：把「疑似串台」变成**确定结论**（确认 / 排除），并留下可复现证据。

**实施要点**：
1. 起一个 bridge（注意 `PORT=8002`，见 T2.1）：
   `PORT=8002 HEADLESS=false .venv/bin/python chatgpt_api_server.py`
2. 用两个不同会话头交叉读写：
   - `X-ChatGPT-Session: iso-a` → 「请记住暗号 ZULU-99。只回复：已记住。」
   - `X-ChatGPT-Session: iso-b` → 「如果你的对话历史中没有出现过暗号，请只回复 NONE；如果出现过，输出该暗号。」
3. 每一步都查 `GET /healthz` 的 `session_keys` / `cluster.keys`，确认 `iso-a`、`iso-b` 是否被登记（若只出现 `ua:python-urllib` 或 `default`，说明请求头未被采纳、所有请求塌缩成同一会话）。
4. 复现 `MAX_SESSION_BUCKETS=3`（`.env` 实际值）+ `PARALLEL_BUCKETS=true` 的组合，检查 `_ensure_page` 的 LRU 换页是否让不同桶落到同一个 ChatGPT 会话。
5. 把结论写入 update.md §2.2（改为「已确认」或「已排除」）。

**验收标准**：
- [ ] 有一段可复制的命令/脚本，能稳定复现「桶 B 是否看到桶 A 的内容」。
- [ ] `/healthz.session_keys` 的输出被记录，明确回答「请求头是否被采信」。
- [ ] 结论写入 update.md §2.2；若排除，则同时说明 D2 失败的真实原因（例如模型幻觉 / 测试自身问题）并给出理由。

**风险/说明**：若判定为「模型幻觉」，不得直接删掉 D2；应先证明「单桶场景下模型也会凭空说出 `Tiger-77`」，再据实调整用例并把理由写进文档。

---

## 3. M1 — P0 修复

### T1.1 修复「新会话播种」路径下工具调用失效

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（单测）— 待 T4.7 e2e 验收 |
| 优先级 | **P0** |
| 来源 | update.md §2.1、§2.5（同源） |
| 预估 | M（1–2 天） |
| 依赖 | 无（但 T2.3 的标签常量化会简化本任务） |

**背景/证据**：`E2E C1` 失败——直连基线输出了 `TOOL_CALL`，bridge 未解析出 `tool_calls`，模型改为凭自身知识编造实时天气（`"北京今天（2026年10月6日）天气晴朗…bj.weather.com.cn"`）。`C2` 同样失败（流式 `finish_reason=stop`）。两条失败用例都走**播种**路径（新桶 → `needs_seed=True`），成功的直连基线走**增量**路径。

**目标**：让「客户端传了 `tools`」时，bridge 在**播种与增量两条路径**都能稳定拿到 `tool_calls`。

**实施要点（按证据强度排序，建议逐条 A/B 实测）**：
1. **注入顺序**：`chatgpt_web/prompting.py:build_prompt`
   - 增量路径：`parts=[用户任务]` 后 `insert(1, format_tools_instruction(...))` → 工具说明紧贴任务之后；
   - 播种路径：`parts=[上下文重建头, 格式强调块, 工具说明, 环境说明, …历史…]` → 工具说明被顶到第 3 位。
   - 候选做法：把 `format_tools_instruction` 与 `format_tool_call_emphasis` 放到**用户任务之后**，或在任务前**再重复一次**简短强制块。
2. **单次纠偏重试**：当 `request.tools` 非空且 `tool_choice != "none"`，而首轮回复不含 `TOOL_CALL` 时，追加一次更强硬的指令重发（仅一次），避免把纯文本当最终答案返回给 Pi/Codex。
3. **不要**用「解析更宽松」来蒙混：C1 的回复里**根本没有** `TOOL_CALL` 标记，属模型未调用，不是解析问题。

**验收标准**：
- [ ] `... tests/e2e/test_parity.py -k "c1 or c2" -v -s` → 两条均 PASSED（连续 2 次运行，见 T4.7）。
- [ ] 新增单测：在 `seed=True` 且带 `tools` 时，断言注入块顺序满足新约定（例如「工具说明出现在用户任务之后」或「任务前存在强制块」）。测试必须断言**新行为**，不得为了通过而放宽。
- [ ] `.venv/bin/python -m pytest -q` 全绿，且既有 `tests/test_prompting.py` / `tests/test_seed_prompt.py` 的断言未被削弱。
- [ ] 在 update.md §2.1 记录：改动前后 e2e C1/C2 的实测结果对比。

**风险/说明**：模型输出具非确定性。若单次修复后仍偶发，应把「首轮无工具 → 纠偏重试」作为兜底，而不是靠多次重跑碰运气。改 `build_prompt` 会同时影响 `/v1/chat/completions`、`/v1/responses`、播种、任务快照——回归面广，务必跑全量单测。

### T1.2 ~~修复会话分桶串台~~（WONTFIX）

| 项 | 内容 |
| --- | --- |
| 状态 | `WONTFIX`（用户决定，2026-10-06） |
| 优先级 | ~~P0~~ |
| 来源 | update.md §2.2 |
| 预估 | — |
| 依赖 | ~~T0.1~~ |

**关闭理由**：同 T0.1——跨桶隔离不是设计需求；不修代码、不新增跨桶隔离单测。

**替代验收口径**：以「同一任务上下文一致」为准——`d1`（同桶两轮记忆）、`d3`（reset 后播种仍记得）必须保持绿色（见 §8）。

**背景/证据**：见 T0.1。若 T0.1 判定为串台，则属**不同 Agent/任务上下文互相污染**，是最高优先级的数据正确性问题。

**目标**：不同会话桶绝不共享网页会话上下文；`D2` 通过。

**实施要点（按 T0.1 结论取舍）**：
- 若「请求头未被采纳」：修 `chatgpt_web/server.py:_session_key` 的优先级/别名解析，并在 `chat_completions` / `responses` 两条路由上补测。
- 若「LRU 换页复用了同一会话」：修 `chatgpt_web/page_pool.py:_ensure_page` —— 明确「换页=新会话」与「状态保留」的语义，必要时在换页后强制 `_open_new_chat` 并校验成功（当前 `_open_new_chat` 失败会静默沿用当前页）。
- 若「判定为模型幻觉」：见 T0.1 的风险说明处理。

**验收标准**：
- [ ] `... tests/e2e/test_parity.py -k d2 -v -s` → PASSED，且 `d1`、`d3` 仍 PASSED（不回归）。
- [ ] 新增单测：用假 driver / 假 page 断言「不同 bucket → 不同 `_page_for()` 结果」与「换页后 `has_history=False` 触发播种」。
- [ ] `/healthz` 能满足 T1.2 的可观测需求：能看到每个 bucket 的页面/会话绑定关系（可与 T3.4 合并交付）。
- [ ] update.md §2.2 更新为「已确认并修复」，附前后证据。

### T1.3 `edit_markdown` 路径沙箱与写入门槛

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | **P0** |
| 来源 | update.md §3.1 |
| 预估 | M（0.5–1 天） |
| 依赖 | 无 |

**背景/证据**：`chatgpt_web/toolcalls.py:115 execute_edit_markdown` 对模型给出的 `path` 只校验「非空字符串」；`server.py:325` / `responses.py:245` 在 `EDIT_MARKDOWN_LOCAL=true`（`.env` 实际为 true）时自动注入该工具。模型被注入后即可**以服务进程权限覆盖本机任意文件**。

**目标**：本地 `edit_markdown` 只能作用于受允许的目录，且落盘需显式开启。

**实施要点**：
1. 新增配置 `EDIT_MARKDOWN_ROOT`（默认项目根）与 `EDIT_MARKDOWN_WRITE`（默认 false）。
2. 在 `execute_edit_markdown` 中：`resolved = Path(path).resolve()`，拒绝绝对路径与含 `..` 的段，并要求 `resolved.is_relative_to(ROOT)`，否则返回 `{"ok": false, "error": "路径越界"}`。
3. `write=true` 仅在 `EDIT_MARKDOWN_WRITE=true` 时生效，否则降级为 dry-run 并提示。
4. 同步更新 `.env.example`、README 配置表与「安全」一节（否则 `tests/test_config_drift.py` / `test_doc_sync.py` 会失败——这是设计如此）。

**验收标准**：
- [ ] 新增单测：`path="../../etc/passwd"`、`path="/tmp/x.md"`、`path="<root 内合法相对路径>"` 三种输入的返回分别为「拒绝 / 拒绝 / 正常 dry-run」。
- [ ] 单测：`EDIT_MARKDOWN_WRITE=false` 时即便 `write=true` 也不落盘（可断言文件 mtime/内容不变）。
- [ ] `.venv/bin/python -m pytest tests/test_config_drift.py tests/test_doc_sync.py -q` 通过（`.env.example` 与 README 已同步补齐新键）。
- [ ] 全量 `pytest -q` 通过。

---

## 4. M2 — P1：E2E harness 与一致性

### T2.1 ~~E2E harness 端口不再被环境变量 `PORT` 污染~~（WONTFIX）

| 项 | 内容 |
| --- | --- |
| 状态 | `WONTFIX`（用户决定，2026-10-06） |
| 优先级 | ~~P1~~ |
| 来源 | update.md §2.3 |
| 预估 | — |
| 依赖 | 无 |

**关闭理由（用户原话）**：`PORT` 已在 `.env` 中定义为 `8002`，**无须重新定义或验证**；因此不新增端口解析函数、不加 `PORT > 0` 校验、不新增端口相关测试用例。

**运行时约定（取代原验收标准）**：跑 e2e 时如宿主 shell 自带 `PORT`（历史事故里是 `PORT=0`），在上命令前显式带 `E2E_PORT=8002` 覆盖即可；仓库 `.env` 的 `PORT=8002` 是唯一端口定义源。

**历史证据（留档）**：宿主环境自带 `PORT=0`，而 `config._load_env_file` 不覆盖已存在的真实环境变量 → 测试进程里 `config.PORT == 0` → 以 `--port 0` 启动 uvicorn（实绑随机端口），却轮询 `http://127.0.0.1:0/healthz`，90s 超时（首次运行 4 failed / 1 passed / 19 errors）。

### T2.2 bridge 子进程在启动失败时被回收

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | **P1** |
| 来源 | update.md §2.4 |
| 预估 | S–M |
| 依赖 | 无（与 T2.1 同属 harness 加固，建议一起做） |

**背景/证据**：`unittest` 在 `setUpModule` 抛错时**不调用** `tearDownModule`，而 `ensure_started()` 超时抛 `RuntimeError` 时没有先杀自己启动的 uvicorn → 残留进程各自带一棵持有 `user_data` 的有头 Chromium，导致其后 `test_new_chat_probe`、`test_prompt_submit_e2e` 全部以「profile 已被另一个 Chromium 实例占用」假失败（实测残留 PID 99382 / 99574）。

**目标**：任何失败路径都不残留 bridge / Chromium / profile 锁，单点故障不再扩散。

**实施要点**：
1. `BridgeServer.ensure_started()` 超时分支：先 `terminate()`→`wait()`→`kill()` 再抛异常。
2. 用 `addModuleCleanup` / `try/finally`（或 pytest session fixture）保证回收。
3. 兜底：`pkill -f 'uvicorn chatgpt_api_server'` + 等待端口释放 + 按需清理残留 `Singleton*`。
4. 需要独占 profile 的模块统一改用独立 profile（参考 `test_parity._copy_profile`）。

**验收标准**：
- [ ] 人为让 `ensure_started` 失败（例如占用端口/把 `STARTUP_TIMEOUT_S` 调成 1），断言进程被回收且端口释放。
- [ ] 失败后再跑 `test_prompt_submit_e2e.py -k single -v -s` → PASSED（不再因 profile 被占用而失败）。
- [ ] 运行后 `pgrep -fl 'uvicorn chatgpt_api_server'` 无输出。

### T2.3 注入块标题常量化（修去重与泄漏检测）

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | **P1** |
| 来源 | update.md §3.2 |
| 预估 | S |
| 依赖 | 无（T1.1 受益于此） |

**背景/证据**：`prompting.py:247` 用 `"[工具调用说明]"`、`:256` 用 `"[edit_markdown 说明]"` 去重，但真实注入块首行是 `"[Tool Calling Instructions]"`（`toolcalls.py:206`）与 `"[edit_markdown notes]"`（`toolcalls.py:105`）——永不相等，**去重恒不生效**；`tests/e2e/test_parity.py:270` 的泄漏检测 token 同样匹配不到真实块，该断言形同虚设。

**目标**：去重判断与泄漏检测都基于同一份真实标题常量。

**实施要点**：
1. 在 `toolcalls.py` 导出常量，例如 `TOOLCALL_HEADER = "[Tool Calling Instructions]"`、`EDIT_MD_HEADER = "[edit_markdown notes]"`，生成块与判定都引用它。
2. `prompting.py` 的去重改为引用常量；`tests/e2e/test_parity.py` 的 `assert_no_injection_leak` 的 token 列表改为引用真实 head 常量（含 `[上下文重建]`、`[任务状态]` 等已有的中文头）。
3. 顺带确认 `format_tool_call_emphasis()` 的插入位置与去重逻辑不冲突（见 T1.1）。

**验收标准**：
- [ ] 新增单测：当入站 system 消息已含真实 head 时，`build_prompt` **不再**重复追加同一块（`prompt.count(HEADER) == 1`）。
- [ ] 新增单测：注入块真实 head 出现在 prompt 中（回归「生成块与判定同源」）。
- [ ] `tests/test_prompting.py`、`tests/test_toolcalls.py`、`tests/test_seed_prompt.py` 全通过。

### T2.4 修正 `_bucket_busy` 的语义

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | **P1** |
| 来源 | update.md §3.3 |
| 预估 | S |
| 依赖 | 无 |

**背景/证据**：`page_pool.py:78` 用 `self._lock_for(bucket).locked()` 判断桶忙；而 `_lock_for`（`:32`）在 `PARALLEL_BUCKETS=false` 时对任何桶都返回全局 `self.lock` → 只要**任意**桶在跑，所有桶都算忙。后果：`responses.py` 流式预检查把「别的桶在跑」误报成「本桶忙」并立刻 503（绕过 `BUCKET_LOCK_TIMEOUT_S` 的排队语义）；`_evict_session_cache` / LRU 换页在请求期间永远选不出候选。

**目标**：忙闲判断反映**真实桶**，而非全局锁状态。

**实施要点**：`_bucket_busy(bucket)` 改为基于 `self._active_buckets`（`_session_lock` 在持锁期间精确维护），并把 `_bucket_busy` 提升为公开 API 供 `responses.py` 使用（不再跨模块调私有方法）。

**验收标准**：
- [ ] 新增单测：`PARALLEL_BUCKETS=false` 时，仅当 bucket 在 `_active_buckets` 中才返回 True；另一个桶在跑时，当前桶返回 False。
- [ ] 现有 `tests/test_sessions.py`（含缓存淘汰用例）全通过。
- [ ] 单测：LRU 换页在「别的桶忙、本桶空闲」时能选出候选。

---

## 5. M3 — P1/P2：可观测性与加固

### T3.1 日志体系与异常可见性

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | P1 |
| 来源 | update.md §3.4 |
| 预估 | M |
| 依赖 | 无（T1.2 的可观测字段可并入本任务交付） |

**背景/证据**：库代码 **57** 处 `print`（`chat_io.py` 22、`completion.py` 15、`server.py` 8 …）、**60** 处裸 `except Exception`，多处 `except Exception: pass`（状态落盘、任务快照落盘），失败静默，排障只能靠 stdout + `CHATGPT_DEBUG=1`。

**目标**：分级日志 + 请求级上下文 + 消除静默失败。

**实施要点**：
1. 引入标准 `logging`：按模块建 logger；`CHATGPT_DEBUG=true` → DEBUG，否则 INFO。
2. 保留现有中文提示语（用户按 README 对照），但改由 logger 输出。
3. 每个请求生成 `request_id`（并回传在响应头或日志中），多 Agent 并发时可区分。
4. 「本应不失败」的落盘点（`_save_session_state`、`tasks.record`、`markdown_io.backup_md`）改为至少 `logger.warning(exc_info=True)`；DOM 探测类可保留静默但降为 debug。
5. `/healthz` 增加「每个 bucket → 页面/会话」映射（配合 T1.2）。

**验收标准**：
- [ ] 新增单测：状态文件写入失败（只读目录 / mock 抛错）时会产生 warning 级日志，且不再静默。
- [ ] 单元测试运行期间日志可按 `CHATGPT_DEBUG` 切换级别（断言 caplog 记录数）。
- [ ] `grep -c 'print(' chatgpt_web/*.py` 的计数显著下降（目标：库代码 0 处业务 print）。
- [ ] 全量 `pytest -q` 通过。

### T3.2 非回环暴露的告警与文档声明（**不引入 API Key**）

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | P2 |
| 来源 | update.md §3.5 |
| 预估 | S |
| 依赖 | 无 |

**前提（设计决定，不可违反）**：本项目的核心设计需求就是**零配置、不需要 API Key**——客户端（Pi / Codex 等）只要把 `base_url` 指向 `http://127.0.0.1:8002/v1`、`api_key` 随便填即可。因此**禁止**新增 `API_KEY` / `Authorization: Bearer` 之类的鉴权；本任务只解决「暴露到非回环地址时用户无法察觉」这一点。

**背景/证据**：`HOST` 默认 `127.0.0.1`（安全），但改成 `0.0.0.0` 后所有端点（含 `/debug/dom`、`/session/reset`）对外裸奔，任何能访问该地址的人都可以借登录态与 ChatGPT 配额发起请求。既有可选防护只有 `/session/reset` 的 `RESET_TOKEN`（默认空 = 不校验），属既有行为，不是新增鉴权。

**目标**：非回环暴露时用户能立刻察觉，且文档明确该用法不受支持；同时把「无鉴权」这一设计决定固定下来，防止被后续改动误加。

**实施要点**：
1. 启动时若 `HOST` 为非回环地址（不是 `127.0.0.1` / `::1` / `localhost`），打印醒目告警：「本服务不提供鉴权，非回环暴露会让任何能访问该地址的人使用你的 ChatGPT 账号，请仅在本机使用」——复用 `completion._warn_if_blocked` 的提示风格，**不阻断启动**。
2. README「安全」节 + `.env.example` 明确声明：服务设计为仅监听 `127.0.0.1`；需要远程访问请用 SSH 端口转发 / VPN 等外部手段，**不要**期望在服务内加 key。
3. **不要**改动 `/session/reset` 的 `RESET_TOKEN` 语义（保持「留空 = 不校验」）。

**验收标准**：
- [ ] 新增单测：`HOST=0.0.0.0` 时启动输出告警；`HOST=127.0.0.1` 时**不**告警。
- [ ] 新增回归测试（锁定设计决定）：无任何认证头请求 `/v1/models` 与 `/v1/chat/completions` **不得**返回 401/403。
- [ ] `grep -rn 'API_KEY' chatgpt_web/` 为空（确认没有引入鉴权配置）。
- [ ] README 与 `.env.example` 已声明非回环不受支持；`tests/test_config_drift.py` / `tests/test_doc_sync.py` 通过。
- [ ] `RESET_TOKEN` 行为未被改动（既有用例保持通过）。

### T3.3 同步文件 I/O 转线程 + 原子写

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | P1 |
| 来源 | update.md §3.6 |
| 预估 | M |
| 依赖 | 无 |

**背景/证据**：`session_store._save_session_state`（每轮写整个状态文件）、`tasks.record`、`chat_io.save_extracted_files`、`toolcalls.execute_edit_markdown` 都在协程内同步执行且无 `await`，大文件时卡住事件循环 → 影响 SSE keep-alive 与其它桶响应。

**目标**：I/O 不阻塞事件循环；状态文件原子替换，崩溃不损坏。

**实施要点**：
1. 用 `await asyncio.to_thread(...)` 包住上述落盘/读取。
2. 状态文件写入改为「临时文件 + `Path.replace`」（`markdown_io.write_md` 已是此模式，可复用）。
3. `_read_state_file` 对损坏文件的容错保持（当前返回 `{}`，会丢全部桶状态——原子写后可保留；若仍解析失败，应记 warning 而非静默）。

**验收标准**：
- [ ] 新增单测：并发触发两次状态保存（不同桶）后，状态文件仍是合法 JSON 且**两个桶的状态都在**（回归「后写覆盖前写」）。
- [ ] 新增单测：写入过程中抛错不会损坏原文件（可用 mock 让 `replace` 前的写入失败）。
- [ ] 全量 `pytest -q` 通过。

### T3.4 选择器更新与 `/healthz` 选择器自检

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（代码/单测）— 真实 DOM 复校随 T4.7 |
| 优先级 | P1 |
| 来源 | update.md §2.5、§2.6 |
| 预估 | M |
| 依赖 | 无（需真实登录跑探测脚本） |

**背景/证据**：
- `SEND_BUTTON_SELECTORS` 四条**全部 0 命中**（`button[data-testid="send-button"]`、`button[aria-label="Send prompt"]`、`button[aria-label*="Send"]`、`button[type="submit"]`）→ `chat_io._click_send_button` 永不可能成功（当前靠真实键盘 Enter 兜住）。
- `test_dom_probe` 自身的「输入文本后枚举按钮」步骤用 `page.query_selector(...).click()`，而 Playwright 判定 composer `not visible` → 30s 超时、探测中断，拿不到「有输入时」的 DOM。
- `NEW_CHAT_SELECTOR` 建议按 `[data-testid="create-new-chat-button"]` 优先（当前 aria-label 在前）。

**后续 2（2026-10-06，线上回归修复，见 update.md §2.10）**：真实网页改版导致
`RESPONSE_SELECTORS` 全面失效（助手回复换到 `[data-markdown-text-style]` /
`[class*="MarkdownRoot"]`，`data-message-author-role` 已不存在），且
`generating` 因侧边栏标题类名 `stopAtEnd-<hash>` 而恒为 True、代码块由
`pre > code` 换成 `div.CodeBlock-*` + `[data-language]`。已三处同步新选择器、
收紧 stop 词口径、新增零节点一次性诊断；真实网页已实测通过（驱动级 + 两路 HTTP）。
`tests/e2e/test_dom_probe.py` 同步新增「回复节点选择器命中数 + 助手节点结构」校准段，
下次改版只需复跑该探测。

**后续 3（2026-10-06，线上使用反馈，见 update.md §2.11）**：工具**空输出**会被模型读成
「命令没生效」而反复重发同一条命令（用户侧看到同一条命令死循环）。已把空输出改为显式说明
（已完成、无输出、请给下一条指令）+ 工具说明新增规则 + 同一命令重复时点名
（`toolcalls.format_repeat_call_hint`）。空输出不能靠「不发新 prompt」绕过：HTTP 必须有响应，
且空输出在 agent 流程里是最常见的成功形态（`git add` / `mkdir` / 干净的 `git status`……）。

**后续 1（2026-10-06，线上回归修复，见 update.md §2.9）**：真实运行暴露两个旧实现的盲区——
① `_open_new_chat` 用 `wait_for_selector`，只认「第一个匹配且可见」，而现网侧边栏会先匹配到
当前会话项（`aria-current="page"`）或折叠态零尺寸节点，导致有按钮也整轮超时；
② Think pill 只靠 `.__composer-pill` 类名，改版即失效。已改为「attached 轮询 + 候选排序 +
JS click 兜底」与「文本扫描兜底」，并补 8 条单测；真实 DOM 复校仍随 T4.7。

**目标**：选择器与真实 DOM 对齐，并让「选择器失效」可被持续观测。

**实施要点**：
1. **先修探测脚本**：改用 bridge 已验证的方式（`state="attached"` + JS `focus()` + `keyboard.insert_text`），复用 `chat_io._call_fill` / `_clear_input` 的思路。
2. 重跑探测拿到「有输入时」的按钮列表，更新 `config.SEND_BUTTON_SELECTORS`（`.env.example` 同步）。
3. `NEW_CHAT_SELECTOR` 把 `[data-testid="create-new-chat-button"]` 提到最前（`.env.example` 同步）。
4. 新增自检端点 `GET /_debug/selectors`（受 `CHATGPT_DEBUG` 控制），返回每条配置选择器的命中数；或在 `/healthz?deep=1` 暴露 `selector_health` 字段。

**验收标准**：
- [ ] 探测脚本能完整输出「输入文本后」的发送按钮候选（不再 30s 超时）。
- [ ] `SEND_BUTTON_SELECTORS` 至少有一条在真实首页**命中 ≥1**；记录探测证据。
- [ ] `chat_io._click_send_button` 有单测覆盖「按候选顺序返回第一个命中的按钮」。
- [ ] `/healthz`（或 `/_debug/selectors`）返回每条选择器的命中数，且在 `CHATGPT_DEBUG=false` 时关闭。
- [ ] `tests/test_config.py::test_selectors_are_chatgpt_not_deepseek` 等既有选择器断言仍通过。

---

## 6. M4 — P2：工程化与优化

### T4.1 pyproject + ruff/mypy + 最小 CI

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06，用户决定「保留并合并」原有 `uv init` 骨架；CI 未推送验证） |
| 优先级 | P2 |
| 来源 | update.md §3.9 |
| 预估 | M |
| 依赖 | 建议在 T1.x/T2.x 之后做，避免 CI 一开始就红 |

**背景/证据**：`requirements.txt` 仅 5 行且全部未固定版本；无 `pyproject.toml`、无 dev 依赖声明（测试需 `pytest`，`fastapi.testclient` 需 `httpx`）、无 CI、无 lint/类型检查。此类工具本可提前发现：`_resolve_session` 死代码、T2.3 的标签不一致、`ChatMessage.content: Optional[Any]` 泛滥。

**目标**：依赖可复现、质量门禁自动化。

**实施要点**：
1. `pyproject.toml`：项目元数据 + 固定带上下界的运行期版本（如 `fastapi>=0.115,<1`）+ dev extra（`pytest`、`httpx`、`ruff`、`mypy`）+ `[tool.pytest.ini_options]`（`testpaths=tests`、`addopts=-q`）。
2. `ruff` 配置（行宽、`select` 规则集）与 `mypy` 配置（可先 `ignore_missing_imports`，逐步收紧）。
3. 最小 CI：`ruff check` + `mypy chatgpt_web` + `pytest -q`；e2e **默认 skip**，不进入常规 CI。
4. 注意：仓库根目录已存在一份疑似 `uv init` 生成的 `pyproject.toml`（**非本次任务产物**），实施前先与用户确认是保留还是合并，避免覆盖他人在途改动。

**验收标准**：
- [x] `pip install -e '.[dev]'`（或冻结的 lock）后 `pytest -q` 可跑；`.venv` 之外无需额外手工装包。→ 实测 `pip install -e '.[dev]'` 成功，随后 `pytest` → 307 passed / 24 skipped。
- [x] `ruff check .` 与 `mypy chatgpt_web` 有明确的通过/豁免清单，且豁免项有注释说明原因（不得静默忽略）。→ `ruff check .` 全过（唯一 per-file 豁免：`driver.py` 的 `E402`，配置内有原因）；`mypy chatgpt_web` → 0 error（豁免：4 个 Mixin 模块的 `attr-defined`，配置内有原因；另修 7 处真实类型问题）。
- [ ] CI 配置在 PR 上能跑通单测；e2e 不在常规 CI 内。→ `.github/workflows/ci.yml` 已建（三条命令本地均通过、e2e 默认 skip），但**未 push，PR 运行未验证**。

### T4.2 结束判定状态机抽取 + `_complete_text` 优化

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | P2 |
| 来源 | update.md §3.10 |
| 预估 | M–L |
| 依赖 | 无（但改动 `chat_io` 需重跑 e2e A/B/D 组） |

**背景/证据**：`chat_io._send_chat_locked` 轮询循环约 200 行、嵌套 4 层；`_complete_text`（`chat_io.py:267`）**每轮**都 `cloneNode(true)` + 屏幕外挂载再读 `innerText`（仅为绕过逐 token 显现动画）。E2E 实测未显示变慢（比值 ≤1.0），故属**优化**而非缺陷。

**目标**：判定逻辑可穷举测试；无动画时不做昂贵的 DOM 克隆。

**实施要点**：
1. 把「是否仍在生成 / 是否落定 / 静默计数 / 卡死计数 / 超时延长」抽成纯函数或小状态机（输入 `generating / normalized / last / pending` → 输出 `continue | finish | extend | fail`）。
2. `_complete_text`：先检测节点内是否存在 `.animating/.pending`，**没有动画时直接 `inner_text()`**；仅必要时才走 clone 流程。
3. 顺带评估 `_page_shows_context_limit` 的 `document.body.innerText` 成本（已有 `CAP_CHECK_EVERY` 节流）。

**验收标准**：
- [ ] 新增状态机单测，覆盖：生成中不结束 / 停止按钮消失但内容仍在变 / 分段输出恢复 / 静默窗口满足后结束 / 卡死快速失败 / 超时但仍在生成则延长 / 延长额度用尽仍超时。`tests/test_end_detection.py` 的既有场景必须全部覆盖，不得减少。
- [ ] `_complete_text` 单测：无 `.pending/.animating` 时**不触发** clone（可用 mock 断言 evaluate 调用次数/参数）。
- [ ] e2e A1/A2/A4（内容对等、长文尾哨兵）与 D1/D3 仍 PASSED，耗时比值仍 ≤1.0。

### T4.3 E2E 软失败指标化（不再用 skip 掩盖模型不配合）

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（代码）— 运行效果待 T4.7 观察 |
| 优先级 | P2 |
| 来源 | update.md §2.7 |
| 预估 | S–M |
| 依赖 | T1.1（同源问题，建议一起观察） |

**背景/证据**：`test_markdown_io_e2e` 的 `test_e2e_model_edit_dry_run` 与 `test_e2e_edit_keeps_fences_paired` 在「模型没返回 `edit_markdown`」时直接 `skipTest` → **6 条里 2 条被静默跳过**，面板显示「全绿」，但最有价值的「模型 → 结构化编辑」链路未被验证。
（后续收敛：`test_e2e_edit_keeps_fences_paired` 与另外 3 条纯本地用例因效果被覆盖而删除，联网用例只剩 `test_e2e_model_edit_dry_run`——见 doc/e2e_test_design.md §7。）

**目标**：「模型不按约定调用工具」作为**可见的失败指标**，与真正「不适用」区分开。

**实施要点**：
1. 增加分类：`不适用（环境/上游）` vs `模型未按约定输出（软失败）`，后者计入汇总（例如模块级计数器 + `tearDownModule` 打印，或直接 `subTest` + 标红）。
2. 可先允许**一次带更强指令的重试**（与 T1.1 的单次纠偏重试共用同一条加固指令），重试后再判定。
3. 保留「上游确实不支持」时的 skip 通道，但必须连同样的强化重试一起失败才 skip。

**验收标准**：
- [ ] 「模型未返回 edit_markdown」时，测试输出可被明确识别为软失败（不再只显示 SKIPPED）；在汇总里可见。
- [ ] 当模型按约定返回时，用例 PASSED（证明软失败通道不会把成功也标红）。→ 收敛后联网用例只剩 1 条（`test_e2e_model_edit_dry_run`，见 doc/e2e_test_design.md §7）；该项仍待 T4.7 e2e 验证。
- [ ] update.md §2.7 更新记录改法。

### T4.4 文档死链修复 + 重建 `doc/e2e_test_design.md`

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | P2 |
| 来源 | update.md §4 |
| 预估 | M |
| 依赖 | 无（建议尽早，T2.1/T2.2 的验收依赖该文档的前置条件说明） |

**背景/证据**（`700bb67` 删除了整个 `doc/`，引用未同步）：

| 引用位置 | 指向 | 现状 |
| --- | --- | --- |
| `README.md:238,239,271` | `doc/design.md`、`doc/tasks.md` | 已删除（本文件即为 `doc/tasks.md` 的重建） |
| `chatgpt_web/responses.py:7` | `doc/codex_support.md` | 从未被 git 跟踪 |
| `chatgpt_web/markdown_io.py:3` | `doc/update.md` | 旧设计稿已删；已重新落位为分析/建议文档 |
| `tests/test_sessions.py:1`、`tests/test_responses.py:1` | `doc/update_codex.md` | 已删除 |
| `tests/e2e/__init__.py:3`、`test_parity.py`、`direct.py` | `doc/e2e_test_design.md` | 已删除（本次 e2e 结论依赖它） |

**目标**：所有 doc 引用可达；e2e 判定矩阵与前置条件单独成文。

**实施要点**：
1. 新建 `doc/e2e_test_design.md`：判定矩阵（直连为准基线 / 双侧不达标=SKIP / 单侧不达标=FAIL）、gating 开关（`CHATGPT_E2E`、`E2E_HEADED`、`E2E_PORT`、`E2E_FULL`）、profile 独占与 `E2E_PORT` 必需性、以及本次暴露的 harness 陷阱。
2. 逐条修引用：`responses.py:7`、`markdown_io.py:3`、`tests/test_sessions.py:1`、`tests/test_responses.py:1`、README 的「项目结构 / 状态」段落。
3. `markdown_io.py` 的 docstring 指向本文（`doc/update.md`）不合适——改为指向 README 或模块自身说明。

**验收标准**：
- [ ] 全仓库不存在指向不存在文件的 `doc/*.md` 引用：`grep -rn 'doc/' --include=*.py --include=*.md . | grep -v '.venv'` 逐条人工核对通过。
- [ ] `doc/e2e_test_design.md` 存在且覆盖 T4.4 实施要点 1 的全部条目（判定矩阵 + gating 开关 + profile 独占/`E2E_PORT` 前置 + harness 陷阱）。
- [ ] `README.md` 的项目结构与当前实际文件一致。

### T4.5 清理死代码与重复实现

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | P2 |
| 来源 | update.md §3.7 |
| 预估 | S |
| 依赖 | 无（T4.1 的 lint 也可发现同类问题） |

**背景/证据**：`responses.py:280 _resolve_session()` 无任何调用方；`responses.py:255 _maybe_run_edit_markdown` 与 `server.py:281 _run_local_edit_markdown` 逐行重复。

**目标**：删除死代码，本地 `edit_markdown` 执行收拢为单一实现。

**实施要点**：把本地执行抽到 `toolcalls.run_local_edit_markdown(tool_calls, backup_dir)`，`server` 与 `responses` 共用（与 T1.3 的沙箱改造一起做，避免二次改动）。

**验收标准**：
- [ ] `grep -rn '_resolve_session' .`（排除 `.venv`）无残留。
- [ ] 新增单测覆盖 `run_local_edit_markdown`（含非 `edit_markdown` 调用原样透传）。
- [ ] 全量 `pytest -q` 通过。

### T4.6 `asyncio.get_event_loop()` → `get_running_loop()`

| 项 | 内容 |
| --- | --- |
| 状态 | `DONE`（2026-10-06） |
| 优先级 | P2 |
| 来源 | update.md §3.8 |
| 预估 | S |
| 依赖 | 无 |

**背景/证据**：`chat_io.py:630,795,806` 在协程内使用 `asyncio.get_event_loop()`（仓库 `.venv` 为 Python 3.14）。

**验收标准**：
- [ ] 三处改为 `asyncio.get_running_loop()`。
- [ ] 新增静态断言测试：`chatgpt_web/` 源码不含 `get_event_loop()`（`tests/test_config_drift.py` 式的小型源码检查）。
- [ ] 全量 `pytest -q` 通过。

### T4.7 E2E 稳定性验证（连跑 2–3 次）

| 项 | 内容 |
| --- | --- |
| 状态 | `DEFERRED`—用户决定（2026-10-06）「暂时不跑」e2e；验收未完成 |
| 优先级 | P2（但**T1.1 的验收依赖它**） |
| 来源 | update.md 附录 B |
| 预估 | M（每次约 8–25 分钟，需真实网络） |
| 依赖 | T2.2（否则运行本身不可信；T2.1 已 WONTFIX，运行时显式带 `E2E_PORT=8002` 即可） |

**背景/证据**：本次 E2E 为**单次运行**，C1/C2 存在模型非确定性的可能。

**目标**：区分「已修好」与「碰巧通过」。

**实施要点**：在 T1.1 改动后连跑 2–3 次，逐次记录逐用例结果与耗时比值；把结果表写进 update.md §2.1。

**用户决定（2026-10-06）**：**暂不连跑**。本轮曾在决定前启动过一次完整 e2e 并被主动停止（未形成有效证据）；因此 T1.1 / T3.4 / T4.3 的 e2e 验收一律保持**未完成**，不得以「代码已改 + 单测绿」代替。后续需要补跑时，按 `doc/e2e_test_design.md` 的前置条件显式带 `E2E_PORT=8002`。

**验收标准**：
- [ ] 连续 2–3 次运行中，`c1`、`c2` 全绿；`d1`、`d3`、`a1/a2/a4`、`b1/b2/b4/b6/b8`、`prompt_submit` 3 条全绿。
- [ ] `d2`（跨桶隔离）只作为**观察项**记录（用户判定为非验收项，见 T0.1/T1.2），其失败不得掩盖其它用例的结论。
- [ ] 每次运行的逐用例结果与耗时表被归档（写入 update.md 或单独结果文件）。
- [ ] 若仍有偶发失败，明确标注为「已知不稳定 + 归因」，不得以「重跑通过」结案。

---

## 7. 追踪矩阵（update.md ↔ tasks.md）

| update.md 章节 | 优先级 | 任务 |
| --- | --- | --- |
| §2.1 工具调用播种路径失效 | P0 | T1.1（+ T4.7 验证、T4.3 同源） |
| §2.2 分桶隔离疑似失效 | ~~P0~~ | T0.1 → T1.2，二者均 **WONTFIX**（理由：用户判定“同一任务上下文一致即可，不存在串台，无须验证”；`d1`/`d3` 作为替代门禁） |
| §2.3 E2E 端口被 `PORT` 污染 | P1 | T2.1 → **WONTFIX**（理由：用户决定 `PORT` 以 `.env` 的 8002 为准，不重定义/不校验；运行时用 `E2E_PORT=8002` 覆盖宿主变量） |
| §2.4 `setUpModule` 泄漏子进程 | P1 | T2.2 |
| §2.5 发送按钮选择器落空（+探测脚本 bug） | P1 | T3.4 |
| §2.6 `NEW_CHAT_SELECTOR` 重排 | P2 | T3.4 |
| §2.7 Markdown e2e 用 skip 掩盖 | P2 | T4.3 |
| §2.8 正面结论（A/B/D/提交链路全绿） | — | 作为回归基线，不单列任务（见 §8） |
| §2.9 线上回归：新建对话 / 思考模式按钮找不到 | P1 | T3.4 延伸修复①（已完成，见 update.md §2.9；真实 DOM 复校随 T4.7） |
| §2.10 线上回归：抓不到回复内容（网页版改版） | P0 | T3.4 延伸修复②（已完成，驱动级 + 两路 HTTP 已实测，见 update.md §2.10） |
| §2.11 工具空输出 → 反复重发同一条命令（死循环） | P1 | T1.1 延伸（prompt 侧已完成，含重复点名硬防线，见 update.md §2.11） |
| §2.12 任务已结束后仍追发 prompt（模型被迫再吐新指令） | P1 | T1.1 延伸（已完成：纠偏收窄为「还没调用过任何工具」，见 update.md §2.12） |
| §2.13 超长 TOOL_CALL 行被网页渲染改写 → 调用被丢弃、任务静默结束 | P0 | T1.1 延伸（解析侧兜底已完成 + 诊断日志，见 update.md §2.13） |
| §2.14 工具调用载体改为 ```tool_call 围栏（纯文本行会被静默改坏命令） | P0 | T1.1 延伸（提示词 + 回归用例已完成，真机 A/B 已验证，见 update.md §2.14；生效需重启桥） |
| §3.1 `edit_markdown` 可写任意路径 | P0 | T1.3 |
| §3.2 去重/泄漏标签不一致 | P1 | T2.3 |
| §3.3 `_bucket_busy` 语义错误 | P1 | T2.4 |
| §3.4 日志与异常可见性 | P1 | T3.1 |
| §3.5 非回环暴露缺少告警/文档声明（设计上不引入 API Key） | P2 | T3.2 |
| §3.6 同步 I/O 阻塞事件循环 | P1 | T3.3 |
| §3.7 死代码与重复实现 | P2 | T4.5 |
| §3.8 `get_event_loop()` | P2 | T4.6 |
| §3.9 依赖与工程化 | P2 | T4.1 |
| §3.10 轮询循环 / `_complete_text` | P2 | T4.2 |
| §4 文档死链 | P2 | T4.4 |
| §5 路线图 | — | §1 里程碑（M0–M4） |
| 附录 B 未完成事项 | — | T0.1（判定）、T4.7（稳定性） |

---

## 8. 回归基线（不得回退的既有能力）

> 注：`d2`（跨桶隔离）已由用户判定为非验收项（见 T0.1/T1.2），**不在**下表的门禁范围内；其余条目均为硬门禁。

以下能力在本次 E2E 中已实测通过，**任何改动都不得使其退化**（每次 e2e 验收必须一并确认）。

> 本轮状态：e2e 验收按用户决定暂缓（T4.7 = `DEFERRED`），下表为**待复验**基线；单测基线（`pytest -q` → 358 passed / 17 skipped）已在每次改动后确认。
>
> 另：§2.10（回复节点改版）已由**真实网页**实测通过（驱动级 + `curl /v1/chat/completions` 两路），
> 因此“回复内容提取”这项能力本身已有非 e2e 的实测证据；下表仍是 e2e 口径的待复验门禁。

| 用例 | 保障的能力 |
| --- | --- |
| A1 / A2 / A4 | 内容对等、长文不截断（长度比 ~1.0、中文占比达标）、注入块不泄漏 |
| B1 / B2 | `/healthz`、`/v1/models` 契约 |
| B4 | chat SSE：首块含 `role`、`id`/`created` 唯一、`finish_reason`、`[DONE]` |
| B6 | Responses 命名事件：`sequence_number` 严格递增、`completed.output` 含哨兵 |
| B8 | 官方 `openai` SDK 可直接对接 |
| D1 / D3 | 多轮记忆、`/session/reset` 后播种不丢上下文 |
| prompt_submit ×2 | 多行（含代码块）提交、残留草稿清理（单行是前者的子集，收敛时删除） |
| Markdown IO ×1 + 单测 | 模型 `edit_markdown` → dry-run / 结构健康（联网）；读取字节保真、锚点定位、整块替换保语言标签改由 `tests/test_markdown_io.py` 单测锁定 |

---

## 9. 非目标（本阶段不做）

- **不引入 API Key / Bearer 鉴权**：零配置、免 key 是本项目的**核心设计需求**（客户端 `api_key` 随便填即可）。把「缺少鉴权」当缺陷来补与项目定位冲突；非回环暴露只做启动告警与文档声明（见 T3.2），并加回归测试锁死该决定。
- **不引入完整 Markdown AST 重排**：`markdown_io` 的定位是「围栏安全的行区间编辑」，语义化重排会破坏用户原有格式。
- **不使用官方 ChatGPT API 替代网页驱动**：项目定位即「网页版 → OpenAI 兼容端点」。
- **不在常规 CI 中运行 e2e**：需要真实登录与有头浏览器，只能作为手动/夜间门禁。
- **不追求 `HEADLESS=1`**：实测会被 Cloudflare 挑战页拦截，维持「有头 + `xvfb-run`」的既有约定。
- **不做多进程并发**：`launch_persistent_context` 独占 `user_data`，同机多实例需各自独立的 profile/目录。
