# 提示词结构设计

本文规定发往 ChatGPT 网页版的**单轮输入文本**的组装规则，是客户端（Pi / Codex / 任意
OpenAI 兼容调用方）与本桥接之间的契约。

**语言分工：本项目注入的提示词文本一律英文**（区段标题、注入块、纠偏提示词都是英文常量）；
**本文档用中文**。模型可用用户的语言作答，但脚手架本身不含中文。

## 1. 三块结构

一轮输入文本由**三个有序区段**组成，区段内部可缺省，但**相对顺序固定**：

| # | 区段 | 来源 | 语义 |
| --- | --- | --- | --- |
| 1 | `[Tool Calling Instructions]` | `format_tools_instruction(tools)` | 契约：身份链路 + 工具清单 + 调用规则 |
| 2 | 本轮任务（互斥二选一） | `_render_message` | `[User Request]` 或 `[Tool Results <id>]` |
| 3 | `[Repeated Empty Tool Call]` | `format_repeat_call_hint` | 近因保险：禁止重发空输出命令 |

**顺序是 契约 → 任务 → 纠偏。** 三块不嵌套、不交错。

### 1.1 为什么契约在最前（与 DeepseekBridge 相反）

DeepseekBridge 的排法是「任务 → 结果 → 环境与要求」，把契约放最后，理由是避免模型把格式
说明当成任务。**本项目的实测结论相反**（`doc/tasks.md` T1.1，E2E C1/C2 失败）：

- 播种时契约与真正的任务之间隔着环境说明与几百行历史，模型**直接凭自身知识编造**
  （编了一份北京天气）。
- 本项目的主要故障模式不是「把说明当任务」，而是**根本不调用工具**。

因此顺序按「提高调用率」排：契约在最前（增量路径位置 0、播种路径紧贴任务之前），
纠偏在最后（近因效应）。这与 DeepseekBridge 的核心原则一致——**分区、有序、不交错**——
只是把「契约」放在前而不是后。

### 1.2 第 2 块内部互斥

第 2 块**要么是用户任务、要么是工具结果，不同时出现**：

- 有工具结果 → 用户指令已提交 → 只发 `[Tool Results <id>]`
- 没有工具结果 → 尚无执行 → 只发 `[User Request]`

这是 DeepseekBridge I2/I3 同一判据的两种表现：*这条 user 有没有已经落进网页会话*。
一旦落进去，既不该重发（I2），它触发的工具结果也不该在下一轮之后再重发（I3）。

增量路径以「最后一条 assistant 之后」切分，畸形输入（旧 tool 结果 + 新 user）时旧结果自然被排除。

## 2. 区段标题常量

| 常量 | 值 | 用途 |
| --- | --- | --- |
| `TOOLCALL_HEADER` | `[Tool Calling Instructions]` | 契约块标题；**去重判定也用它** |
| `EMPHASIS_HEADER` | `[Output Format Emphasis]` | 播种首轮的格式兜底块 |
| `EDIT_MD_HEADER` | `[edit_markdown notes]` | edit_markdown 用法补充 |
| `RETRY_HEADER` | `[Tool Call Correction]` | 首轮未调用工具的纠偏 |
| `JSON_FORMAT_HEADER` | `[Tool Call JSON Format Error]` | JSON 语法错误的纠偏 |
| `REPEAT_HEADER` | `[Repeated Empty Tool Call]` | 重复空输出点名 |
| `SECTION_USER` | `[User Request]` | 用户任务块 |
| `SECTION_TOOL_RESULTS` | `[Tool Results]` | 工具结果块（渲染为 `[Tool Results <id>]`） |
| `CONTEXT_REBUILD_HEADER` | `[上下文重建]` | 播种路径的历史重放头 |
| `ENV_NOTE_HEADER` | `[环境说明]` | 播种路径的环境说明头 |

标题常量的唯一用途是**让模型区分段落语义**，以及供生成 / 去重 / 泄漏检测共用同一字符串。

## 3. 组装算法

```
build_prompt(messages, tools, tool_choice, seed, seed_max_chars, task_block):
    if seed:
        systems, kept, truncated = _seed_messages(messages, budget)
        parts = ["[上下文重建] …", SEED_ENV_NOTE]
        if task_block: parts.append(task_block)      # 永不被尾部截断丢掉
        if truncated:  parts.append("（更早的部分…已省略）")
        if use_tools:  parts.insert(1, format_tool_call_emphasis(tools))
        used_messages = systems + kept
    else:
        parts = []
        used_messages = _run_messages(messages)      # 最后一条 assistant 之后

    parts.extend(_render_message(m) for m in used_messages)

    if use_tools and TOOLCALL_HEADER not in inbound:
        增量：parts.insert(0, format_tools_instruction(tools))     # 契约在最前
        播种：parts.insert(before_last_rendered, format_tools_instruction(tools))

    if edit_markdown declared and EDIT_MD_HEADER not in inbound:
        parts.append(edit_markdown_spec())

    if 有空结果 且 该命令重复 ≥2 次:
        parts.append(format_repeat_call_hint(repeats))              # 纠偏在最后

    return "\n\n".join(非空 parts).strip()
```

### 3.1 增量路径：什么算「新增」

`_run_messages()` 取最后一条 assistant 之后的消息，然后丢弃：

- 全部 `system` 消息——harness 每轮重发完整系统提示；网页会话已带着它，重发既浪费上万
  字符又会被当成新指令；
- harness 元提示（`_is_meta_prompt`）与环境包装块（`_is_environment_wrapper`）。

若为空，退回最后一条真实 user 消息。

### 3.2 播种路径：预算与截断

`_seed_messages()` 在 `SEED_MAX_CHARS`（12000）内保留最近的消息：system 每条按
`SEED_SYSTEM_MAX_CHARS`（2000）截断、总量不超过预算一半；其余从新往旧累加。
`task_block` 插在历史之前，尾部截断丢不到它（「轮转不丢任务」）。

## 4. 各块内容

### 4.1 契约块 `[Tool Calling Instructions]`

由四部分组成，全部英文：

1. **身份链路段**（承重，见 §4.1.1）
2. **一个具体示例调用**——用第一个工具的真实名字与真实必填参数键生成，占位值类型正确
3. **工具清单**——默认 `name(required,keys): description`，描述按句末/词边界截断到 200 字符
4. **规则 1–9**——载体、参数类型、单行 JSON、命令原样、最多三条、只输出围栏块、
   禁止以「我没有工具」拒答、空输出处理、禁止 XML/DSL

#### 4.1.1 身份链路段（防拒答）

> You produce text only; you cannot run anything yourself. This conversation is read by a
> local client running on the user's machine: it executes the tool calls you write and
> returns the real output to you as your next message. No tool has to be installed or
> mounted on your side, and the tools below will never appear in your built-in tool list -
> that is expected and normal… Writing one is not fabrication.
> Calling a tool is the only way you take real action here… the task FAILS.

这段措辞是**承重的**。两种旧写法都导致过拒答：

| 旧措辞 | 模型反应 |
| --- | --- |
| “You have NO direct access to a shell, filesystem…” | 「本会话没挂载工具，伪造调用违反真实状态」→ 拒答，给用户一份补丁 |
| “You are an autonomous agent… tools are ALREADY MOUNTED and LIVE” | 「这里没有挂载 read/write/edit/bash 执行器，所以不能真的发 tool_call」→ 拒答 |

两种读法都把提示词变成**关于模型自身能力的问题**。正确框架只描述一条链路：模型写文本 →
客户端执行 → 结果作为下一条消息回来。永远不断言模型「有」或「没有」什么。

#### 4.1.2 唯一载体

标签恰好为 `tool_call` 的代码围栏，内含一个 JSON 对象。背景（`doc/update.md` §2.14）：网页版把
**纯文本**调用行当 Markdown 渲染，会吃掉反斜杠（JSON 失效）并折叠缩进（命令被改写）；
放进围栏则一字不差。规则 1–4 分别约束标签、参数类型、单行转义、命令放在 `command` 值里。

**不做负向示范**：规则从不写出被禁的纯文本调用形态（列举即示范）。

#### 4.1.3 精简原则

提示词只保留**可执行约束**，删掉：

- 重复表述（同一约束只说一次）
- 解释性长句（为什么不能这样做）
- 示例之外的格式化样板

被删的语义点（如「空输出是正常结果」「命令必须放进 command 值」）以最短可读形式保留在
规则里，并继续由测试断言守护，避免精简时悄悄丢约束。

### 4.2 任务块

| 块 | 渲染 |
| --- | --- |
| `[User Request]` | 用户原文（`strip()` 后） |
| `[Tool Results <id>]` | 工具正文**逐字节保留**；超 `TOOL_RESULT_MAX_CHARS` 显式截断；空输出替换为说明 |
| `[System]` | 仅播种路径 |
| `[Your Previous Reply]` | 历史助手回复 |

工具正文**不得 `strip()`**：Pi/Codex 的 `read` 结果会原样作为 `edit` 的 `oldText`，
抹掉首尾空白会导致精确匹配失败。

### 4.3 纠偏块 `[Repeated Empty Tool Call]`

同一命令已发 ≥2 次且每次结果为空时，在 prompt **末尾**点名命令与次数，说明重复是死循环。

## 5. 空输出（死循环防护）

实测：命令成功但没有 stdout，harness 渲染成 `(no output)`，模型理解成「命令没生效」，
**原样重发同一条命令**，无限循环。两层防护：

1. **逐条结果**——`_is_empty_tool_output()` 剥掉外壳行（命令回显、`Took Xs`、`Wall time`、
   退出码、token 统计、空 `Output:`、分隔线）与空标记后，若什么都不剩，正文替换为
   `EMPTY_TOOL_RESULT_NOTE`（英文）。
2. **点名提醒**——`_repeated_empty_calls()` 通过 `tool_call_id` 映射回调用、按签名计数，
   ≥2 次时追加 `format_repeat_call_hint()`。

只有空输出路径替换正文；真实结果逐字节保留。

## 6. 不变量（每条都有守护测试）

| # | 不变量 | 违反后果 |
| --- | --- | --- |
| I1 | 标题常量是去重与泄漏检测的唯一键 | 去重恒不生效 |
| I2 | 增量路径丢弃全部 system 与 harness 噪声 | 完整系统提示每轮当新指令重发 |
| I3 | 工具正文逐字节保留（仅截断与空输出替换例外） | `edit` 找不到 `oldText` |
| I4 | 契约块最多注入一次（对**入站消息**去重） | 两份说明互扰；强调块被误判为已存在 |
| I5 | 契约块先于任务 | 模型凭知识作答 |
| I6 | 播种轮开头有强调块 | 新会话退回原生 DSML 标记 |
| I7 | `task_block` 在历史之前 | 轮转丢任务 |
| I8 | 提示词里每个示例调用都可解析 | 模型照抄非法模板 |
| I9 | 空输出不原样透传；重复 ≥2 次被点名 | 无限重跑循环 |
| I10 | 三块顺序固定为 契约 → 任务 → 纠偏 | 模型把契约当任务，或把纠偏当任务 |

### 6.1 去重针对入站消息

`TOOLCALL_HEADER not in "\n".join(rendered_inbound_messages)`。若对拼装后的 `parts` 判定，
我们自己注入的强调块（提到了标题）会让判定以为说明已存在而跳过工具清单。

## 7. 纠偏 / 重发提示词

| 常量 | 触发 | 内容 |
| --- | --- | --- |
| `RETRY_HEADER` | 首轮回复未调用工具，且 `TOOL_NUDGE_UNTIL_FIRST_CALL` 开启 | 复述链路，要求恰好一个 `tool_call` 块 |
| `JSON_FORMAT_HEADER` | `tool_call_predicate` 失败（围栏未闭合 / JSON 非法 / 引号不配对等） | `malformed.RETRY_PROMPT` |
| `REPEAT_HEADER` | 同一命令 ≥2 次且每次空输出 | 点名命令与次数 |

### 7.1 任务已开始用工具后不再纠偏

`has_prior_tool_use()` 为真时 `tool_nudge_predicate()` 返回 `None`。实测：用过工具后纯文本
收尾时若立刻纠偏，等于把结论推成新命令，任务永远结束不了。

### 7.2 重发不带构造信息

JSON 格式重发只发 `RETRY_PROMPT`——不加 system、不加工具清单、不重放历史。
重试受 `MALFORMED_RETRIES` 限制，耗尽后**如实抛错**，不静默返回坏正文。

## 8. 反模式清单

1. 增量切分不丢系统噪声 → harness 元提示当新指令重放。
2. 只发最后一条 user → 工具结果丢失，反复重发同一调用。
3. 每轮发完整 system → 网页会话被上万字灌满。
4. 写「你无法访问 shell」或「工具已挂载」→ 拒答。
5. 同时宣传纯文本调用行与禁止纯文本行 → 模型二选一失败。
6. 列举被禁形态 → 负向示范。
7. 整数参数用 `"..."` 占位 → 类型错误。
8. `<command>` 占位符 → 被原样照抄。
9. 对工具正文 `strip()` → `edit` 匹配失败。
10. `(no output)` 原样透传 → 死循环。
11. 逐条加 `[tool result: <name>]` 前缀 → 冗余噪声。
12. 去重对拼装结果而非入站消息 → 跳过工具清单。
13. 重发带 system / 工具清单 / 历史 → 模型把重发当新任务。
14. 重试耗尽后静默返回坏正文 → 故障被吞。
15. 工具描述截断切在词中间 → 丢掉最关键约束。
16. 三块顺序被打乱（如纠偏放到最前） → 模型把纠偏当本轮任务。

## 9. 实现位置

| 职责 | 位置 |
| --- | --- |
| 单轮组装、增量 / 播种分支、空输出处理 | `chatgpt_web/prompting.py::build_prompt` |
| 消息渲染 | `chatgpt_web/prompting.py::_render_message` |
| 区段标题常量（单点定义） | `chatgpt_web/toolcalls.py`：全部标题常量，包括 `SECTION_USER`、`SECTION_TOOL_RESULTS` |
| 契约块、强调块、示例生成 | `chatgpt_web/toolcalls.py::format_tools_instruction`、`format_tool_call_emphasis`、`_tool_example_call` |
| 纠偏提示词 | `toolcalls.py::format_tool_retry_nudge`、`format_tool_json_format_nudge`、`format_repeat_call_hint` |
| 坏回复判定 | `toolcalls.py::tool_call_predicate`、`malformed.py::RETRY_PROMPT` |
| 可调参数 | `config/_core.py`：`SEED_MAX_CHARS`、`SEED_SYSTEM_MAX_CHARS`、`TOOL_RESULT_MAX_CHARS`、`TOOL_NUDGE_UNTIL_FIRST_CALL`、`TOOLS_INSTRUCTION_VERBOSE`、`TOOLS_DESC_MAX_CHARS` |
| 测试 | `tests/test_prompting.py`、`test_seed_prompt.py`、`test_tool_injection.py`、`test_toolcall_malformed.py` |

## 10. 未决问题与实测状态

以下问题均需要真实网页会话数据。单元测试只能证明实现行为，不能替代模型端的效果证据。
当前 E2E 按 `doc/tasks.md` T4.7 的既有用户决定暂停；重新启动前必须获得明确授权，并准备独立、已登录的
测试 profile 与独立端口。不得终止或复用其他项目的浏览器进程及用户数据目录。

| 问题 | 所需证据 | 当前状态 |
| --- | --- | --- |
| 区段标题的模型理解度 | ≥10 轮真机回答中 `[User Request]` / `[Tool Results]` 字面量复述次数 | `BLOCKED`：待采样 |
| 精简后的有效性 | 精简与英文标题落地后的 E2E C1/C2 连续两次结果 | `BLOCKED`：待获授权的 E2E |
| 契约放前 vs 放后 | 同一组 E2E C1/C2、两种位置各 3 次的调用率对照 | `BLOCKED`：开关与单测已完成，真机 A/B 待执行 |
| 空输出说明措辞 | 当前三句版本与候选短版本在相同用例下的重复调用率 | `BLOCKED`：待前后对照 |
| 纠偏的实际有效率 | ≥20 个真实纠偏样本；统计重发后合法调用数及分母 | `BLOCKED`：日志字段已实现，样本待采集 |

**解释边界**：未运行、未采集或样本不足均记为 `BLOCKED`，不能记作通过，也不能推断效果为零。
