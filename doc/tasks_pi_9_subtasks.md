# P2：模块拆分子任务分解

> 来源：doc/tasks_pi_9.md
> 目标：将模块拆分建议转换为可执行工程任务。

## 总体原则

- 保持现有 API 行为不变。
- 优先新增模块，再迁移调用。
- 保留旧 import 路径，通过 facade 兼容。
- 每个子任务独立提交。
- 每阶段必须增加测试并通过 `pytest -q`。

---

# PI-901 Tool Runtime 拆分

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

## 子任务

### PI-905-1 创建 session package

结构：

```
session/
├── schema.py
├── store.py
├── migration.py
└── lock.py
```

### PI-905-2 Schema 拆分

负责：

- session 数据模型；
- schema version。

### PI-905-3 Migration 拆分

负责：

```
v1 -> v2 -> v3
```

### PI-905-4 Lock 拆分

负责：

- 并发控制；
- timeout。

---

# PI-906 Config 模块化

## 子任务

### PI-906-1 创建 config package

拆分：

- ServerConfig
- BrowserConfig
- SessionConfig
- ToolConfig
- LimitConfig
- DebugConfig

### PI-906-2 保持环境变量兼容

验收：

- 原 `.env` 不需要修改。

### PI-906-3 增加配置测试

覆盖：

- 默认值；
- 非法值；
- 环境覆盖。

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
