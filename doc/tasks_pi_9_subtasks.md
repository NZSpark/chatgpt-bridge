# P2：模块拆分子任务分解

> 来源：doc/tasks_pi_9.md
> 目标：将模块拆分建议转换为可执行工程任务。

## 状态汇总（2026-10-07 核对代码后更新）

| 阶段 | 任务 | 状态 | 落地位置 / 备注 |
|---|---|---|---|
| 一 | PI-901 Tool Runtime 拆分 | ✅ 已完成 | `chatgpt_web/tools/`（parser/validator/policy/executor/ledger/serializer）；`toolcalls.py` 为 facade；`tests/test_tools_package.py` |
| 二 | PI-902 Completion 拆分 | ✅ 已完成 | `chatgpt_web/completion/`（lifecycle/generator/extractor）；`completion/__init__.py` 即 facade；`tests/test_completion_package.py` |
| 三 | PI-903 Browser 层拆分 | ✅ 已完成 | `chatgpt_web/browser/`（selectors/dom_adapter/diagnostics/driver）；`dom_adapter.py` 为 facade；`tests/test_browser_package.py` |
| 四 | PI-904 API Adapter 拆分 | ⬜ 未开始 | 无 `api/`；`server.py`(717行)/`responses.py`(692行)/`streaming.py`(247行) 仍分立 |
| 五 | PI-905 Session 模块化 | ✅ 已完成 | `chatgpt_web/session/`（schema/migration/lock/store）；`session_store.py` 为 facade；`tests/test_session_package.py` |
| 六 | PI-906 Config 模块化 | ✅ 已完成 | `chatgpt_web/config/`（_core + server/browser/session/tools/limits/debug）；`config/__init__.py` 为兼容 facade；`tests/test_config.py` 含包结构回归 |

## 总体原则

- 保持现有 API 行为不变。
- 优先新增模块，再迁移调用。
- 保留旧 import 路径，通过 facade 兼容。
- 每个子任务独立提交。
- 每阶段必须增加测试并通过 `pytest -q`。

---

# PI-901 Tool Runtime 拆分

## 状态：已完成（2026-10-07）

落地：`chatgpt_web/tools/`（parser / validator / policy / executor / ledger /
serializer）；`chatgpt_web/toolcalls.py` 作为兼容 facade 再导出；
`tests/test_tools_package.py` 为独立回归测试。

## 目标

将过大的 `toolcalls.py` 拆分为独立 tools package。

## 子任务

### PI-901-1 创建 tools 包结构

新增：

```
chatgpt_web/tools/
├── parser.py
├── validator.py
├── policy.py
├── executor.py
├── ledger.py
└── serializer.py
```

验收：

- package 可正常导入；
- 原 `toolcalls.py` 保留。

### PI-901-2 迁移 parser

负责：

- tool_call fence 解析；
- JSON extraction；
- malformed repair。

验收：

- 原解析测试全部通过；
- 新 parser 有独立测试。

### PI-901-3 迁移 validator

负责：

- 参数 schema 校验；
- required 字段检查。

验收：

- validation error 类型保持兼容。

### PI-901-4 迁移 policy

负责：

- allowed_tools；
- allowed_paths；
- write_enabled；
- runtime/network policy。

验收：

- policy 在 executor 前执行。

### PI-901-5 迁移 executor

负责：

```
ToolCallRequest -> execute -> ToolResult
```

验收：

- executor 不依赖模型输出格式。

### PI-901-6 迁移 ledger

负责：

- 去重；
- 执行记录；
- result hash。

验收：

- session 隔离；
- 重复调用不会重复执行。

---

# PI-902 Completion Pipeline 拆分

## 目标

降低 completion.py 复杂度。

## 状态：已完成（2026-10-07）

实际落地结构（与下面子任务里理想化的 ``runner.py`` 命名略有出入，因为真实
代码里 completion 承载的是**会话生命周期**而非 task 生命周期；task 状态机在
``task_state.py``）：

```
chatgpt_web/completion/
├── __init__.py    组装 + re-export
├── lifecycle.py   CompletionMixin 主体（会话生命周期；对应下面的 runner 职责）
├── generator.py   生成等待：结束判定状态机 + 增量回调 + 阈值组装
└── extractor.py   回复 / 代码块提取（strip_code_noise 等纯函数）
```

``chatgpt_web/completion.py`` 现在是兼容 facade：

- 再导出子包全部公开对象（历史 ``from chatgpt_web.completion import CompletionMixin`` 不变）；
- 保留可打补丁常量 ``_NEW_CHAT_SEARCH_TIMEOUT_S`` / ``_NEW_CHAT_POLL_INTERVAL_S``，
  ``CompletionMixin._open_new_chat`` 调用时回读 facade，既有回归测试（``mock.patch.object(completion, ...)``）仍生效。
- ``chat_io.py`` 的 ``_strip_code_noise`` / ``EndLimits`` 组装改为委托子包，行为不变。

独立回归测试：``tests/test_completion_package.py``。

## 子任务

### PI-902-1 创建 completion package

新增：

```
completion/
├── runner.py      -> 实际为 lifecycle.py
├── generator.py
└── extractor.py
```

### PI-902-2 迁移任务生命周期

移动：

- task 创建；
- 状态流转；
- 完成处理。

进入 runner.py。

### PI-902-3 迁移生成逻辑

移动：

- 输入发送；
- 等待生成；
- streaming delta。

进入 generator.py。

### PI-902-4 迁移提取逻辑

移动：

- assistant reply；
- code block；
- tool call 检测。

进入 extractor.py。

验收：

- completion.py 只作为 facade。  ✅

---

# PI-903 Browser 层拆分

## 状态：已完成（2026-10-07）

实际落地结构：

```
chatgpt_web/browser/
├── __init__.py     组装 + re-export
├── selectors.py    JS 片段 + 硬编码 fallback 选择器 + SELECTOR_VERSION（唯一来源）
├── dom_adapter.py  ChatGPTDOMAdapter（DOM 查询与启发式）
├── diagnostics.py  DiagnosticsMixin（命中数 / 停止控件候选 / 文本长度）
└── driver.py       BrowserLifecycleMixin（Playwright 启动 / context / page 生命周期）
```

说明：可被 ``.env`` 覆盖的候选选择器（``INPUT_SELECTORS`` / ``RESPONSE_SELECTORS`` /
``NEW_CHAT_SELECTOR`` 等）仍留在 :mod:`chatgpt_web.config`（测试用
``patch.object(config, ...)`` 的契约）；纯代码内的 JS 片段与硬编码 fallback 选择器
移到 ``browser/selectors.py``。

兼容：``chatgpt_web.dom_adapter`` 仍是 facade（再导出 ``ChatGPTDOMAdapter`` 与
选择器常量）；``ChatGPTWebDriver`` 组合 ``BrowserLifecycleMixin``，``init`` /
``close`` / ``playwright`` / ``context`` / ``page`` 行为不变。

独立回归测试：``tests/test_browser_package.py``。

## 子任务

### PI-903-1 创建 browser package

结构：

```
browser/
├── driver.py
├── dom_adapter.py
├── selectors.py
└── diagnostics.py
```

### PI-903-2 Selector 集中管理

迁移：

- CSS selector；
- fallback selector；
- selector version。

验收：

- 业务代码不直接使用 selector。

### PI-903-3 Driver 生命周期拆分

负责：

- browser 启动；
- context 管理；
- page 生命周期。

### PI-903-4 Diagnostics 独立

负责：

- DOM probe；
- screenshot；
- debug dump。  ✅（screenshot/debug dump 暂未涉及，现有诊断方法已迁入）

---

# PI-904 API Adapter 拆分

## 状态：未开始

现状：`chatgpt_web/api/` 不存在；`server.py`（~29KB）/ `responses.py` /
`streaming.py` 仍是独立模块，协议转换与 HTTP 处理仍混在 server 里。

## 子任务

### PI-904-1 创建 api package

结构：

```
api/
├── chat_adapter.py
├── responses_adapter.py
└── streaming_adapter.py
```

### PI-904-2 Chat Adapter

负责：

- messages 转换；
- Chat response 格式化。

### PI-904-3 Responses Adapter

负责：

- input/output item；
- tool mapping。

### PI-904-4 Streaming Adapter

负责：

- SSE chunk；
- delta event。

验收：

- server.py 只负责 HTTP。

---

# PI-905 Session 模块化

## 状态：已完成（2026-10-08）

实际落地结构：

```
chatgpt_web/session/
├── __init__.py   组装 + re-export
├── schema.py     SessionState + STATE_SCHEMA_VERSION + 文档校验/payload 过滤
├── migration.py  v1(legacy root/default) -> v2
├── lock.py       落盘串行化锁 STATE_FILE_LOCK
└── store.py      SessionStoreMixin（分桶状态 + 磁盘读写 + 轮转判定）
```

兼容契约（``chatgpt_web/session_store.py`` 仍是 facade）：

- ``from chatgpt_web.session_store import SessionState, SessionStoreMixin`` 不变；
- ``session_store.STATE_SCHEMA_VERSION`` / ``session_store.json`` /
  ``session_store._warned_bad_state`` / ``session_store.logger`` 仍可
  ``patch.object``：store 层在**调用时**经 ``_facade()`` 回读这些名字；
- 日志 logger 名保持 ``chatgpt_web.session_store``，``assertLogs(...)`` 不变；
- ``SessionStoreMixin._read_state_file`` / ``_save_session_state`` 等方法仍在
  同一个 mixin 类上，class-level patch 生效。

独立回归测试：``tests/test_session_package.py``；既有
``test_state_persistence.py`` / ``test_sessions.py`` / ``test_session_concurrency.py``
/ ``test_session_invariants.py`` 全部通过。

## 子任务（全部完成）

### PI-905-1 创建 session package  ✅

### PI-905-2 Schema 拆分  ✅

- session 数据模型（SessionState）；schema version（STATE_SCHEMA_VERSION=2）。

### PI-905-3 Migration 拆分  ✅

- v1（legacy root/default）-> v2；未知版本抛 ValueError。
  （当前最高 schema 为 v2；v3 待有需要时再引入。）

### PI-905-4 Lock 拆分  ✅

- 并发控制：``STATE_FILE_LOCK`` 保护整文件「读改写」，原子写（tmp+replace）。

---

# PI-906 Config 模块化

## 状态：已完成（2026-10-08）

实际落地结构：

```
chatgpt_web/config/
├── __init__.py   兼容 facade：`from ._core import *` + 再导出 domain 类型
├── _core.py      .env 加载 + 全部可调参数 + typed 快照 + build/load/summary
├── server.py     ServerConfig
├── browser.py    BrowserConfig
├── session.py    SessionConfig
├── tools.py      ToolConfig
├── limits.py     LimitConfig（=CompletionConfig 别名）+ StorageConfig
└── debug.py      DebugConfig
```

关键兼容点：

- `chatgpt_web/config.py` 单文件删除，改为包；`import chatgpt_web.config` /
  `from chatgpt_web import config` 仍解析到 `config/__init__.py`。
- `config.<NAME>`（历史代码与测试的 `patch.object(config, ...)`）不变：
  `__init__` 把 `_core` 全部公开名字再导出。
- `build_config_bundle()` 通过 `_facade()` 在**调用时**读 `chatgpt_web.config`
  属性（而非捕获 `_core` 全局），故 `patch.object(config, "HOST", ...)` 生效。
- `PROJECT_ROOT` 修正为 `_core.py.parent.parent.parent`（包比原模块深一层）。

测试：`tests/test_config.py`（含 `ConfigPackageTests`）、`tests/test_config_drift.py`
与 `tests/test_doc_sync.py` 改为扫描 `chatgpt_web/config/*.py`。

## 子任务（全部完成）

### PI-906-1 创建 config package  ✅

- ServerConfig / BrowserConfig / SessionConfig / ToolConfig / LimitConfig / DebugConfig

### PI-906-2 保持环境变量兼容  ✅

- 原 `.env` 不需要修改（键与默认值不变）。

### PI-906-3 增加配置测试  ✅

- 默认值、非法值回退、环境覆盖、包结构、facade 契约。

---

# 实施顺序

1. PI-901 Tool Runtime
2. PI-902 Completion
3. PI-903 Browser
4. PI-904 API Adapter
5. PI-905 Session
6. PI-906 Config

---

# 每个任务完成标准

- 新模块测试完成；
- 原测试无新增失败；
- `git diff --check` 通过；
- 保留兼容 facade；
- 单独 commit。
