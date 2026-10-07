# P2：模块拆分详细建议

> 来源：doc/tasks_pi.md # 9. P2：模块拆分
> 目标：降低单模块复杂度，提高可维护性、测试隔离能力和后续功能扩展能力。

---

# 1. 背景与目标

当前 ChatGPTBridge 已经完成状态机、事件模型、Tool Runtime、DOM Adapter、异常层等基础治理，但随着功能增加，核心模块容易继续膨胀：

- `chat_io.py` 同时承担浏览器输入、消息采集、生成等待、回复提取、错误处理；
- `completion.py` 同时承担请求生命周期、浏览器交互、协议转换、stream 控制；
- `toolcalls.py` 同时承担解析、验证、策略、安全、执行和序列化；
- `server.py`、`responses.py`、`streaming.py` 存在协议逻辑重复；
- 配置、session、任务管理、诊断逻辑逐渐交叉。

本阶段目标不是简单拆文件，而是建立清晰边界：

1. 单一职责；
2. 降低模块间耦合；
3. 保持现有 API 行为不变；
4. 为未来多浏览器、多模型、多后端扩展准备接口。

---

# 2. 总体目标架构

建议逐步演进为：

```
chatgpt_web/
│
├── api/
│   ├── chat_adapter.py
│   ├── responses_adapter.py
│   └── streaming_adapter.py
│
├── browser/
│   ├── driver.py
│   ├── page_pool.py
│   ├── dom_adapter.py
│   ├── selectors.py
│   └── diagnostics.py
│
├── completion/
│   ├── runner.py
│   ├── generator.py
│   └── extractor.py
│
├── tools/
│   ├── parser.py
│   ├── validator.py
│   ├── policy.py
│   ├── executor.py
│   ├── ledger.py
│   └── serializer.py
│
├── session/
│   ├── store.py
│   ├── schema.py
│   ├── migration.py
│   └── lock.py
│
├── config/
│   ├── server.py
│   ├── browser.py
│   ├── tools.py
│   └── limits.py
│
├── events.py
├── errors.py
└── models.py
```

---

# 3. 第一阶段：Tool Runtime 拆分（最高优先级）

## PI-901 toolcalls.py 拆分

优先级：P2

当前问题：

`toolcalls.py` 已包含：

- parser；
- JSON 修复；
- typed request；
- validator；
- policy；
- executor；
- ledger；
- serialization。

文件继续增长会降低维护效率。

## 建议拆分

### tools/parser.py

负责：

- tool_call fence 解析；
- JSON extraction；
- shell fence fallback；
- malformed repair。

输入：

```
model text
```

输出：

```
ToolCallRequest
```

---

### tools/validator.py

负责：

- 参数类型检查；
- required fields 检查；
- schema validation。

不负责：

- 权限；
- 执行。

---

### tools/policy.py

负责：

```
allowed_tools
allowed_paths
write_enabled
runtime_limit
network_policy
```

输出：

```
allow / deny
```

---

### tools/executor.py

负责：

```
ToolCallRequest
        ↓
execute()
        ↓
ToolResult
```

不关心模型如何生成调用。

---

### tools/ledger.py

独立保存：

```
session_key
tool_call_id
tool_name
arguments
status
duration
result_hash
```

支持：

- 并发去重；
- 重试恢复；
- 调试审计。

---

验收标准：

- 原 `toolcalls.py` 保留兼容入口；
- 所有旧测试通过；
- 新模块拥有独立测试。

---

# 4. 第二阶段：Completion Pipeline 拆分

## PI-902 completion.py 拆分

当前 completion 负责过多生命周期逻辑。

建议：

## completion/runner.py

负责任务生命周期：

```
request
 ↓
create task
 ↓
generate
 ↓
finish
```

---

## completion/generator.py

负责：

- 输入发送；
- 等待生成；
- streaming delta。

---

## completion/extractor.py

负责：

- assistant reply 提取；
- code block 提取；
- tool call 检测。

---

验收：

- completion.py 成为 facade；
- 无业务逻辑继续增加。

---

# 5. 第三阶段：Browser 层拆分

## PI-903 browser package

目标：隐藏 Playwright 细节。

---

## browser/driver.py

负责：

- browser 生命周期；
- page 创建；
- context 管理。

---

## browser/dom_adapter.py

已有基础，继续扩大：

统一：

```
find_input()
find_reply()
find_stop()
find_new_chat()
```

---

## browser/selectors.py

集中：

- selector 常量；
- fallback selector；
- selector version。

禁止业务代码直接出现 CSS selector。

---

## browser/diagnostics.py

负责：

- DOM probe；
- screenshot；
- debug dump。

---

# 6. 第四阶段：API Adapter 拆分

## PI-904 server.py 轻量化

目标：server 只负责：

```
HTTP request
      ↓
Adapter
      ↓
Response
```

---

拆分：

### api/chat_adapter.py

Chat Completions：

- messages 转换；
- response 格式化。

### api/responses_adapter.py

Responses API：

- input/output item；
- tool call mapping。

### api/streaming_adapter.py

统一：

- SSE chunk；
- delta event。

---

# 7. 第五阶段：Session 模块化

## PI-905 session package

拆分：

### session/schema.py

负责数据结构。

### session/store.py

负责保存读取。

### session/migration.py

负责：

```
v1 -> v2 -> v3
```

### session/lock.py

负责并发控制。

---

# 8. 配置模块拆分

## PI-906 config package

从单一 config.py 演进：

```
ServerConfig
BrowserConfig
SessionConfig
ToolConfig
LimitConfig
DebugConfig
```

要求：

- 保持 env 兼容；
- 提供统一 snapshot；
- 测试默认值。

---

# 9. 拆分实施顺序

推荐顺序：

|阶段|模块|风险|优先级|
|-|-|-|-|
|1|tools|低|最高|
|2|completion|中|高|
|3|browser|中|高|
|4|api adapter|低|中|
|5|session|低|中|
|6|config|低|中|

---

# 10. 每阶段验收要求

每次拆分必须：

1. 保留旧 import 路径；
2. 增加 facade；
3. 新增单元测试；
4. pytest -q 无新增失败；
5. git diff --check 通过；
6. 不改变 API 行为。

---

# 11. 风险控制

禁止一次性大重构。

采用：

```
新增模块
 ↓
迁移一个调用点
 ↓
测试
 ↓
删除旧内部实现
```

每个拆分保持一个可独立提交的 commit。

---

# 12. 最终目标

完成后核心代码结构应达到：

- API 层不了解浏览器细节；
- Browser 层不了解 OpenAI 协议；
- Tool 层不了解网页实现；
- Session 层不了解请求协议；
- Config 层统一提供运行参数。

最终形成可持续演进的 Bridge Kernel 架构。
