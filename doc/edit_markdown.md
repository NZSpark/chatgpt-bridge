# `edit_markdown` 命令设计与实现说明

> 本文根据仓库当前实现整理，重点覆盖命令契约、提示词注入、解析与执行管线、Markdown 结构处理、安全策略、配置、测试和历史演进。代码位置以当前目录结构为准；涉及设计目标与实际保证的区别会单独说明。

## 1. 概览

`edit_markdown` 是 ChatGPTBridge 的内置 Markdown 文件编辑工具。ChatGPT 网页端并不直接向本项目提供 OpenAI 风格的函数调用执行接口，因此项目使用提示词约定，让模型以结构化工具调用的形式提出编辑请求，再由桥接层解析、校验并选择本地执行或返回调用结果。

核心目标：

- 通过明确的行号区间和替换文本进行局部编辑，避免整文件重写。
- 在编辑前扫描 Markdown 围栏，编辑后重新解析并进行结构校验。
- 默认只生成 diff，不因模型请求而自动写盘。
- 将本地文件访问限制在配置的路径沙箱中。
- 真正写盘前创建备份；通过结构化结果返回 diff、写入状态和错误信息。
- 通过执行台账避免同一会话中的相同调用 ID 被重复执行，并记录结果用于排查。

主要实现位置：

| 文件 | 职责 |
| --- | --- |
| `chatgpt_web/tools/parser.py` | 内置工具 schema、工具调用载体解析、`ToolCallRequest` 类型 |
| `chatgpt_web/tools/validator.py` | 工具请求的结构校验、规范化和调用 ID 去重 |
| `chatgpt_web/tools/policy.py` | 工具策略、路径沙箱、写入权限检查 |
| `chatgpt_web/tools/executor.py` | 工具执行管线、本地 `edit_markdown` 执行和结果组装 |
| `chatgpt_web/tools/ledger.py` | 按会话与调用 ID 保存执行记录、缓存结果和并发去重 |
| `chatgpt_web/tools/serializer.py` | 工具执行结果序列化 |
| `chatgpt_web/toolcalls.py` | 兼容 facade、提示词格式化函数和子包符号再导出 |
| `chatgpt_web/markdown_io.py` | Markdown 读取、围栏扫描、行区间替换、diff、校验、备份与回滚 |
| `chatgpt_web/prompting.py` | 工具说明注入、工具调用纠偏及重复空结果提醒等提示词逻辑 |
| `chatgpt_web/config/_core.py` | `EDIT_MARKDOWN_*` 环境配置和配置 bundle |

相关测试包括 `tests/test_markdown_io.py`、`tests/test_toolcalls.py`、`tests/test_tool_injection.py`、`tests/test_prompting.py`、`tests/test_config.py`、`tests/test_property_invariants.py` 和 `tests/e2e/test_markdown_io_e2e.py`。

## 2. 命令契约

### 2.1 工具 schema

`chatgpt_web/tools/parser.py` 定义 `EDIT_MARKDOWN_TOOL_NAME`、`EDIT_MARKDOWN_TOOL` 和 `BUILTIN_TOOLS`。工具名称固定为 `edit_markdown`，参数 schema 为：

| 参数 | JSON 类型 | 是否必需 | 含义 |
| --- | --- | --- | --- |
| `path` | string | 是 | 目标 Markdown 文件的相对路径 |
| `start` | integer | 是 | 开始行号，从 1 开始，包含该行 |
| `end` | integer | 是 | 结束行号，从 1 开始，包含该行 |
| `new_text` | string | 是 | 替换整个 `[start, end]` 区间的新文本 |
| `write` | boolean | 否 | 是否请求写入文件，默认 `false` |

示例调用：

```tool_call
{"name":"edit_markdown","arguments":{"path":"doc/example.md","start":10,"end":12,"new_text":"替换后的内容","write":false}}
```

注入提示词时，`description` 与各字段的 `description` 一律使用**英文**（与项目其他注入文案一致，见 `doc/prompt_design.md`）：

- 工具描述：`Edit a local Markdown file by 1-based inclusive line range. ...`；
- 字段描述：`Path to the target Markdown file, relative to the sandbox root.`、`First line to replace (1-based, inclusive).`、`Last line to replace (1-based, inclusive).`、`Replacement text for lines [start, end]; \n separates lines.`、`true to persist to disk; default false returns only the diff.`

同理，模型可见的错误串（`path rejected (...)`、`line range out of bounds: ...`、`file too large: ...`、
`structural validation failed after edit`、`backup failed, file not written: ...`、`write=true was ignored: ...`）
也都是英文；中文只保留在仓库内部注释与本说明文档里。修改这些字符串时需同步
`tests/test_edit_markdown_sandbox.py` 的断言。

`start` 和 `end` 构成 **1-based 闭区间**。例如 `start=10, end=12` 会替换原第 10、11、12 行，而不是只替换第 10 至 11 行之间的内容。

`new_text` 可以包含换行。空字符串表示该区间不保留替换行，因此可以用来删除选中行。行号、路径和文本均应来自目标文件的实际状态，不能直接照抄说明中的占位示例。

### 2.2 请求写入不等于获准写入

`write=true` 只表示模型提出了写盘请求，不构成写入授权。是否能够写盘还受服务端 `EDIT_MARKDOWN_WRITE` 配置约束。若写入权限关闭，工具会降级为 dry-run，仍返回预览 diff 和说明信息。

## 3. 整体执行流程

逻辑上分为提示词与传输、解析与策略、实际编辑、结果回传四个阶段。

```text
客户端请求 / 对话消息
       |
       v
prompting 注入工具说明和格式要求
       |
       v
模型输出 tool_call 结构化内容
       |
       v
parser 解析为 ToolCallRequest
       |
       v
validator：结构校验、规范化、调用 ID 去重
       |
       v
policy：工具允许列表与路径/写权限策略
       |
       v
executor：执行 edit_markdown
       |
       +--> 路径与文件大小检查
       +--> 读取 Markdown、扫描围栏
       +--> 替换行区间、重新扫描与校验
       +--> dry-run 返回 diff
       |        或
       +--> 备份原文件、写回文件、返回 diff
       |
       v
serializer / 本地执行结果关联 / 响应层
       |
       v
工具结果进入后续对话
```

具体调用入口可能因 API 请求路径和配置而异。`chatgpt_web/tools/executor.py` 是本地执行的共同实现，`server.py` 与 `responses.py` 相关路径共用这一执行逻辑，避免维护两套不同的 Markdown 写入代码。

## 4. 提示词注入与工具调用解析

### 4.1 为什么需要结构化工具调用

ChatGPT 网页端不是项目自己的命令执行器。桥接层通过提示词规定模型输出一个带 `tool_call` 标签的代码围栏，客户端或桥接层再将它解析为结构化调用。

`chatgpt_web/toolcalls.py` 是历史兼容 facade。具体解析职责已拆入 `chatgpt_web/tools/` 子包；旧的 import 路径仍然再导出相关符号，减少迁移对既有调用方的影响。

### 4.2 说明文本与格式纠偏

`toolcalls.edit_markdown_spec()` 提供 Markdown 编辑说明，包含调用示例、行号约定以及代码围栏注意事项。`EDIT_MD_HEADER` 等标题常量用于让提示词去重判断与泄漏检测引用同一标识。

项目还提供通用工具调用纠偏和 JSON 格式纠偏逻辑。解析层支持当前的 `tool_call` 围栏，同时保留对历史 `TOOL_CALL:` 纯文本标记以及其他模型输出载体的兼容处理。兼容能力属于通用工具解析器的一部分，不代表每一种载体都应作为新格式继续注入。

`prompting.py` 中的工具调用判定与纠偏机制会检查回复是否包含可用调用，并区分无调用、载体结构错误和 JSON 语法问题。重复的空工具结果也有专门提醒，用于防止模型把空输出误解为执行失败后不断重复同一命令。

**边界：** 提示词只能引导模型遵守格式和编辑约定，不能代替执行层的参数、路径、结构和权限校验。

## 5. 结构化请求管线

### 5.1 `ToolCallRequest`

`chatgpt_web/tools/parser.py` 定义不可变的 `ToolCallRequest`，统一保存：

- `id`：调用 ID；若模型没有提供有效 ID，构造过程可以生成 ID。
- `name`：工具名称。
- `arguments`：参数字典。
- `source_span`：能够可靠识别时的原始文本区间。
- `raw_text`：诊断用途的原始文本，不应成为执行逻辑的输入依据。

这个类型构成解析结果与执行逻辑之间的类型化边界。

### 5.2 管线步骤

`run_tool_call_pipeline()` 在 `chatgpt_web/tools/executor.py` 中按以下次序工作：

1. `parse_tool_call_requests()`：将文本解析为类型化请求。
2. `validate_tool_call_requests()`：检查请求类型、非空 ID、非空工具名和 arguments 对象；支持可选的单工具参数校验器。
3. `normalize_tool_call_requests()`：清理名称和 ID 两端空白，并复制参数字典。
4. `deduplicate_tool_call_requests()`：丢弃重复的调用 ID，保留不同 ID 的独立调用。
5. `check_tool_call_policy()`：执行允许列表和可选的 `ToolPolicy` 检查。
6. `execute_tool_call_requests()`：通过注入的 executor 边界执行或返回兼容字典。
7. `serialize_tool_call_results()`：序列化结果供上层回传。

如果没有解析出调用，但原始文本包含明确的工具调用标记，则会抛出 `ToolCallParseError`，避免把明显失败的工具调用静默当成普通文本完成。

需要注意：通用验证器主要检查类型化请求的结构，并不自动完整执行每一个工具 schema 的 JSON Schema 验证。`execute_edit_markdown()` 自己还会对路径、行号、文本和文件进行检查。

## 6. Markdown 编辑引擎

实现位于 `chatgpt_web/markdown_io.py`，模块不依赖浏览器或网络。整体采用按行读取、扫描围栏、建立行视图、替换区间和重新校验的思路。

### 6.1 数据结构

- `Line`：保存 1-based 行号、文本、是否在围栏内、语言标签和缩进信息。
- `Fence`：保存围栏起止行、语言、标记字符和标记长度。
- `MdDoc`：保存文档路径、行列表、换行符风格、是否带尾换行及围栏列表。
- `Anchor`：为更高层的定位功能描述行、标题、围栏或正文片段锚点。
- `Issue`：保存结构验证发现的问题代码、说明和行号。
- `Backup`：保存原文件路径、备份路径和文本快照。

### 6.2 读取和围栏扫描

`read_md()` 读取文件后交由 `parse_md()` 解析；`parse_md()` 构建行对象并调用 `scan_fences()`，随后由 `_mark_fences()` 标记围栏范围。

围栏扫描采用状态机，主要规则为：

- 行首允许最多 3 个空格缩进。
- 开围栏使用至少 3 个反引号或波浪号。
- 闭围栏必须使用相同类型的标记，长度不得短于开围栏，且标记之后只能有空白。
- 4 个空格缩进视为缩进代码，不作为围栏开始。
- 反引号开围栏的信息串不能包含反引号。
- 未闭合的围栏抛出 `MarkdownError`，不会默默继续编辑。

语言标签取开围栏信息串的第一个单词。开围栏和闭围栏行本身也会被标记为位于围栏内。

### 6.3 行区间替换

`apply_edit(doc, start, end, new_text)` 执行 1-based 闭区间替换：

1. 检查行号是否在文件范围内且区间合法。
2. 将替换文本中的换行统一为 `\n`，再切分成替换行。
3. 拼接区间前的旧行、替换行和区间后的旧行。
4. 重新创建行对象并扫描全部围栏。
5. 返回新的 `MdDoc`，保留文档原有的换行风格和尾换行元数据。

这种设计避免模型根据大段文件内容重写整份文档，减少误改周边内容的风险。对于通常的统一换行文件，未选中的行内容、空行、缩进和尾部空格都由行视图保留。

但需要区分**设计目标**和**字节级保证**：文件 I/O 使用 `Path.read_text()` / `Path.write_text()` 的文本模式，Python 的通用换行处理可能规范化 CRLF；当前实现也会规范化解析过程中遇到的换行。因而不能把所有文件情况都描述为严格的区间外字节级不变，尤其是 CRLF 或混合换行文件。`parse_md()` 的内存往返测试覆盖 CRLF 情况，不等同于所有真实文件 I/O 场景均能逐字节保真。

### 6.4 结构校验

编辑完成后，`verify()` 检查解析结果中的围栏范围及语言标签保留情况。由于 `parse_md()` 会在未闭合围栏时直接失败，`verify()` 主要检查重新编辑后仍可解析的结构是否有异常迹象。

`execute_edit_markdown()` 在验证失败时返回 `ok=false` 和 `issues` 数组，不继续写盘。

围栏提示和重新扫描的作用是保护 Markdown 结构，不是语义审查。执行器不会判断替换内容是否符合文档主题，也不会自动保证每一个围栏内容都保持原样。如果请求区间包含围栏行，执行器实际依赖重新解析与校验来判断结果是否可接受；是否应该编辑围栏内容仍需要由请求意图和上层规则决定。

### 6.5 Diff、写回与备份

- `write_md(doc, dry_run=True)` 生成统一 diff，不落盘。
- 实际写回时，`write_md()` 先生成临时文件，再使用 `replace()` 替换目标文件。
- `_unified_diff()` 使用 unified diff 格式返回更改前后的差异。
- `backup_md()` 在指定备份目录中创建带时间戳的 `.bak` 文件。
- `rollback_md()` 通过临时文件和替换操作恢复备份文本。
- `render_view()` 为模型生成带行号和围栏边界标记的文本视图。

备份与回滚是可调用的独立功能。正常执行 `edit_markdown` 时，真正写盘前创建备份；备份失败会阻止写盘。dry-run 不需要创建备份。

## 7. 本地执行实现

### 7.1 执行入口

`run_local_edit_markdown()` 遍历上游结构化调用：

- 未启用本地执行或没有调用时，原样返回输入。
- 名称为 `edit_markdown` 的调用进入本地执行。
- 其他工具调用原样透传，并保持顺序。
- 执行结果挂回对应调用，供上层回传。

该函数为 `server` 与 `responses` 相关路径提供共用执行实现。

### 7.1.1 同批对同一文件的多次调用

模型在一轮里可能对**同一个文件**给出多个 `edit_markdown`（例如一次请求改多处）。这些调用的
`start` / `end` 都是基于**同一份原始文件**的绝对行号，而每次编辑都可能改变行数。若按模型给出的
顺序从前往后执行，后一个调用的行号就会指向偏移后的内容——轻则静默改错位置，重则行号越界。

实现规则：

- 按 `path`（`arguments.path` 原样去空白）分组；组内**按 `start` 降序**执行（先改后面的行）。
- 组内超过 1 个调用时记一条 INFO 日志，便于从运行日志确认发生过重排。
- **返回列表仍保持模型给出的原始顺序**，客户端看到的 `tool_call_id` ↔ 结果对应关系不变。
- 缺省或非法的 `start` 视为 0（排到最后执行），实际执行时仍由 `execute_edit_markdown()` 报错。

提示词侧同步约定（`toolcalls.edit_markdown_spec()`）：同一文件多次编辑请按 `start` 降序给出，
或合并为一次编辑。这是对模型行为的第一道引导；上面的执行期重排是兜底，不依赖模型是否遵守。

回归用例：`tests/test_edit_markdown_sandbox.py::
RunLocalEditMarkdownTests::test_same_file_three_edits_front_to_back_do_not_drift`。

### 7.2 `execute_edit_markdown()` 的处理顺序

1. 取得 `path` 并调用 `resolve_edit_path()`。
2. 解析 `start`、`end`，验证 `new_text` 是字符串。
3. 读取 `write` 请求，并与 `EDIT_MARKDOWN_WRITE` 合并得到最终写盘决定。
4. 检查目标文件是否存在、能否访问及文件大小是否超过配置上限。
5. 调用 `read_md()`；未闭合围栏等 Markdown 结构错误会作为结构化错误返回。
6. 检查行号区间，调用 `apply_edit()`。
7. 调用 `verify()`；结构检查有问题则拒绝继续。
8. 若获准写盘，先执行 `backup_md()`；备份失败时不写入。
9. 调用 `write_md()` 输出 diff，并根据写盘决定落盘或 dry-run。
10. 返回带路径、行区间、请求写盘状态、实际写盘状态、备份路径与 diff 的结果。

### 7.3 成功结果

成功结果包含以下字段：

| 字段 | 含义 |
| --- | --- |
| `ok` | 操作是否成功 |
| `path` | 请求使用的路径 |
| `resolved_path` | 沙箱校验后的解析路径 |
| `start`、`end` | 实际替换区间 |
| `write_requested` | 是否请求写盘 |
| `written` | 是否真正写盘 |
| `backup` | 成功创建的备份路径；dry-run 时为 `null` |
| `diff` | unified diff |
| `note` | 请求写盘但权限关闭时的说明，可选 |

常见错误包括路径越界、文件不存在、文件过大、行号非法、替换文本类型错误、未闭合围栏、编辑后校验失败和备份失败。执行器通常返回 `{"ok": false, "error": "..."}`，结构校验失败时还会包含 `issues`。

当前执行器会通过 `int()` 转换行号、通过 `bool()` 解释 `write`。调用方应遵循 schema，传入真正的 JSON integer 与 boolean，而非字符串形式的数字或布尔值；不要依赖宽松转换来纠正错误类型。

## 8. 路径沙箱与写入安全

### 8.1 `resolve_edit_path()`

`chatgpt_web/tools/policy.py` 对模型提供的路径进行约束：

- `path` 必须是非空字符串。
- 拒绝绝对路径。
- 拒绝路径组成部分中出现 `..`。
- 将配置的沙箱根目录解析为绝对路径。
- 解析目标路径后，要求其仍位于沙箱根目录内。
- 返回解析后的路径，供后续读写使用，而不是把未经处理的相对路径交给文件操作。

利用解析后的路径进行包含关系检查，可防止路径穿越，也能检测解析后指向沙箱外部的符号链接目标。

### 8.2 `ToolPolicy`

`ToolPolicy` 定义可允许的工具集合、允许路径、写入权限以及网络、运行时间和输出等策略字段。当前显式具备专用策略逻辑的是 `edit_markdown`；其他策略字段为策略边界预留扩展空间，并不表示每一种限制都已经由该命令独立实现。

如果提供完整 `ToolPolicy`，策略层会检查工具是否获准、是否申请了禁止的写入，以及路径是否满足沙箱和额外的 `allowed_paths` 限制。即使没有显式提供完整策略，本地执行器也会在执行过程中调用路径解析逻辑。

### 8.3 两道写入门槛

写入决策由两个相互独立的条件约束：

1. 模型请求参数 `write=true`。
2. 服务配置 `EDIT_MARKDOWN_WRITE=true`。

二者必须同时满足。模型不能通过在参数中写 `true` 自行开启服务端权限。成功写盘前还必须完成结构验证和备份。

### 8.4 风险与运行建议

`edit_markdown` 是一个可受模型输出影响的本地文件工具。即使提示词或网页内容受到提示注入影响，路径沙箱与写入门槛也应继续生效。建议：

- 平时保持 `EDIT_MARKDOWN_WRITE=false`，先检查 diff。
- 将 `EDIT_MARKDOWN_ROOT` 设置为需要操作的最小项目目录，而不是不必要地扩大访问范围。
- 使用可写盘配置时确认备份目录可用，并定期检查备份。
- 不要让包含不可信内容的会话获得超过工作需要的本地文件写入范围。
- 把路径权限、服务绑定地址和本地会话安全作为整体安全边界来管理。

需要注意的是，备份文件名使用秒级时间戳加原文件名；高频连续备份同一文件时，应留意同一秒内文件名冲突的可能性。原子替换减少了直接截断目标文件的风险，但并不自动提供跨进程事务、文件锁或崩溃一致性保证。

## 9. 执行台账、幂等性和可观测性

`chatgpt_web/tools/ledger.py` 中的 `ToolExecutionLedger` 使用 `(session_key, tool_call_id)` 作为键：

- `claim()` 决定调用是否为当前执行者，或者是否应等待正在进行的调用。
- `get()` 读取已有结果。
- `record()` 保存规范化参数、执行耗时、成功状态、错误类型、结果哈希与结果快照。
- `complete()` 写入记录并唤醒等待同一调用的线程。
- `snapshot()` 返回当前记录集合。

在 `run_local_edit_markdown()` 中，重复的调用 ID 会复用原结果，避免相同会话中的重放请求再次写文件。执行时还会更新工具调用指标，记录耗时与失败次数，并通过日志上下文关联 `tool_call_id`。

这套台账是进程内的内存结构，不能直接等同于跨进程或跨重启持久化的幂等存储；其去重范围由会话键和调用 ID 决定。若一个新的逻辑操作使用新的调用 ID，就不能仅靠该台账判断其语义上是否与之前的操作重复。

## 10. 配置说明

配置声明位于 `chatgpt_web/config/_core.py`，并进入工具配置 bundle。`.env.example` 与 README 也记录这些选项。

| 环境变量 | 代码默认值 | 作用 |
| --- | --- | --- |
| `EDIT_MARKDOWN_LOCAL` | `false` | 是否由桥接层本地执行模型发出的 `edit_markdown` 调用 |
| `EDIT_MARKDOWN_WRITE` | `false` | 是否准许实际写盘；关闭时即使请求 `write=true` 也只预览 |
| `EDIT_MARKDOWN_BACKUP_DIR` | `output/backups` | 写盘前备份目录 |
| `EDIT_MARKDOWN_ROOT` | `PROJECT_ROOT` | 路径沙箱根目录 |
| `EDIT_MARKDOWN_MAX_FILE_BYTES` | `4194304` | 单文件读取上限，默认 4 MiB；小于等于 0 时当前大小检查不启用 |

**配置细节：** `.env.example` 中的示例值将 `EDIT_MARKDOWN_LOCAL` 写成了 `true`，但 Python 代码和 README 表格的默认值为 `false`。示例环境文件是用户复制后再配置的模板，不应与代码默认值混为一谈。部署时必须检查实际 `.env` 和启动配置，确认是否自动本地执行。

`EDIT_MARKDOWN_LOCAL=false` 时，工具仍可能作为结构化调用被解析，但本地执行函数会原样返回调用，由上层调用方决定如何处理。不要把“工具 schema 已注册”与“桥接层必定执行本地文件操作”视为同一件事。

## 11. 两种 Markdown 编辑路径的区别

项目还在 `markdown_io.py` 中保留一套较高层的编辑辅助函数：

- `render_view()` 生成带行号与围栏标记的文本视图。
- `generate_edit()` 调用传入的 LLM，解析模型产生的 `edit_markdown`，返回 `(start, end, new_text)`。
- `_build_edit_prompt()` 构建面向该辅助流程的局部编辑提示词。

这套接口面向已经读取到内存中的 `MdDoc`，其工具参数示例不要求 `path` 和 `write`。它与供 API 工具调用使用的 `execute_edit_markdown(args)` 不是同一入口。前者产出建议的行区间与新文本，后者负责目标路径、权限、备份和实际执行。

同样，`Anchor` 和 `locate()` 提供 line、heading、fence、text 四种定位能力，但当前暴露的内置 `edit_markdown` schema 使用显式 `start` / `end` 行号，不会自动把标题或文本锚点转换成行号。若调用方希望按标题定位，需要先通过其他逻辑确定行区间。

## 12. 测试覆盖地图

| 测试文件 | 主要验证内容 |
| --- | --- |
| `tests/test_edit_markdown_sandbox.py` | 路径沙箱、写入门槛、同批同文件多调用的执行顺序与返回顺序 |
| `tests/test_markdown_io.py` | 文档读取、LF/CRLF 内存解析、尾换行、围栏状态机、定位、替换、diff、备份、回滚和结构验证 |
| `tests/test_toolcalls.py` | 工具调用格式、解析与修复、类型化请求、工具管线、执行台账和本地 Markdown 执行相关用例 |
| `tests/test_tool_injection.py` | 内置工具说明、示例 JSON 可解析性、注入格式和去重相关契约 |
| `tests/test_prompting.py` | 提示词构建、工具调用判定与纠偏等行为 |
| `tests/test_config.py` | 配置加载和工具配置 bundle 的值传递 |
| `tests/test_property_invariants.py` | 解析不变量及路径沙箱边界等属性验证 |
| `tests/e2e/test_markdown_io_e2e.py` | 真实网页会话中的 Markdown 编辑端到端流程 |
| `tests/test_api_contracts.py`、`tests/test_api_package.py` | API 注册、接口兼容与服务入口相关契约 |

阅读和修改后建议优先运行与改动最接近的单元测试，再运行整个测试套件。涉及真实模型的 E2E 测试还受网页会话、模型是否遵守工具调用约定和上游服务可用性影响，应把环境不可用与代码回归区分开来。

本说明不宣称某次测试已经通过；测试结果应以实际运行记录为准。

## 13. 历史设计演进与问题处理

本功能相关的设计变化散落在代码注释、`doc/update.md` 和 `doc/code_block_fence.md` 中，主要脉络如下：

1. **从文本标记转向代码围栏。** 2026-10-06 起，主注入格式改为 `tool_call` 代码围栏。项目记录了网页 Markdown 渲染会破坏纯文本标记中的反斜杠转义或连续空格的问题，因此保留旧标记解析兼容，但不再把旧格式作为首选注入形式。
2. **加强调用示例的合法性。** 历史示例出现过尖括号占位符、无效 JSON 和参数类型与 schema 不匹配的问题。相关提示词和测试逐步改为可以解析的字面示例，并明确说明占位内容必须替换。
3. **执行逻辑拆分。** PI-901 将通用工具调用逻辑拆分为 parser、validator、policy、executor、ledger 与 serializer，`toolcalls.py` 保留兼容 facade。
4. **增加路径沙箱。** 历史审查曾指出 `execute_edit_markdown` 直接使用模型提供路径会造成任意文件读写风险。当前实现通过 `resolve_edit_path()` 拒绝绝对路径、`..` 段及解析到沙箱之外的目标，并另设全局写入门槛。
5. **合并本地执行入口。** `run_local_edit_markdown()` 抽出为 server 与 responses 共用的实现，避免两个入口的本地执行逻辑逐渐不一致。
6. **增强重复调用防护。** 执行台账对相同会话与调用 ID 进行去重；提示词侧也对重复且总是空输出的工具调用做专门提醒。
7. **更明确地检查端到端调用。** 历史更新记录了模型未返回 `edit_markdown` 时曾有 E2E 用例直接跳过的情况。后续测试调整倾向于把重试后仍未产生调用记录为失败或软失败，而不是把它显示成普通跳过。当前运行状态仍应以最新测试输出为准。

历史背景的详细记录可参阅 `doc/update.md`、`doc/code_block_fence.md` 及 `tests/e2e/test_markdown_io_e2e.py`。

## 14. 当前实现的关键边界与后续检查建议

1. **逐字节保真需要实测。** 文档接口的设计目标是仅替换指定行、保留区间外内容，但文本 I/O 的换行规范化意味着 CRLF 与混合换行场景不能只根据内存模型断言字节级保真。值得补充真实文件 I/O 的 CRLF 与混合换行往返测试，并按需要改用显式控制换行的读写方法。
2. **schema 与执行器类型校验并非完全等价。** `start` / `end` 当前通过 `int()` 转换，`write` 通过 `bool()` 转换。若希望严格拒绝错误 JSON 类型，可补充专用参数 validator，或在执行器中先进行严格类型检查。
3. **备份文件名冲突。** 当前名称精度为秒级，连续备份同名文件可能撞名。若频繁写盘是受支持的使用方式，可改进为高精度时间戳或唯一后缀，并增加碰撞测试。
4. **策略扩展要保持真实。** `ToolPolicy` 暴露的字段不应被文档或调用方误认为都已实施。新增工具时应明确哪些字段由策略层、执行器或外部调用方真正执行。
5. **同批多调用的重叠区间尚未拦截。** 当前只做「按 `start` 降序执行」，区间重叠时仍会依次叠加
   应用（等于以后一次的新内容为基础再改）。若认为这属于误用，应在执行前拒绝重叠区间并把错误
   回灌给模型，而不是静默叠加。
6. **保留最小权限和 dry-run。** 本地执行开关、路径沙箱、全局写入门槛、结构校验、备份与台账是互补措施，任何一个都不应被提示词约定取代。
6. **将端到端失败分类清楚。** 模型未调用工具、工具 JSON 格式错误、路径策略拒绝、Markdown 结构错误以及网页/模型环境不可用属于不同故障，应在测试与日志中保持可区分。

## 15. 总结

`edit_markdown` 的关键设计并不只是一个按行替换函数，而是围绕模型生成的本地文件修改请求建立完整的受控执行链：schema 明确输入、提示词规定载体、解析器恢复结构化请求、validator 与 policy 控制请求边界、Markdown 引擎检查结构并生成 diff、执行器落实 dry-run/备份/写入门槛，最后通过台账与结构化结果帮助调用方去重和排错。

最重要的使用原则是：**先生成并审查 diff，再在明确授权下写盘；只在沙箱内操作；任何结构或权限错误都不应通过提示词重试来绕过。** 同时，应持续以真实文件 I/O 测试验证换行保真、严格参数类型和备份唯一性等容易被接口描述掩盖的边界条件。
