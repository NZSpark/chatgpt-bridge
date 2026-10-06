# E2E 对等测试设计（`tests/e2e/`）

> 重建于 2026-10-06（doc/tasks.md T4.4）。本文件替代 `700bb67` 删除的旧设计稿，
> 是 `tests/e2e/*` 各模块 docstring 的引用目标。
> 首轮真实运行结果与由结果推导的建议见 `doc/update.md`（§1、§2）。

## 1. 判定矩阵

### 1.1 定位

单测（`tests/*.py`）不发起网络请求；E2E（`tests/e2e/`）**真实访问 ChatGPT 网页版**，
回答两个问题：

1. bridge 相对「人直接操作网页」是否**能力对等**；
2. 改动是否破坏了真实链路（DOM、模型、登录态、风控）。

### 1.2 判定原则（核心）

**直连（`DirectChatGPTClient`）是「上游能力基线」**。同一 prompt、同一断言 predicate
分别在直连与 bridge 上执行：

| 直连侧 | bridge 侧 | 判定 |
| --- | --- | --- |
| 达标 | 达标 | **PASS** |
| 达标 | 不达标 | **FAIL**（bridge 代码缺陷） |
| 不达标 / 无法执行 | — | **SKIP**（环境 / 上游问题，不算 bridge 缺陷） |

实现见 `test_parity.py::E2ECase.run_parity` 与 `guard_upstream`：

- 上游/环境类失败一律转 SKIP：HTTP `502/503/504`；或响应 `error.type` ∈
  `{timeout, upstream_error, context_length_exceeded}`。
- 直连侧默认重试 1 次；重试后仍不达标 → SKIP，并在消息里附原文开头（说明是环境问题）。
- bridge 侧默认重试 1 次；重试后仍不达标 → **FAIL**，消息里打印两侧原文各 800 字符。
- 每次尝试使用独立会话桶：`e2e-{case_id}-r{attempt}`（避免复用上一次失败的上下文）。

### 1.3 直连基线的独立性（`direct.py`）

- 只与 bridge 共享**配置**（`.env` 选择器、入口 URL），**不复用**其发送 / 轮询逻辑；
- 结束判定采用独立算法：「文本连续 N 轮不变 且 节点内无未显现 token」，
  不使用 bridge 的停止按钮 / 节点数 / 双阈值判据——避免用被测代码验证被测代码；
- 同步 Playwright API，单页面串行提问（降低风控概率）。

### 1.4 用例矩阵

| 组 | 用例 | 内容 |
| --- | --- | --- |
| A 内容对等 | `a1` / `a2` / `a4` | 哨兵、事实、长文尾哨兵（含长度比与中文占比） |
| B 协议与端点 | `b1` / `b2` / `b4` / `b6` / `b8` | `/healthz`、`/v1/models`、chat SSE 序列、Responses 命名事件、openai SDK 冒烟（`b8` 需 `E2E_FULL=1`） |
| C 工具调用 | `c1` / `c2` | 非流式 / 流式 function calling（`TOOL_CALL` 解析链路） |
| D 会话 | `d1` / `d2` / `d3` | 多轮记忆、跨桶隔离（**观察项**，见 §5）、reset 后播种保上下文 |
| E Markdown IO | `test_markdown_io_e2e.py`（1 条） | `render_view` → 模型 `edit_markdown` → `generate_edit` → `apply_edit` → `write_md`（dry-run）；纯逻辑部分由单测覆盖，见 §7 |
| F 提交链路 | `test_prompt_submit_e2e.py`（2 条） | 多行（含代码块）提交、残留草稿清理后提交 |

另有 `test_dom_probe.py` 一个**选择器校准探针**（2026-10-06 由原 `test_dom_probe`
与 `test_new_chat_probe` 合并而成）：一次页面加载完成 composer / 发送按钮 /
新建对话的命中统计与候选推断，不依赖 bridge。用途：网页改版后重新校准 `.env`。

## 2. Gating 开关

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `CHATGPT_E2E` | `=1` 才运行 E2E；否则全部 skip（常规 `pytest -q` 的 17 skipped，收敛后数量） | 未设置 |
| `E2E_HEADED` | `=1` 有头运行；实测 `HEADLESS=1` 会被 Cloudflare 挑战页拦截 | 未设置（无头） |
| `E2E_PORT` | bridge 端口；未设置时回退 `config.PORT`（仓库 `.env` 已定为 8002） | `config.PORT` |
| `E2E_FULL` | `=1` 启用 `b8`（openai SDK 联调） | 未设置 |
| `CHATGPT_DEBUG` | 启用 `/_debug/selectors` 选择器自检（服务侧开关，T3.4） | 未设置 |

> **注意**：`config` 读取真实环境变量优先于 `.env`，而宿主 shell 可能自带 `PORT`。
> 因此运行 E2E 时**显式带 `E2E_PORT=8002`**。该问题（update.md §2.3）已由用户决定
> 关闭（T2.1 = WONTFIX）：`PORT` 以 `.env` 的 8002 为准，不为端口做重定义 / 校验。

## 3. 前置条件

1. **profile 独占**：
   - bridge 使用 `user_data/`；直连使用副本 `user_data_e2e/`（`_copy_profile` 每轮刷新）。
     两个 Chromium **不能共享同一 profile 目录**。
   - 跑 E2E 前必须停掉占用 `user_data/` 的服务：`pkill -f chatgpt_api_server.py`；
   - 异常退出后清掉残留单实例锁：`rm -f user_data/Singleton*`
     （harness 侧 `clear_profile_locks` 也会处理）。
2. **登录态**：`user_data/` 必须已完成真实登录；缺失时 `setUpModule` → SKIP，
   并提示「先完成真实登录（`HEADLESS=false` 手动登录）」。
3. **bridge 冷启动**：`ensure_started` 等待上限 90s；若外部已有可用服务则**复用，
   不杀外部进程**（只回收自己拉起的子进程）。

## 4. Harness 陷阱与对策（首轮 E2E 暴露）

| 陷阱 | 表现 | 对策（已实现） |
| --- | --- | --- |
| `setUpModule` 抛错时 `unittest` **不调用** `tearDownModule` | 泄漏 uvicorn + Chromium，独占 `user_data`，后续所有用例假失败 | T2.2：先 `unittest.addModuleCleanup(_cleanup_module)` 再执行可能失败的步骤；`ensure_started` 所有失败路径先 `_terminate_proc`；`reap_orphan_bridges` 只回收 ppid≤1 的孤儿；`stop()` 幂等并 `wait_port_closed` |
| `PORT` 被宿主环境变量污染 | bridge 起在 0 端口 → 首轮 19 个用例基础设施性失败 | 运行时显式 `E2E_PORT=8002`（WONTFIX，见 §2 注意） |
| profile 残留单实例锁 | Chromium 启动即退出 / 新实例起不来 | `clear_profile_locks`（`Singleton*`） |
| profile 副本被占用或复制失败 | 直连侧无法启动 | `_copy_profile` 失败 → SKIP，不误报为 bridge 缺陷 |
| 无头被 Cloudflare 拦截 | 直连基线整体不达标 | 必须 `E2E_HEADED=1` |

## 5. 结果判读与归档

- `tearDownModule` 打印 `[E2E 耗时汇总]`；bridge/direct 比值 > 3 打印 `WARNING`
  （仅提示，不判失败）。
- **`d2`（跨桶隔离）为观察项**：用户判定「同一任务上下文一致即可，跨桶隔离非设计需求」
  （tasks.md T0.1 / T1.2 = WONTFIX；update.md §2.2）。用例**保留原样、不削弱断言**，
  但不计入验收门禁，其失败不得掩盖其它用例的结论。
- 失败诊断：uvicorn 日志在 `$TMPDIR/chatgpt_e2e_uvicorn.log`
  （macOS 默认位于 `/var/folders/.../T/`）。
- 复跑命令（含清场步骤）见 `doc/update.md` 附录 A。

## 6. 新增用例约定

- `case_id` 用小写短名（`a1` / `b4` / `c2` / `d3`…），会话桶统一前缀 `e2e-`；
- 内容对等类用例用 `run_parity`（直连基线 + bridge 判定）；
  只针对 bridge 的协议类用例（`b*` / `d*`）直接调 `BridgeClient`；
- 上游/环境类失败用 `guard_upstream` / `chat_or_skip` 转 SKIP，**不要吞异常**；
- 「模型不按约定调用工具」属**软失败**（计入汇总、可重试一次），

## 7. 套件收敛记录（2026-10-06，仅改测试代码，未联网运行）

原则：**联网用例只保留「单测无法覆盖」的部分**；纯逻辑断言留在单测，凡效果已被
其它用例覆盖的即删除。逐条依据：

| 删除的用例 | 原因 / 覆盖它的用例 |
| --- | --- |
| `test_e2e_read_preserves_bytes` | 纯本地，不发网络；`tests/test_markdown_io.py` 的 `ReadMdTests` / `FenceScanTests` 已覆盖 |
| `test_e2e_locate_anchors` | 纯本地；`LocateTests`（含「围栏内不参与结构匹配」）已覆盖 |
| `test_e2e_fence_block_replace_keeps_lang` | 纯本地；新增单测 `ApplyEditTests.test_replace_fence_block_keeps_sibling_langs` 同等覆盖 |
| `test_e2e_render_view_marks_fences` | 本地断言由 `RenderViewTests` 覆盖；联网部分只是「发个大 prompt、回复非空」，弱于 A1/A4 |
| `test_e2e_edit_keeps_fences_paired` | 与 `test_e2e_model_edit_dry_run` 同为「模型→`edit_markdown`→结构健康」，后者断言更全（dry-run 不落盘 + diff + 围栏数 + `verify`）；追加场景由新增单测 `test_append_at_end_keeps_langs` 覆盖 |
| `test_single_line_prompt_submits` | 单行提交是 `test_multiline_prompt_submits` 的子集（同一 `send_chat` 链路），后者才是历史回归目标 |
| `test_new_chat_probe`（整个模块） | 与 `test_dom_probe` 重复：都开首页 dump 按钮、都打印 `NEW_CHAT_SELECTOR` 命中数；已合并进 `test_dom_probe`，不再单独启动一次浏览器 |

收敛后的 E2E 用例数：**24 → 17**（Markdown 6→1、提交链路 3→2、探针 2→1，其余不变）；
`d2` 按用户决定保留为观察项，未动。相关文档（tasks.md §8、update.md §2.7/附录 A）
已同步为收敛后的口径。

**刻意保留的「看似重复」项**（去重时的边界判定，留档备查）：

* `b1` / `b2`（`/healthz`、`/v1/models`）：单测走 `TestClient` + 假 driver，校验的是路由实现；
  这两条在 E2E 里校验的是**真实跑起来的进程**（单测无法覆盖），且不访问 ChatGPT、开销近似为零，
  保留作为运行前置自检。
* `b4` / `b6`：chat SSE 与 Responses 命名事件是两套不同端点/事件模型，互不覆盖。
* `c1` / `c2`：非流式与流式的 tool_calls 组装路径不同（`finish_reason`、参数分片拼接）。
* `d1` / `d3`：增量多轮记忆 vs reset 后重新播种，前者测会话，后者测播种链路。
  不得用 `skipTest` 掩盖（见 tasks.md T4.3）。
