# ChatGPTBridge 项目分析与改进建议

> 更新日期：2026-10-06
> 本版在静态分析之上，新增**真实 E2E 运行结果**（§1）与**由结果推导的建议**（§2）；原静态分析压缩为 §3。
> 基线：`main @ 700bb67`，单测 `pytest -q` → 219 passed / 24 skipped（24 个 skip 即 e2e 用例）。

---

## 0. 摘要与优先级

| # | 优先级 | 问题 | 证据来源 | 位置 |
| --- | --- | --- | --- | --- |
| 1 | **P0** | 「新会话播种」路径下工具调用（function calling）不可靠：模型直接用自身知识作答 | E2E C1/C2 失败 | `prompting.build_prompt` 播种分支 |
| 2 | ~~P0~~ 已关闭 | 会话分桶隔离：桶 B 读到了桶 A 的暗号——**用户判定无须验证、跨桶隔离非设计需求**（§2.2；T0.1/T1.2 = WONTFIX） | E2E D2 失败 | `server._session_key` / `page_pool._ensure_page` |
| 3 | **P0** | 本地 `edit_markdown` 可写任意路径（模型可控） | 静态 | `toolcalls.execute_edit_markdown` |
| 4 | **P1** | E2E harness 端口被环境变量 `PORT` 污染，首次运行 **19 个用例基础设施性失败** | E2E 第一次运行 | `tests/e2e/bridge.py:79` |
| 5 | **P1** | `setUpModule` 失败会泄漏 bridge 子进程，污染其后所有用例 | E2E 第一次运行 | `BridgeServer` 无失败清理 |
| 6 | **P1** | `SEND_BUTTON_SELECTORS` 全部落空（发送按钮兜底链路已死） | DOM 探测 | `config.SEND_BUTTON_SELECTORS` |
| 7 | **P1** | 去重 / 泄漏检测标签与实际注入块不一致，去重逻辑恒不生效 | 静态 | `prompting.py:247,256` |
| 8 | **P1** | `_bucket_busy` 用共享锁状态判断「桶忙」，默认串行模式下语义错误 | 静态 | `page_pool.py:32,78` |
| 9 | **P2** | Markdown e2e 用 `skipTest` 掩盖「模型不按约定调用 edit_markdown」 | E2E 2 skipped | `test_markdown_io_e2e.py` |
| 10 | **P2** | 非回环暴露缺少告警与文档声明（**设计上不引入 API Key**，见 §3.5）；同步文件 I/O 阻塞事件循环；57 处 `print` + 60 处裸 `except` | 静态 | 见 §3 |
| 11 | **P2** | 依赖未固定版本、无 `pyproject.toml`/CI/lint；文档死链 | 静态 | 见 §3、§4 |

---

## 1. 真实 E2E 运行结果

### 运行环境

- macOS（Apple Silicon），`.venv` Python 3.14.5，Playwright Chromium 153.0.8010.12（有头）。
- `user_data/` 存在真实 ChatGPT 登录态；`.env` 实际生效值：`HEADLESS=false`、`PARALLEL_BUCKETS=true`、`MAX_SESSION_BUCKETS=3`。

### 运行命令

```bash
# 注意：必须先停掉占用 user_data 的 bridge（三个用例独占该 profile）
CHATGPT_E2E=1 E2E_HEADED=1 E2E_FULL=1 E2E_PORT=8002 \
  .venv/bin/python -m pytest tests/e2e -v -s
```

> `E2E_PORT=8002` 是**必需**的，原因见 §2.3；此外运行前需 `pkill -f chatgpt_api_server.py`。

### 结果：3 failed / 19 passed / 2 skipped（507s，8 分 27 秒）

> 注：下表是**收敛前**的原始运行记录（用例名保留原样以便对照）。2026-10-06 对套件做了去重收敛：
> 5 条 Markdown 用例 + 1 条提交用例 + 1 个探测模块被删除/合并（逐条依据见 doc/e2e_test_design.md §7）；
> 收敛后的 E2E 用例数为 **17**（24 → 17）。

| 用例 | 结果 | 说明 |
| --- | --- | --- |
| `DomProbe::test_probe_dom` | PASSED | DOM 探测；发现发送按钮选择器全部落空（§2.4） |
| `MarkdownIoE2ETests::test_e2e_read_preserves_bytes` | PASSED | 读取字节保真 |
| `MarkdownIoE2ETests::test_e2e_locate_anchors` | PASSED | 锚点定位 / 围栏内不参与匹配 |
| `MarkdownIoE2ETests::test_e2e_fence_block_replace_keeps_lang` | PASSED | 整块替换保留语言标签 |
| `MarkdownIoE2ETests::test_e2e_render_view_marks_fences` | PASSED | `[[fence:json]]` 视图确实送达模型 |
| `MarkdownIoE2ETests::test_e2e_model_edit_dry_run` | **SKIPPED** | 模型未按约定返回 `edit_markdown`（`skipTest`） |
| `MarkdownIoE2ETests::test_e2e_edit_keeps_fences_paired` | **SKIPPED** | 同上 |
| `NewChatButtonProbe::test_probe_new_chat_button` | PASSED | 首页按钮探测；给出选择器建议 |
| `TestAContentParity::test_a1_sentinel_parity` | PASSED | 哨兵 + OpenAI 响应结构 + 注入块未泄漏 |
| `TestAContentParity::test_a2_fact_parity` | PASSED | 事实对等 |
| `TestAContentParity::test_a4_longform_tail_sentinel` | PASSED | 长文尾哨兵；长度比 1.00、中文占比达标（未截断） |
| `TestBProtocol::test_b1_healthz` | PASSED | |
| `TestBProtocol::test_b2_models` | PASSED | |
| `TestBProtocol::test_b4_stream_sequence` | PASSED | SSE 首块 role / 唯一 id / `finish_reason=stop` / `[DONE]` |
| `TestBProtocol::test_b6_responses_events` | PASSED | Responses 命名事件 + `sequence_number` 严格递增 |
| `TestBProtocol::test_b8_openai_sdk_smoke` | PASSED | 官方 openai SDK 联调（`E2E_FULL=1`） |
| `TestCToolParity::test_c1_tool_call_parity` | **FAILED** | 直连基线产出 `TOOL_CALL`，bridge 未解析出 `tool_calls` |
| `TestCToolParity::test_c2_tool_call_stream` | **FAILED** | 流式 `finish_reason` 为 `stop`，缺 `tool_calls` |
| `TestDSession::test_d1_multiturn_memory` | PASSED | 同桶多轮记忆正常 |
| `TestDSession::test_d2_bucket_isolation` | **FAILED** | 桶 B 返回了桶 A 的暗号 `Tiger-77` |
| `TestDSession::test_d3_reset_then_seed_keeps_context` | PASSED | reset + 播种后上下文仍保留 |
| `PromptSubmitE2ETests::test_single_line_prompt_submits` | PASSED | |
| `PromptSubmitE2ETests::test_multiline_prompt_submits` | PASSED | 核心回归：多行 prompt 走真实键盘 Enter 提交成功 |
| `PromptSubmitE2ETests::test_stale_draft_is_cleared` | PASSED | 残留草稿被清空，未拼进新 prompt |

### 耗时（`tearDownModule` 汇总）

```
a1  direct=22.5s  bridge=13.0s  ratio=0.58
a2  direct=15.8s  bridge=12.7s  ratio=0.80
a4  direct=23.7s  bridge=23.7s  ratio=1.00
c1  direct=24.0s  bridge=18.2s  ratio=0.76
d1  direct=35.0s  bridge=21.2s  ratio=0.60
d3  bridge=33.6s
b4  bridge=13.1s（first_data=0.02s，keepalives=1）
```

**结论**：bridge 相对直连**不存在性能劣势**（比值全部 ≤1.0），流式首字节即返回、keep-alive 生效、长文无截断。核心链路（会话、流式、Responses、Markdown IO、提交、多轮记忆、重置播种）**在真实网页上基本可用**；问题集中在**工具调用**一处（**分桶隔离**已由用户判定关闭，见 §2.2）。

---

## 2. 由 E2E 结果得出的改进建议

### 2.1（P0）工具调用在「新会话播种」路径下失效

**现象**：C1、C2 两条用例都失败，而 C1 的直连基线（同一份 `build_prompt` 注入、同一 `get_weather` 工具）**成功输出了 `TOOL_CALL`**。

**证据**（C1 断言原文）：

```
AssertionError: [] is not true : 直连基线已产生 TOOL_CALL 标记，但 bridge 解析不出 tool_calls
（DOM 提取/解析缺陷）。message={"role": "assistant",
 "content": "北京今天（2026年10月6日）天气晴朗，气温大约 9–24°C，风力较小，降雨概率很低，
 空气质量预报为优。\nbj.weather.com.cn\n+1", "tool_calls": null}
```

即模型**没有调用工具，而是凭自身知识编造了实时天气**（还附了引用）。C2 同样：流式 `finish_reason=['stop']`，无 `tool_calls`。

**关键线索（而非结论）**：两条失败用例都走**播种**路径（`session=e2e-c1/c2` 是全新桶 → `needs_seed=True`），而成功的直连基线走**增量**路径。两条路径的注入顺序不同（`prompting.build_prompt`）：

- 增量路径：`parts = [用户任务]`，随后工具说明 `insert(1, ...)` → **工具说明紧跟在用户任务之后**；
- 播种路径：`parts = [上下文重建头, 格式强调块, 工具说明, 环境说明, ...历史...]` → 工具说明被**上下文重建头 + 强调块**顶到了第 3 位，与最后的用户任务之间还隔着环境说明与历史。

**建议**：
1. **把工具说明与「必须调用」的强制要求同时放到用户任务之后**（利用近因效应），或在播种路径里在任务前**再重复一次**简短强制块；代码注释本身已认定「工具说明放用户任务之前能降低无视工具的概率」，但真实结果与预期相反，值得用 A/B 实测验证顺序假设。
2. 当客户端传了 `tools` 且 `tool_choice` 为 `required`/`auto` 时，若首轮回复**不含** `TOOL_CALL`，**自动追加一次更强硬的纠偏重试**（例如：「你没有 shell/文件系统访问，必须调用工具；直接作答＝失败」），仅重试一次；当前直接返回纯文本，客户端（Pi/Codex）会把它当最终答案。
3. 把 C1/C2 作为**发布门禁**：工具调用是该项目对 Codex CLI / Pi 的核心价值，现在两条主用例常红。
4. 顺带修 §3 里的去重标签不一致——它意味着「工具说明」每次都无条件追加一次，干扰真实 A/B 分析。

### 2.2（已关闭 / WONTFIX）会话分桶隔离——**用户判定为非验收项**

> **处置（2026-10-06，用户决定）**：用户明确要求——“同一个任务必须保持上下文一致，不存在串台；这个无须验证”。因此本节不做判定实验、不修跨桶隔离（tasks.md 的 T0.1 / T1.2 均为 `WONTFIX`），只把「**同一任务（同一会话桶）内上下文一致**」作为验收项（D1 / D3 保持绿）。
>
> D2 用例**保留原样、不做任何削弱**，但**不计入验收门禁**：结果如实记录为「已知差异（用户判定：跨桶隔离非设计需求）」。

**现象（仅存档，不再深究）**：D2 断言 `assertNotIn("Tiger-77", b_text)` 失败，桶 B（全新 session）输出了桶 A 的暗号：

```
AssertionError: 'Tiger-77' unexpectedly found in 'Tiger-77' :
桶 B 看到了桶 A 的暗号（分桶隔离失效/上下文串台）：'Tiger-77'
```

**已确认的相邻事实**：D1（同桶两轮记忆）与 D3（reset 后播种仍记得）**都通过**——即用户关心的「同一任务上下文一致」是成立的。

**被测证据（仅记录，不再复验）**：用 `/tmp/iso_test.py` 向桶 `iso-a` 写入暗号 `ZULU-99` 后，全新桶 `iso-b` 也回了 `ZULU-99`；同时 `/healthz.session_keys` 中出现 `iso-a` / `iso-b`，说明 `X-ChatGPT-Session` 请求头**已被采信**（排除「塌缩成 default 桶」）。机制未继续追查（用户明确无须验证）。

**原建议（未采纳，留档）**：先做判定（头是否采信 + LRU 换页是否复用同一会话）；若确为泄漏，作为最高优先级数据串台修复；并给 `/healthz` 增加「桶→Chat 会话」映射字段。

### 2.3（已关闭 / WONTFIX）E2E harness 的端口被环境变量 `PORT` 污染

> **处置（2026-10-06，用户决定）**：`PORT` 已在仓库根目录 `.env` 中固定为 `8002`，用户明确要求**不再为端口做重定义或校验**，因此本节只保留根因记录，不做代码改动。运行 e2e 时如宿主 shell 自带 `PORT`，用 `E2E_PORT=8002` 显式覆盖即可（harness 侧的进程回收仍按 T2.2 交付）。
>
> 对应的 tasks.md T2.1 状态为 `WONTFIX`。

**现象**：第一次运行（未设 `E2E_PORT`）结果为 **4 failed / 1 passed / 19 errors**，绝大多数是 `RuntimeError: bridge 90s 内未就绪`。

**根因**（已实测确认）：

```
$ printenv PORT           -> 0
$ python -c "from chatgpt_web import config; print(config.PORT)"  -> 0
$ cat $TMPDIR/chatgpt_e2e_uvicorn.log
INFO:  Uvicorn running on http://127.0.0.1:62253      <-- 实际随机端口
```

`tests/e2e/bridge.py` 用 `PORT = os.environ.get("E2E_PORT") or config.PORT` 推导 URL，而 `_load_env_file` **不覆盖已存在的真实环境变量**——宿主环境自带的 `PORT=0` 覆盖了 `.env` 的 `PORT=8002`。于是 harness 以 `--port 0` 启动 uvicorn（OS 随机分配 62253），却一直轮询 `http://127.0.0.1:0/healthz`，90s 后超时。**这是一个能一票否决整套 e2e 的静默陷阱**（本地 shell 通常没有 `PORT`，CI/容器/托管环境几乎都有）。

**原建议（未采纳，留档）**：
- ~~`E2E_PORT` 直接**硬默认 8002**（不回退 `config.PORT`）~~；
- ~~启动前 `assert port and port > 0`~~；
- ~~`config.py` 对 `PORT=0` 给出启动告警~~。

**实际处置**：不改代码。端口以 `.env`（`PORT=8002`）为准；e2e 运行时按 §0.3 的完整命令显式带 `E2E_PORT=8002` 即可规避宿主环境变量干扰。

### 2.4（P1）`setUpModule` 失败会泄漏 bridge 子进程，污染后续所有用例

**现象**：第一次运行中，`test_markdown_io_e2e` 的 `setUpModule` 抛错后，其后 `test_new_chat_probe` 与 `test_prompt_submit_e2e` 全部以「profile 已被另一个 Chromium 实例占用」失败：

```
RuntimeError: 浏览器用户目录 .../user_data 已被另一个 Chromium 实例占用。
```

**根因**：`unittest` 在 `setUpModule` 抛错时**不会调用** `tearDownModule`，而 `BridgeServer.ensure_started()` 超时抛 `RuntimeError` 时**没有先杀掉自己刚启动的 uvicorn**。实测残留进程：

```
99382  python -m uvicorn chatgpt_api_server:app --host 127.0.0.1 --port 0
99574  python -m uvicorn chatgpt_api_server:app --host 127.0.0.1 --port 0
# 各自带一棵持有 ChatGPTBridge/user_data 的有头 Chromium
```

**建议**：
- `ensure_started()` 超时分支里先 `terminate()/kill()` 子进程再抛异常；
- 用 `addModuleCleanup` / `try/finally`（或 pytest 的 session fixture）保证任何路径都回收；
- 加入 `pkill -f 'uvicorn chatgpt_api_server'` 之类的兜底 + 等待端口释放 + 清理残留 `Singleton*`；
- 独立的 profile（如 `_copy_profile` 的做法）应推广到所有需要独占 profile 的模块，避免互相踩。

### 2.5（P1）发送按钮选择器全部落空

**证据**（DOM 探测，真实首页）：

```
-- SEND_BUTTON_SELECTORS --
  'button[data-testid="send-button"]': 0
  'button[aria-label="Send prompt"]': 0
  'button[aria-label*="Send"]': 0
  'button[type="submit"]': 0
```

整条兜底链 0 命中，`chat_io._click_send_button` 在当前网页版**永远不可能成功**。目前靠 `_keyboard_enter`（真实键盘 Enter）提交，故功能不受影响——但「合成事件 → 点发送按钮」的两级兜底实际只剩一级。

**建议**：
- 用探测结果更新 `SEND_BUTTON_SELECTORS`（当前 DOM 里发送按钮很可能是 `composer` 内基于 `<button>` 的图标按钮，建议另跑一次「输入文本后再枚举按钮」的探测——注意探测脚本自身要先修，见下）；
- 加一条 `/_debug/selectors`（或 `/healthz?deep=1`）自检，把每条配置选择器的命中数暴露出来，网页改版时一眼可见。

**附带发现（探测脚本自身的 bug）**：`test_dom_probe` 的「输入文本后观察按钮」步骤用 `page.query_selector(...).click()`，而 Playwright 判定该 composer `not visible`，导致 30s 点击超时、探测中断：

```
输入探测失败: TimeoutError('ElementHandle.click: Timeout 30000ms exceeded ...
 - element is not visible')
```

应改用 bridge 已验证的方式（`state="attached"` + JS `focus()` + `keyboard.insert_text`），否则发送按钮探测拿不到「有输入时」的 DOM。

### 2.6（P2）`NEW_CHAT_SELECTOR` 建议按 `data-testid` 优先重排

探测给出的建议（真实首页）：

```
NEW_CHAT_SELECTOR=[data-testid="create-new-chat-button"]||button[aria-label="New chat"]||...
```

当前配置把 `a[aria-label="New chat"]` 放第一位（命中 1），`[data-testid="create-new-chat-button"]` 命中 2。两者都可用，但 `data-testid` 通常比本地化 aria-label 稳定（历史上 aria-label 改版就失效过）。建议把 testid 提到最前。

### 2.7（P2）Markdown e2e 用 `skipTest` 掩盖模型不配合

`test_e2e_model_edit_dry_run` 与 `test_e2e_edit_keeps_fences_paired` 在「模型没返回 `edit_markdown`」时直接 `skipTest`。结果是**6 条用例里有 2 条被静默跳过**，e2e 面板显示为「全绿」，但 `edit_markdown` 这条最有价值的「模型 → 结构化编辑」链路其实没被验证。

**建议**：把「模型未按约定返回工具」记为**软失败指标**（例如输出 `WARNING` 并计入汇总，或允许一次带更强指令的重试后再跳过），不要与真正的「不适用」混为一谈；这与 §2.1 的工具调用问题同源。

**处置（2026-10-06，T4.3 已实现）**：`tests/e2e/test_markdown_io_e2e.py` 的 `edit_markdown` 联网用例不再 `skipTest`
（同日套件收敛后，该模块只剩 `test_e2e_model_edit_dry_run` 这一条联网用例；被删用例及覆盖依据见 doc/e2e_test_design.md §7）：

1. `generate_edit` 失败后，**追加一次与 T1.1 同源的强化纠偏指令**（`toolcalls.format_tool_retry_nudge()`）重试；
2. 重试后仍拿不到 `edit_markdown` → 记为**软失败**：用例直接 `FAIL`，并计入模块级 `_SOFT_FAILURES`，在 `tearDownModule` 打印 `[E2E 软失败汇总]`（不再显示为 SKIPPED）；
3. 只有 bridge/上游不可用（HTTP 502/503/504、`timeout`/`upstream_error`/`context_length_exceeded`，且强化重试后仍失败）才走 skip 通道（环境问题）。

待验证项（T4.7）：当模型按约定返回时，该用例应 `PASSED`（证明软失败通道不会把成功也标红）；若仍为软失败，则作为可见红灯计入。

### 2.8 值得保留的正面结论

- **内容对等**：A1（哨兵）、A2（事实）、A4（长文尾哨兵 + 长度比 + 中文占比）全绿 → 结束判定没有过早截断，也没有重复/串台。
- **协议正确**：B1/B2/B4/B6/B8 全绿 → chat SSE（首块 role、唯一 id、`finish_reason`、`[DONE]`）与 Responses 命名事件（`sequence_number` 严格递增、`completed.output` 含哨兵）均符合规范，官方 openai SDK 可直接对接。
- **会话连续性**：D1（多轮记忆）、D3（reset 后播种不丢上下文）全绿 → 「每轮重播种 + 任务快照」的设计在真实网页上有效。
- **提交链路**：单行 / 多行（含代码块）/ 残留草稿三条全绿 → 真实键盘 Enter 的修复有效。

### 2.9（已修复）线上回归：新建对话 / 思考模式按钮找不到

**现象（用户实测日志，`CHATGPT_DEBUG=1`）**：启动时 `_open_new_chat` 逐个候选超时
（`wait_for_selector: Timeout 3000ms` × 9），其中 `button[aria-label="New chat"]`
报告 “locator resolved to 2 elements” 仍超时；随后 `_select_think_mode` 报
「未找到思考模式按钮，按默认模式继续」。

**根因**：

1. `completion._open_new_chat` 用 `page.wait_for_selector(selector)`，其默认语义是
   「等**第一个**匹配且**可见**」。现网侧边栏里同一选择器会先匹配到当前会话项
   （`aria-current="page"`）或折叠态的零尺寸节点 → 明明有可点按钮也整轮超时。
   （T3.4 只把**探测脚本**改成了 `state="attached"`，生产代码没同步。）
2. `THINK_MODE_SELECTOR` 完全依赖 `.__composer-pill` 类名；哈希类名改版即失效，
   配置选择器全部落空后没有任何兜底。
3. 次要放大因素：`NEW_CHAT_SELECTOR` 首条 `[data-testid="create-new-chat-button"]`
   在部分版本不存在，旧实现每条约白等 3s。

**修复（`chatgpt_web/completion.py` + 选择器清单）**：

1. `_open_new_chat`：在**总**预算（8s）内轮询 `query_selector_all`（attached 即可），
   跨选择器收集候选并按「可见且非当前项 > 可见 > 非当前项 > 其它」排序；
   真点击失败退回 JS 原生 click；命中即返回，不再逐条白等 3s。
2. `_try_select_think_once`：配置选择器全部落空时，用 `_THINK_FALLBACK_JS` 扫描
   `button, [role="button"]`，按文本（`THINK_MODE_TEXTS`）匹配，优先带
   `aria-pressed`（pill 标志）且可见的节点。
3. `NEW_CHAT_SELECTOR` 新增 `button[aria-label*="New chat"]` /
   `a[aria-label*="New chat"]` 前缀匹配，覆盖工作区版本（如 “New chat in Techtorium”）；
   `.env` 与 `.env.example` 已同步。

**验证**：`tests/test_selectors.py` 新增 8 条单测（候选排序、JS click 兜底、
侧边栏晚渲染轮询、预算内放弃、Think 文本兜底、配置选择器命中时不触发兜底）；
全量 `pytest` → **317 passed / 17 skipped**，`ruff` / `mypy` 干净。
**仍未验证**：真实网页 DOM 下的点击效果（需重启服务复跑，同 T3.4 的“真实 DOM 复校”）。

### 2.10（已修复）线上回归：抓不到服务端回复内容（网页版改版）

**现象（用户实测日志，`CHATGPT_DEBUG=1`）**：发出去的请求一直等不到内容，
轮询从 poll=1 到 46+ 全是 `nodes=0 len=0 stable=0 generating=True saw=True`，
客户端最终拿到空回复（超时）。

**根因（三条，均在真实网页上用只读 DOM 探测实测确认）**：

1. **回复节点选择器全面失效**。现网助手回复容器不再带
   `[data-message-author-role="assistant"]`，`message-content` / `.markdown` /
   `div[class*="response"]` 也全部不存在（逐条命中数都是 0）。回复正文在
   `<div class="MarkdownRoot-<hash>" data-markdown-text-style="assistant-message">`
   （代码块则在其内的 `div.CodeBlock-<hash>`）。
2. **`generating` 恒为 True（误报）**。`_GENERATING_JS` 用裸的 `/stop/i.test(cls)`
   判定类名，而现网**侧边栏会话标题**的类名带 `stopAtEnd-<hash>`（截断样式）：
   这些节点可见且位于视口下半部，于是被当成「停止生成」控件——实测一个**已完成**的
   会话页 80/80 轮都返回 True。后果是结束判定的 `settled` 永远不成立，
   即便回复早就渲染完，也要空转到总超时（默认 180s + 最多 300s 延长）。
3. **代码块不再是 `pre > code`**。新版是 `div.CodeBlock-<hash>` + CodeMirror 的
   `div.cm-content[data-language="python"]`（页面 `pre` / `code` 命中数均为 0），
   语言写在 `data-language`；`_extract_code_blocks` 因此永远抽不到代码块。

**修复**：

1. `RESPONSE_SELECTORS`（`config` 默认 + `.env` + `.env.example` 三处同步）改为
   `[data-message-author-role="assistant"], [data-markdown-text-style], [class*="MarkdownRoot"], message-content, .markdown`
   —— 语义属性优先、老版属性兜底。实测：composer 不带 `data-markdown-text-style`，
   全新对话页上全部命中 0，不会把输入框/空白页当成回复；用户消息容器
   （`[data-user-message-bubble]`）**不**在列表里。
2. `_GENERATING_JS` / `_STOP_CANDIDATES_JS`：类名口径收紧为「**独立的** stop 词」
   `(^|[-_])(stop)([-_]|$)`（大小写不敏感），并把 `data-testid` 一起纳入标签匹配。
   `stopAtEnd-*` 不再命中，`stop-button` / `aria-label="Stop streaming"` / `停止` 仍命中。
3. `CODE_BLOCK_SELECTOR=[class*="CodeBlock"], pre`、`CODE_TAG_SELECTOR=[data-language], code`，
   `_extract_code_blocks` 增加 `data-language` 语言识别（只读正文节点，语言头不会混进代码）。
4. 附带加固：`INPUT_SELECTORS` 第 3 条加 `:not([data-language])`——新版代码块正文也是
   `contenteditable` + `role="textbox"`，不排除的话一旦 composer 选择器落空，
   prompt 会被写进回复正文。
5. 可观测性：`chat_io` 新增**一次性**诊断——连续 3 轮零节点且页面仍在生成时，
   打印每条 `RESPONSE_SELECTORS` 的命中数与页面文本长度，让下次改版一眼可见
   （而不是再从“等到超时”反推）。

**验证（真实网页，2026-10-06 16:13–16:23，有头 Chromium + `user_data` 已登录 profile）**：

| 层次 | 命令 | 结果 |
| --- | --- | --- |
| 驱动级 | `driver.send_chat(...)` | poll=1 零节点 → poll=2 `nodes=1 len=24` → `generating` True→False → poll=6「页面已落定且内容静默 4 次」；回复 `print('pong')`，代码块 `{"lang": "python", "code": "print('pong')"}` |
| HTTP 级（纯文本） | `curl /v1/chat/completions`（`tool_choice=none`） | HTTP 200 / 19.4s，`content="pong"`，6 轮落定 |
| HTTP 级（工具模式） | 同上（`.env` 的 `EDIT_MARKDOWN_LOCAL=true` 会自动注入 `edit_markdown`） | HTTP 200 / 35.2s，返回 `tool_calls`（含 T1.1 纠偏后的第二轮，两轮内容都成功提取） |

修复前同一路径要等到 180s + 延长额度用尽（约 480s）后返回空内容。
单测：`pytest` → **328 passed / 17 skipped**，`ruff` / `mypy` 干净；新增/扩展
`test_config`（新版选择器钩子）、`test_selectors`（stop 词口径、代码块提取、
composer 排除）、`test_end_detection`（零节点诊断一次性 + 首帧不误报）。

**遗留（非本次范围）**：回复文本会把代码块的头部 chrome 一起读进来
（`Python\nRun\nprint('pong')`）——这是「读消息节点 innerText」的既有行为
（旧版同样会带 `Copy` / `Download` 行），代码块提取已改为只读正文节点、不再受影响。

### 2.11（已修复）工具空输出导致模型反复重发同一条命令（死循环）

**现象（用户实测）**：客户端执行 `git status --short` 没有任何输出时，bridge 发给网页版的
prompt 里是：

```
[工具执行结果 call_b17e8c60238e4095]
(no output)
```

模型把「没有输出」理解成「命令没生效 / 工具坏了」，于是把它刚才发过的同一条命令**原样再发
一遍**；客户端再执行一次、又是一样的空输出 —— 用户侧看到同一条命令被反复执行
（实测连续 4 次 `$ git status --short` / `(no output)` / `Took 0.0s`）。

**根因**：空输出只有「客户端渲染出的歧义文字」，没有任何语义说明（`_render_message`
除截断外原样透传 tool 结果）。模型在一个“看起来失败”的信号上无法前进，只能重试。

**关于「干脆不发新 prompt」**：**不可行**。HTTP 请求必须有响应，不给模型下一步客户端
就一直等；而且在 agent 流程里空输出恰恰是**最常见的成功形态**（`git add`、`mv`、`cp`、
`mkdir`、干净的 `git status`……）——跳过等于让每个写操作都卡住。正确做法是「照发，
但把空输出说清楚 + 重复时点名」（即本次修复）。

**修复（全部在 prompt 侧，不改响应契约）**：

1. `prompting._is_empty_tool_output`：识别「空输出等价形态」——空白内容、`(no output)` /
   `(空输出)` 之类空标记，以及客户端外壳行（`$ cmd` 命令回显、`Took 0.0s`、`Wall time:`、
   `Process exited with code 0`、`Original token count: 0`、空 `Output:`、分隔线）。
   只有「除外壳与空标记外什么都不剩」才算空；**非空结果仍逐字节保留**
   （edit 工具 oldText 精确匹配的前提，已用 5 种形态的单测锁定）。
2. `prompting._render_message`：空输出替换为显式说明（`EMPTY_TOOL_RESULT_NOTE`）：
   「本命令已执行完毕、退出正常，但没有任何输出：空输出是有效的正常结果，既不是失败，
   也不代表工具异常或命令未生效。请直接给出下一条指令或最终结论；不要重复执行同一条命令」。
   歧义原文（`(no output)`）不再交给模型。
3. `toolcalls.format_tools_instruction`：新增规则——工具结果为空 = 命令成功且确实没有输出，
   继续下一步；**绝不重发同一条命令**（重复就是死循环）。
4. **重复点名（硬防线）**：`prompting._repeated_empty_calls` 用 `tool_call_id` 把空结果映射
   回发起它的 assistant 调用，再按「命令签名」（工具名 + 规范化 JSON 参数）统计历史出现
   次数；≥2 次时在 prompt 末尾追加 `toolcalls.format_repeat_call_hint`：
   `called 2x, always empty: bash|{"command": "git status --short"}`，并明说再发永远不会
   有输出，要求换命令或直接收尾。

**验证**：`tests/test_prompting.py` 新增 9 条单测（空白 / `(no output)` / Codex 风格空结果
均被解释；5 种非空形态逐字节不变；单次不点名、重复点名、不同命令不点名、非空结果不点名；
工具说明含空输出规则），并把真实场景的整段 prompt 打印出来人工核对。
`pytest` → **337 passed / 17 skipped**，`ruff` / `mypy` 干净。

### 2.12（已修复）任务已结束后仍追发 prompt，模型被迫再吐新指令

**现象（用户实测）**：ChatGPT 已经把任务做完、用纯文本收尾（例如「工作区是干净的，任务完成」），
bridge 却**紧接着又往输入框发了一条 prompt**，于是 ChatGPT 又生成了一条新指令，任务永远收不了尾。

**根因**：T1.1 的工具纠偏（`chat_io.send_chat(validate_reply=...)`）在**任何**工具模式下都会触发：
只要模型这次的回复里没有解析出 `TOOL_CALL`，判定就返回 False → 立刻在同一会话追发
`format_tool_retry_nudge()`。而「没有工具调用」有两种完全不同的含义，旧实现把它们混为一谈：

| 场景 | 真实含义 | 旧行为 | 现行为 |
| --- | --- | --- | --- |
| 本轮任务**一次工具都没调用过**（首轮） | 模型可能完全无视工具、凭自身知识编了个结果（C1/C2 失败） | 追发纠偏 | **保留**（T1.1 的原始目标） |
| 历史里已有 tool 结果 / assistant `tool_calls` | 任务早已进入执行阶段，这次的纯文本是**收尾** | 追发纠偏 → 把结论重新推成一条新命令 | **不追发**，纯文本即最终答案 |

**关于「无指令反馈就等待下一条指令，超时则判定任务结束」**：桥是一个 HTTP 服务，**没有能力
自行收到 ChatGPT 的后续消息**——网页版不会在无人发 prompt 时主动说话，所以「等」在桥侧只有两种
落地方式，二者都已具备：

1. **不主动制造新 prompt**（本次修复）：无指令的回复原样作为最终答案返回，客户端（Codex / Pi）
   看到没有 `tool_calls` 即判定任务结束；「预定时间」由客户端自己的循环负责。
2. **回复可能还没说完时继续等**（既有能力）：模型分段输出、停止按钮短暂消失后**又**补出
   `TOOL_CALL` 的情况，由 `end_detection.evaluate_poll` 的静默窗口（`RESUME_QUIET_POLLS` ×
   `POLL_INTERVAL_S`，默认 ≈6s）继续等到内容出现或静默结束，
   回归测试：`tests/test_end_detection.py::GeneratingStateTests::test_transient_pause_then_tool_call_is_not_truncated`。

**修复内容**：

1. `config.TOOL_NUDGE_UNTIL_FIRST_CALL`（默认 `true`）：纠偏只在本轮任务**还没调用过任何工具**时生效；
   设为 `false` = **完全不纠偏**（桥绝不自行追发任何 prompt，模型没调用工具时直接返回纯文本）。
2. `prompting.has_prior_tool_use(messages)`：历史里出现 `role == "tool"` 或带 `tool_calls` 的
   assistant 消息即认为任务已进入执行阶段（两条路径都覆盖：`/v1/chat/completions` 的原始 messages，
   以及 `/v1/responses` 由 `function_call` / `function_call_output` 转换来的消息）。
3. `prompting.tool_nudge_predicate(messages, tools, tool_choice)`：返回 `None`（不纠偏）或判定函数；
   与三个入口的 `wants_tools = bool(tools) and tool_choice != "none"` 口径一致。
4. 三处接线统一改为 `tool_nudge_predicate(...)`：`server.py`（非流式）、`streaming.py`（chat SSE）、
   `responses.py`（`run_chat`，流式 / 非流式共用）。

**验证**：新增 17 条单测——

* `prompting` 侧（首轮仍纠偏、有 tool 结果 / 有 `tool_calls` 即不纠偏、无 tools /
  `tool_choice="none"` 不纠偏、配置关掉即完全不纠偏）；
* 两条调用链的**接线**断言（`tests/test_streaming.py::NudgePredicateWiringTests`、
  `tests/test_responses.py::RunChatNudgePredicateTests` 直接断言传给 driver 的 `validate_reply`
  是 `None` 还是判定函数）；
* **驱动层**回归（`tests/test_tool_injection.py::ToolRetryTests::test_task_end_sends_no_second_prompt`）：
  用「输入框里最后一次被填充的文本」作为判据，断言收尾场景下桥**没有**再发任何 prompt。
  该用例有真实区分力——把 `validate_reply` 换回旧的 `tool_call_predicate` 会立刻复现故障
  （实测输出「桥又发了一条 prompt: True / 模型被迫吐出新指令: True」）。
`.env` / `.env.example` / README 同步新增该配置项（`tests/test_config_drift.py` 强制模板不落后）。
`pytest` → **354 passed / 17 skipped**，`ruff` / `mypy` 干净。

### 2.13（已修复）超长 `TOOL_CALL` 行被网页渲染改写 → 整条调用被丢弃、任务静默结束

**现象（用户实测）**：模型输出了单行约 5.4 KB 的 `TOOL_CALL: {"name":"bash", …}`，
Pi 却收到一条**纯文本**回复（没有 tool_calls）→ 把回复当成最终答案、**直接结束任务**；
桥的日志里没有任何线索。用户随后把该行贴回来问「为什么没有正常解析」。

**证据来源**：真机会话记录 `~/.pi/agent/sessions/--Users-onetreehill-Github-ChatGPTBridge--/`
（2026-10-06）——同一条回复在 Pi 侧是 `content:[{"type":"text"}]` 而**不是** `toolCall`，
同一会话里更早/更晚的同类回复却都是 `toolCall`（说明桥本身能解析工具调用，问题只出在这一类文本上）。
把记录里的原文取出直接喂 `parse_tool_calls`：**0 条**（`balanced_objs = 0`）。

**根因**：ChatGPT 网页版把这条**纯文本** `TOOL_CALL:` 行当 markdown 渲染，DOM 取回的文本已被改写
（同一行文本、同一命令，渲染前后只差这一层）：

| 模型写的（JSON 转义要求） | DOM 取回的 | 后果 |
| --- | --- | --- |
| `\"`（值内引号） | **裸 `"`** | JSON 不再合法 |
| `\\n`（字面量 `\n`） | `\n` | 语义改变（字面量 ↔ 真换行） |
| `    `（缩进 4 空格） | ` `（1 空格） | 命令**正文**被改写，解析层无法恢复 |
| 末尾 `"}}` | `"}` | 外层对象**少一个** `}` |

于是：括号不平衡 → `_iter_balanced_objects` 两个扫描都抽不出对象（0 个）；
退到 `_salvage_string_args` 兜底，而它要求 `endswith("}}")` → 直接放弃 →
`parse_tool_calls` 返回 `[]` → `server.py` / `responses.py` 把回复作为纯文本返回、
`finish_reason="stop"` → 客户端判定任务结束。**整条调用被静默丢弃。**

**最小复现与验证**（用真机抓到的原文，`tests/test_toolcalls.py::DomRenderDamageTests::RECEIVED`）：

```
_salvage_string_args(segment)          -> None      # 旧路径到此为止
_salvage_string_args(segment + "}")    -> dict      # 只差这一个 }
parse_tool_calls(text, {"bash"})       -> 0 → 1（修复后；命令内容不被改写）
```

**修复内容**：

1. `toolcalls._salvage_missing_final_brace(segment)`：文本恰好以**单个** `}` 结尾时，
   补一个 `}` 再交给锚点式 salvage；能否救回仍由 `_parse_complete_string_args` 守卫决定
   （值真被截断的回复不会被「猜」出调用），并在救回时打一条 warning 说明原因。
2. `parse_tool_calls` 收尾新增**诊断日志**：回复里出现了 `TOOL_CALL` 标记 / 围栏，
   却一个可用调用都没交出去时打 warning（以前这种情况完全静默，
   线上表现为「客户端什么都没执行就结束了」，无从排查）。

**已知局限（留给后续）**：渲染折叠掉的缩进空格**无法从解析层恢复**，
所以救回的命令仍可能缩进错误（会得到可见的 SyntaxError，模型可以据此重试——
比静默结束好，但不是根治）。根治要换掉「纯文本行」这个载体：
例如要求模型把调用放进代码围栏（围栏内容不经 markdown 转义、也不折叠空格），
或让模型避免在 JSON 里出现缩进/反斜杠转义。这属于协议层决策，见 §5 路线图。

**验证**：`tests/test_toolcalls.py::DomRenderDamageTests` 4 条（含真机原文；
另 3 条锁定「合法 JSON 不受影响」「值被截断不得猜出调用」「只少一个 `}` 能救回」）；
把 `_salvage_missing_final_brace` 关掉后 2 条立刻失败，区分力已实测。
`pytest` → **358 passed / 17 skipped**，`ruff` / `mypy` 干净。

### 2.14（已修复）工具调用载体：纯文本行 → ```tool_call 代码围栏（真机 A/B 证据）

§2.13 找到了根因（网页版把纯文本 `TOOL_CALL:` 行当 markdown 渲染），但那只治了「丢掉调用」的
一半：即使保住调用，缩进也已经被改坏（命令被**静默**改写）。因此根本修法是**换载体**。

**验证环境**：真实网页、`HEADLESS=false`、正在跑的桥（`127.0.0.1:8002`）、
请求带 `tools`（`bash`）且 `tool_choice=auto`，提示词带用户要求的环境声明
（`[环境说明] 执行环境在用户本地电脑上…你直接下命令就可以`）——不带这句模型不会真的下命令。

**对照一（运输层，模型只做逐字节回声，排除模型改写）**：同一 payload
`{"command":"printf \\"hi\\"; echo A\\n    echo B\\n        echo C"}`（真实 4/8 空格缩进 + 需要 JSON 转义的引号）：

| 载体 | 桥返回的 `command` | 逐字节一致 | 缩进段 |
| --- | --- | --- | --- |
| ` ```tool_call ` 围栏 | `printf "hi"; echo A\n    echo B\n        echo C` | ✅ | `[4, 8]` |
| 纯文本 `TOOL_CALL:` 行 | `printf "hi"; echo A\n echo B\n echo C` | ❌ | `[]`（4/8 全被折叠成 1） |

**对照二（更早一轮，单行 JSON + `\"` + 4 空格）**：纯文本行 `json.loads` **FAIL**（引号前的反斜杠被吃掉）、
缩进段 0，`parse_tool_calls` → **0 条**（整条丢弃）；围栏则 `json.loads` OK、缩进段 2，解析出 1 条调用。

**对照三（反例，避免误判）**：把同一条命令交给模型**自己写**（不是回声）时，两种载体返回的都是同一种
“1 空格缩进”——那是**模型自己重排**，不是载体丢东西（围栏在对照一里已证明会保留 4/8 空格）。
提示词因此明确要求“原样粘贴、不要改写缩进”。

**实现**：

1. 提示词全面改为围栏形态：`format_tools_instruction`（示例本身就是一个 ```tool_call 块 + 新增规则
   “info string 必须是 `tool_call`，json/text/空 都不会被执行”）、`format_tool_call_emphasis`、
   `format_tool_retry_nudge`（纠偏也要求围栏）、`edit_markdown_spec`、`markdown_io._build_edit_prompt`。
2. **解析层不需要新增分支**：围栏被网页渲染掉后，DOM 里只剩 info string（`tool_call`）单独一行 + JSON，
   既有的「裸标签 + 平衡扫描」兜底正好认得（真机已验：`finish_reason=tool_calls`，命令逐字节一致）；
   围栏还在时（客户端原样贴回）走 `_TOOL_CALL_FENCE_RE`。刻意**不**接受 ` ```json ` 或 DOM 的 `json`
   标签行——否则正文里展示的 JSON 只要带 `name`/`arguments` 就会被误执行。旧的纯文本行仍兼容。
3. 回归用例 `tests/test_toolcalls.py::FencedToolCallCarrierTests`：真机取回的原文逐字节断言（含 4 空格缩进）、
   原始围栏、围栏内合法多行 JSON、`json` 标签负向；`tests/test_prompting.py` 改为**从注入指令里抽出示例
   直接喂 `parse_tool_calls`**（锁死提示词与解析器同源）。

**已知残留**（不是“已完成”）：

* 模型若写 ` ```json ` 且围栏被渲染掉，取不到（提示词已明确禁止，负向行为有用例锁定）。
* 模型自己重排命令时，任何载体都保不住原缩进（对照三）；只能靠提示词要求原样粘贴。
* `tool_call` 围栏会进入 `_extract_code_blocks` 结果；`SAVE_FILES=true` 时可能被当普通代码块存盘
  （默认为 false，后续可按 lang 过滤）。

**验证**：`pytest` → **362 passed / 17 skipped**，`ruff` / `mypy` 干净。改动需**重启桥**才对客户端生效。

**完整技术报告**：[doc/code_block_fence.md](code_block_fence.md)（原因 / 机制 / 实现 / 真机对照数据 / 复现步骤 / 残留）。

### 2.15（已修复）提示词的**否定式**措辞导致模型拒答（“本会话没有挂载工具”）

现象（用户实测）：载体换成 ```tool_call 围栏、解析层又补了 shell 围栏兜底之后，模型不再写
`bash` 围栏，而是**整轮拒答**：

> …但在当前这个会话环境里**没有实际挂载 `read/write/edit/bash` 执行工具**，所以我不能真实发送
> `tool_call` 并等待执行结果；如果我现在伪造一个，会违反实际工具状态。

接着它给出两条“替代方案”：让用户重新连接带执行器的会话，或者直接给出文件路径 + 修改点 +
patch + 测试命令 + 提交信息，让用户自己动手。整轮没有任何 `tool_call`，客户端看到的是
`finish_reason=stop` 的纯文本——任务静默失败。

**根因**：工具说明头段写的是「You have **NO direct access** to a shell, filesystem, or the
internet…」。这句本意是断掉“凭知识作答”的退路，但它是**否定式**陈述，模型把它读成关于
**会话能力**的事实描述（“本会话没有 shell / 文件系统”）→ 它认为**工具没挂载**，而发
`tool_call` 就是在**声称**自己有能力——于是被既有的“不伪造工具输出”拦住，诚实拒答。
元凶不是“工具清单没送到”（清单就在同一段里），而是同一段里那句否定式陈述抵消了清单的效果。

**第一次修法（无效）**：把否定句改成肯定句，头段写成「You are an autonomous agent driving a
local executor bridge… The tools listed below are **ALREADY MOUNTED and LIVE**…」，并新增规则 7
禁止以「no tools / no executor / nothing is mounted」为由拒答。模型换了种拒答：

> 我看到了你提供的本地执行器协议……但在当前这个会话环境里**没有实际挂载
> `read/write/edit/bash` 执行工具**，所以我不能真实发送 `tool_call`……

**真正的根因（用户判定）**：两版措辞毛病相同——都在**描述模型自己的状态**（“你没有 shell
访问” / “你的工具已挂载”）。模型会把它当成一道关于**自身能力**的事实题去核对：

| 头段怎么说 | 模型怎么想 | 结果 |
| --- | --- | --- |
| 「You have NO direct access to a shell…」 | 本会话没有 shell / 文件系统 | 拒答：“迁移到带执行器的会话后我再下命令” |
| 「You are an autonomous agent… tools are ALREADY MOUNTED and LIVE」 | 我是 agent ⇒ 我应该有挂载好的工具列表，可**我的列表里没有它** | 拒答：“没有实际挂载 read/write/edit/bash 执行工具” |

**最终修法：只描述链路，不描述模型的能力。** 三块注入文本统一改为：

* **定位**：「You are a **language model**: the only thing you produce is text, and you cannot
  run anything yourself」——不再写 agent。
* **执行者在客户端**：「This conversation is read by a small **local client** running on the
  user's own computer - when you write a tool call… **that client executes it there for real**
  and returns the real output to you as your next message」。
* **不需要（也无法）挂载在它那一侧**：「**No tool has to be installed or mounted on your
  side**, and the tools below will never appear in your built-in tool list - **that is expected
  and normal**」——直接拆掉那句拒答的事实前提。
* **发调用 ≠ 伪造**：「**not fabrication** and not a false claim about your capabilities: you
  are writing the command and the client runs it」。
* **断退路仍在，但不再用会被读成“没工具”的句型**：「That is the **only way you take real
  action** here… answering from your own knowledge… means the task **FAILS**. Never fabricate
  tool output.」
* **工具清单归属客户端**：「**Tools the client (not you) can execute** - use these exact names:」。
* **规则 7 只禁止拒答这个“借口”**（不声称已挂载）：「Never decline a task on the grounds that
  you \"have no tools\" or that the tools are not in your own tool list: **you are not the one
  that runs them**」；也不得“给用户一段补丁让他自己应用”来代替调用工具。
* `format_tool_call_emphasis` / `format_tool_retry_nudge` 同口径（纠偏块：「You are **not
  expected to have tools of your own**: this conversation is read by a local client… it
  executes whatever tool call you write」）。

**同类教训**：不要用否定句去“切断退路”。任何「你没有 X」都会被模型当成关于自身能力的事实
陈述，而不“X”往往正是它能做的事。要说的是**链路**（你写的东西会被谁拿去做什么），不是
**它的本体**。

**为什么这不是“措辞喜好”**：§2.14/§6.1 都记过。真机 A/B 的**关键前提**就是提示词里必须带一句
“执行环境在用户本地、命令会由客户端真实执行”。旧实现只在 `seed=True` 路径注入
`config.SEED_ENV_NOTE`，**增量路径完全没有**这类声明；现在这句被内建进工具块本身，
两条路径每轮都带着。

**验证**：新增 `test_prompting::test_instruction_denies_the_no_tools_refusal`：三块注入文本都
必须禁止“no tools”式拒答、都不得出现 `agent`（`assertNotIn("agent", text.lower())`），工具块
必须出现 `not fabrication`、`local client running on the`、`returns the real output to you as
your next message`、`No tool has to be installed or mounted on your side`、
`Tools the client (not you) can execute`；`test_instruction_forbids_answering_without_tool` 的
断言从 `NO direct access` / `ONLY way`（旧）→ `ALREADY MOUNTED`（第一版修法，已被否决）→
`only way you take real action` / `FAILS` / `Never fabricate`（当前）。
`ruff check .` / `mypy chatgpt_web` exit 0，`.venv/bin/python -m pytest` → **459 passed /
17 skipped**。

**未做（有意）**：没有加“识别拒答措辞再反复纠偏”的重试机制——现有纠偏只追发一次且仅限“本轮
一次工具都没调用过”；再加一轮拉锯会把“任务收尾的纯文本”也拖成新指令（§2.12 的事故）。
拒答是提示词问题，就在提示词里解决。

---

## 3. 静态分析发现（与 E2E 无关的既有问题）

### 3.1（P0）`edit_markdown` 本地执行可写任意路径

`toolcalls.execute_edit_markdown`（`chatgpt_web/toolcalls.py:115`）对模型给出的 `path` 只校验「非空字符串」即读写；`server.py:325` / `responses.py:245` 在 `EDIT_MARKDOWN_LOCAL=true`（`.env` 实际为 true）时自动注入该工具。模型一旦被注入（tool 结果 / 网页内容），即可**以服务进程权限覆盖本机任意文件**。

建议：加 `EDIT_MARKDOWN_ROOT` 并在解析后校验 `Path(path).resolve().is_relative_to(ROOT)`，拒绝绝对路径与 `..`；`write=true` 需显式开启 `EDIT_MARKDOWN_WRITE`；README「安全」节补充说明。

### 3.2（P1）去重 / 泄漏检测标签与实际注入块不一致

`prompting.py:247` 用 `"[工具调用说明]"`、`:256` 用 `"[edit_markdown 说明]"` 做去重判断，但实际注入块首行是 `"[Tool Calling Instructions]"`（`toolcalls.py:206`）与 `"[edit_markdown notes]"`（`toolcalls.py:105`）——永不相等，去重恒不生效；`tests/e2e/test_parity.py:270` 的泄漏检测 token 也用了中文标签，同样匹配不到真实块（该断言形同虚设）。建议把 header 提为常量，`prompting` 与 e2e 共用。

### 3.3（P1）`_bucket_busy` 语义错误

`page_pool.py:78` 用 `self._lock_for(bucket).locked()` 判断桶忙，而 `_lock_for`（`:32`）在 `PARALLEL_BUCKETS=false` 时对任何桶都返回全局 `self.lock` → 只要**任意**桶在跑，所有桶都算忙。影响：`responses.py` 流式预检查会把「别的桶在跑」误报成「本桶忙」并立刻 503（绕过 `BUCKET_LOCK_TIMEOUT_S` 的排队语义）；`_evict_session_cache` / LRU 换页在请求期间永远选不出候选。建议改用已有的 `self._active_buckets`（`_session_lock` 精确维护）判断。

### 3.4（P1）日志与异常可见性

库代码 57 处 `print`（`chat_io.py` 22、`completion.py` 15、`server.py` 8 …），60 处裸 `except Exception`，多处 `except Exception: pass`（状态落盘、任务快照落盘）。排障依赖 `CHATGPT_DEBUG=1` 与 stdout，且失败静默。建议引入 `logging`（INFO/DEBUG 分级，`CHATGPT_DEBUG` 控制级别）、为每个请求加 `request_id`、对「本应不失败」的落盘点至少 `warning(exc_info=True)`。

### 3.5（P2）非回环暴露缺少告警与文档声明（**设计上不引入 API Key**）

**前提（设计决定）**：本项目的核心设计需求之一就是**零配置、不需要 API Key**——客户端（Pi / Codex 等）只要把 `base_url` 指向 `http://127.0.0.1:8002/v1`、`api_key` 随便填即可。因此**不应当**新增 `API_KEY` / `Authorization: Bearer` 之类的鉴权；把「没有鉴权」当成待补的能力来修，与项目定位冲突。（`/v1/models` 与 `/v1/chat/completions` 对任意 `api_key` 都要照常服务，这是契约的一部分。）

**仍然成立的风险**：`HOST` 默认 `127.0.0.1`（安全），但改成 `0.0.0.0` 后所有端点（含 `/debug/dom`、`/session/reset`）对外裸奔，任何能访问该地址的人都可以借你的登录态与 ChatGPT 配额发起请求。既有设计里唯一的可选防护是 `/session/reset` 的 `RESET_TOKEN`（默认空 = 不校验），那属既有行为，不算新增鉴权。

**建议（只做「不误导 + 可察觉」，不引入鉴权）**：
1. `HOST` 为非回环地址时**启动打印醒目告警**：「本服务不提供鉴权，非回环暴露会让任何能访问该地址的人使用你的 ChatGPT 账号，请仅在本机使用」（复用 `completion._warn_if_blocked` 的提示风格，不阻断启动）。
2. 在 README「安全」节与 `.env.example` 中**明确声明**：服务设计为仅监听 `127.0.0.1`；暴露到非回环地址属**不受支持**用法，风险自负；需要远程访问请用 SSH 端口转发 / VPN 等**外部手段**，而不是在服务里加 key。
3. 加一条**回归测试**锁定该设计决定：不带任何认证头请求 `/v1/models`、`/v1/chat/completions` 必须**不**返回 401/403（防止后续被误加鉴权）。

### 3.6（P1）同步文件 I/O 阻塞事件循环

`session_store._save_session_state`（每轮写整个状态文件）、`tasks.record`、`chat_io.save_extracted_files`、`toolcalls.execute_edit_markdown` 都在协程内同步执行且无 `await`，大文件时会卡住 SSE keep-alive。建议 `await asyncio.to_thread(...)`，状态文件改为「临时文件 + `replace`」原子写（`markdown_io.write_md` 已是此模式，可复用）。

### 3.7（P2）死代码与重复实现

- `responses.py:280` `_resolve_session()` 无任何调用方；
- `responses.py:255` `_maybe_run_edit_markdown` 与 `server.py:281` `_run_local_edit_markdown` 逐行重复。
建议删除死代码、把本地 edit_markdown 执行抽到 `toolcalls.py` 单一函数。

### 3.8（P2）`asyncio.get_event_loop()` 位置不当

`chat_io.py:630,795,806` 在协程内使用（仓库 `.venv` 为 Python 3.14）。建议改用 `asyncio.get_running_loop()`，并加一条静态断言测试。

### 3.9（P2）依赖与工程化

`requirements.txt` 仅 5 行且**全部未固定版本**；无 `pyproject.toml`、无 dev 依赖声明（测试需 `pytest`，`fastapi.testclient` 需 `httpx`）、无 CI、无 lint/类型检查。建议补 `pyproject.toml`（固定带上下界版本 + dev extra）、`ruff` + `mypy` + 最小 CI（`pytest -q`，e2e 默认 skip）。以下问题本可被工具提前发现：`_resolve_session` 死代码、§3.2 标签不一致、`models.ChatMessage.content: Optional[Any]` 泛滥。

**处置（2026-10-06，T4.1 已实现，与原 `uv init` 骨架合并）**：

- `pyproject.toml`：运行期依赖带上下界（`fastapi>=0.115,<1` 等）；dev extra = `pytest` / `httpx` / `ruff` / `mypy` / `openai`（e2e B8 用）；`[tool.pytest.ini_options]`（`testpaths=tests`、`addopts=-q`）；`[build-system]` 用 setuptools。
- `ruff`：`line-length=100`，规则集刻意收敛为 `E4,E7,E9,F,I`；唯一豁免 `chatgpt_web/driver.py` 的 `E402`（惰性 import Playwright 的占位赋值，有意为之），已在配置里注明原因。
- `mypy chatgpt_web`：0 error。豁免仅 4 个 Mixin 模块的 `attr-defined`（`chat_io` / `completion` / `page_pool` / `session_store`——宿主属性由 `ChatGPTWebDriver` 组合后提供，mypy 静态看不到），其余错误码照常检查；另修掉 7 处真实类型问题（隐式 Optional、`ToolCall.function` 类型、`_session_key` 入参联合类型、`tools` 为 None 的窄化、`match` 变量类型冲突）。
- CI：`.github/workflows/ci.yml`（push / PR 上跑 `ruff check .` + `mypy chatgpt_web` + `pytest -q`；e2e 默认 skip）。**未推送验证**：本地三条命令均通过，但“在 PR 上跑通”需真实 push 后才能确认。

### 3.10（P2）结束判定/轮询主循环的性能与可测性

`chat_io._send_chat_locked` 的轮询循环约 200 行、嵌套 4 层；`_complete_text`（`chat_io.py:267`）**每轮**都 `cloneNode(true)` + 屏幕外挂载再读 `innerText`（仅为绕过逐 token 显现动画），长回复下是每 1.5s 一次的固定开销。建议：先探测节点内是否存在 `.animating/.pending`，**没有动画时直接 `inner_text()`**；把「是否仍在生成 / 是否落定 / 静默计数」抽成纯函数状态机，便于穷举边界（现有 `test_end_detection.py` 已用假 page 覆盖，可平移）。

E2E 实测未显示 bridge 变慢（比值 ≤1.0），故此项属**优化**而非缺陷。

---

## 4. 文档死链清单

提交 `700bb67` 删除了整个 `doc/` 目录，但多处引用未同步：

| 引用位置 | 指向 | 现状 |
| --- | --- | --- |
| `README.md:238,239,271` | `doc/design.md`、`doc/tasks.md` | 已删除 |
| `chatgpt_web/responses.py:7` | `doc/codex_support.md` | 该文件**从未被 git 跟踪** |
| `chatgpt_web/markdown_io.py:3` | `doc/update.md`（本文） | 旧设计稿已删；本文已重新落位该路径 |
| `tests/test_sessions.py:1`、`tests/test_responses.py:1` | `doc/update_codex.md` | 已删除 |
| `tests/e2e/__init__.py:3`、`test_parity.py`、`direct.py` | `doc/e2e_test_design.md` | 已删除（本次 e2e 结论依赖它，建议至少重建该文档） |

建议：恢复/重建 `doc/e2e_test_design.md`（e2e 判定矩阵是理解本次失败的关键），其余引用统一改为指向 README 或删除。

**另**：`markdown_io.py` 的 docstring 现在指向本文，但本文定位是「项目分析与改进建议」，建议把该 docstring 改为指向 README 或模块自身说明。

---

## 5. 建议路线图

| 阶段 | 内容 | 依据 |
| --- | --- | --- |
| **第 1 步（P0，1–2 天）** | ① ~~确认并修复会话分桶串台~~（用户判定关闭，§2.2）；② 修复工具调用在播种路径失效（§2.1） | E2E C1 / C2 |
| **第 2 步（P1，1–2 天）** | ③ E2E harness 端口默认值与失败清理（§2.3、§2.4）；④ `edit_markdown` 路径沙箱（§3.1）；⑤ 去重标签常量（§3.2）、`_bucket_busy`（§3.3） | E2E 首次运行 + 静态 |
| **第 3 步（P1，数天）** | ⑥ 日志体系与异常可见性（§3.4）；⑦ 同步 I/O 转线程 + 原子写（§3.6）；⑧ 发送按钮/新建对话选择器更新 + `/healthz` 选择器自检（§2.5、§2.6） | |
| **第 4 步（P2，持续）** | ⑨ `pyproject.toml` + ruff/mypy + CI（§3.9）；⑩ 结束判定状态机抽取与 `_complete_text` 优化（§3.10）；⑪ e2e 软失败指标化 + 重建 `doc/e2e_test_design.md`（§2.7、§4）；⑫ 非回环暴露的启动告警与文档声明（§3.5，**不引入鉴权**） | |

---

## 附录 A：如何复跑 E2E（含本次踩到的坑）

```bash
# 1) 停掉占用 profile 的服务（三个用例独占 user_data）
pkill -f chatgpt_api_server.py
# 2) 清掉可能残留的单实例锁（异常退出时）
rm -f user_data/Singleton*

# 3) 跑全部 e2e；E2E_PORT 必须显式给，否则可能被宿主环境的 PORT 覆盖
cd <project>
CHATGPT_E2E=1 E2E_HEADED=1 E2E_FULL=1 E2E_PORT=8002 \
  .venv/bin/python -m pytest tests/e2e -v -s

# 4) 跑完检查是否有残留（harness 在 setUpModule 失败时不会回收）
pgrep -fl 'uvicorn chatgpt_api_server'
```

- `E2E_HEADED=1` 必需：实测 `HEADLESS=1` 会被 Cloudflare 挑战页拦截。
- 有头运行会真实打开浏览器窗口；每个用例会真实访问 ChatGPT（首次记录为 24 条用例、约 8.5 分钟；2026-10-06 收敛后为 17 条，见 doc/e2e_test_design.md §7）。
- 若只想看 DOM/选择器校准，单独跑 `tests/e2e/test_dom_probe.py` 即可（2026-10-06 已把原 `test_new_chat_probe` 的新建对话推断合并进来），它不依赖 bridge。

## 附录 B：本次未完成的工作

- §2.2 的**分桶隔离确认实验**已由用户判定关闭（无须验证、非设计需求；T0.1/T1.2 = WONTFIX），探测草稿 `/tmp/iso_test.py` 仅作留档，不修改 D2 用例、不计入门禁。
- 本次 E2E 为**单次运行**，`C1/C2` 存在模型非确定性的可能；建议连续跑 2–3 次再定性。
- 本文件为分析文档，未修改任何源码或测试；`pytest -q` 的 219 passed / 24 skipped 为既有基线。
