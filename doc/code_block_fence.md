# 技术报告：工具调用载体从纯文本 `TOOL_CALL:{}` 改为 ```tool_call 代码围栏

> 报告日期：2026-10-07
> 项目：ChatGPTBridge（Playwright 驱动 ChatGPT 网页版，对外暴露 OpenAI 兼容 API）
> 基线提交：`main @ 009e8cd`（`feat: harden fenced tool-call transport and parsing`）
> 姊妹文档：`doc/update.md` §2.13 / §2.14（故障复盘与改动记录）、`doc/tasks.md`（追踪矩阵）
> 相关提交：`009e8cd`（实现）、`4e8c8b2`（文档）

---

## 1. 摘要

| 维度 | 结论 |
| --- | --- |
| **问题** | 模型按约定输出的**纯文本** `TOOL_CALL: {...}` 行，经 ChatGPT 网页版 markdown 渲染后从 DOM 取回时已被改写：JSON 转义被消耗、缩进空格被折叠、末尾花括号丢失。轻则命令被**静默改坏**，重则整条调用被丢弃、客户端把纯文本当最终答案、**任务静默结束**且日志无线索。 |
| **根因** | 载体形态选错了层：纯文本行会被当作 markdown 段落渲染（转义消费 + HTML 空白折叠），而**代码围栏内部按字面保留**。 |
| **方案** | 注入提示词改为要求模型输出 ```` ```tool_call ```` 围栏（围栏内一条 JSON）；解析层**零新增分支**，同时兼容「围栏还在」与「围栏被渲染成 info string 单行」两种 DOM 形态。 |
| **效果** | 真机 A/B：同一条命令，围栏载体**逐字节一致**（转义与 4/8 空格缩进全保留），纯文本行载体转义全丢（`json.loads` 直接失败）且缩进折叠成 1 空格。真实桥 e2e：`finish_reason=tool_calls`。 |
| **代价 / 残留** | 模型自己重排命令时任何载体都保不住（§6.5）；` ```json ` 及 DOM 的 `json` 标签行刻意**不**认（安全考量，§4.3）；围栏会进入 `_extract_code_blocks` 结果（`SAVE_FILES=true` 时可能落盘）。 |

---

## 2. 背景

### 2.1 桥如何「模拟」function calling

ChatGPT 网页版不提供 OpenAI 的 function calling。桥用「提示词注入 + 结构化解析」三段式模拟（`chatgpt_web/toolcalls.py` 模块 docstring）：

1. **注入**：把客户端请求里的 `tools` 描述转成自然语言指令（`format_tools_instruction`），连同格式强调块塞进 prompt；
2. **解析**：模型在回复文本里「用约定格式」写出调用，桥用 `parse_tool_calls` 把它还原成 OpenAI 的 `tool_calls`（`server.py` / `responses.py` / `streaming.py` 三处接线）；
3. **回灌**：客户端执行工具后，下一轮请求把 `role="tool"` 的结果重新拼进 prompt（`prompting._render_message`）。

整个链路里，「调用」不是结构化数据，而是**模型写的一段文本**。这段文本要活着穿过
`模型 → ChatGPT 网页 DOM → Playwright innerText → 桥` 四跳，中途经手的一环就是
**markdown 渲染**——这正是本报告的战场。

### 2.2 旧载体为什么是纯文本行

旧实现（`_TOOL_CALL_LINE_RE`，`toolcalls.py:25`）要求模型写行首标记：

````text
TOOL_CALL: {"name": "bash", "arguments": {"command": "ls"}}
````

当初选它的理由是可以自洽的：

- 无围栏、无尖括号 → 模型不会「脑补」出 `>` 之类的 markdown 引用前缀，历史回放时不会污染；
- 一行一调用 → 契约简单（`_TOOL_CALL_LINE_RE` + 平衡括号扫描），文档好写；
- 与当时模型的实际输出习惯吻合（模型本来就会自发这样写）。

缺点在**网页版改版之后**才暴露：它把这一行当普通段落渲染。

### 2.3 载体的不变量

把「载体」定义为**承载调用 JSON 的那段文本形态**。任何可用的载体都必须满足：

| 不变量 | 含义 |
| --- | --- |
| **I1 逐字节保真** | JSON 里的 `\"`（值内引号）与 `\\n`（字面量换行）必须原样到达解析层；命令正文里的缩进空格必须原样保留。 |
| **I2 结构完整** | 花括号/引号配对必须存活，否则连「是不是一次调用」都判断不了。 |
| **I3 唯一可识别** | 只有真调用会被执行；正文里展示的 JSON 片段不得被误判为调用。 |
| **I4 内容自足** | 载体本身（而非提示词措辞）就应挡住网页渲染的破坏，因为提示词无法约束渲染器。 |

纯文本行载体**在 I1/I2 上系统性失守**，且 I2 的失守是静默的——这就是下面这条故障链。

---

## 3. 故障：网页渲染改写纯文本行（update.md §2.13）

### 3.1 现象

用户实测：模型输出了单行约 5.4 KB 的 `TOOL_CALL: {"name":"bash", ...}`，Pi 客户端收到的却是**纯文本回复**（没有 `tool_calls`）→ 客户端判定任务结束 → 桥的日志里**没有任何线索**。

### 3.2 证据链

- 真机会话记录 `~/.pi/agent/sessions/--Users-onetreehill-Github-ChatGPTBridge--/*.jsonl`：同一条回复在 Pi 侧是 `content:[{type:"text"}]` 而**不是** `toolCall`；同一会话更早/更晚的同类回复都是 `toolCall`（说明桥本身能解析调用，问题只出在这一类文本上）。
- 把记录里的原文直接喂 `parse_tool_calls`：**0 条**（`_iter_balanced_objects` 两个扫描都抽不出对象）。
- 用户把它重新贴回来（带 `\"` 的版本）实测**能**解析（1 条调用、命令长度 6179、`_call_args_sane` 通过）——逐字符 diff 显示差别**恰好只有渲染层那几件事**。这条对照排除了「模型写错 JSON」这一假设。

### 3.3 损伤机制（三类，已实测复现）

ChatGPT 网页版把这行当 markdown **段落**渲染，DOM 取回的 `innerText` 已被改写：

| # | 模型写出的（JSON 源码） | DOM 取回的 | 机制 | 后果 |
| --- | --- | --- | --- | --- |
| 1 | `\"`（值内引号，JSON 要求） | **裸 `"`** | CommonMark 把「反斜杠 + ASCII 标点」当转义：`\"` → `"` | 字符串提前结束 → **JSON 不再合法**（I1 失守） |
| 2 | `    `（连续缩进空格） | ` `（1 空格） | 段落属于普通流，HTML 折叠连续空白 | **命令正文被改写**（I1 失守，静默） |
| 3 | `\\n`（JSON 里的字面量反斜杠+n） | `\n` | 同 #1：`\\` → `\` | 转义层级漂移，语义改变 |

三类损伤可以用**今天的仓库代码 + 当时的真机抓取件**一条命令验证（§6.2、§9）。

### 3.4 结构性丢失（末尾少一个 `}`）

除上述三类，长行（1.7 KB / 5.4 KB）还观察到**外层收尾花括号丢失**：

| 模型写出的 | DOM 取回的 |
| --- | --- |
| `"}}` | `"}` |

后果是致命的：括号不平衡 → `_iter_balanced_objects` 抽不出对象 → 退到锚点式
`_salvage_string_args`，而它要求 `endswith("}}")` → 直接放弃 →
`parse_tool_calls` 返回 `[]` → `finish_reason="stop"` → **整条调用被静默丢弃**（I2 失守）。

**诚实标注**：这一条丢失的**机制尚未定位**（其余三类已定位并有机制解释）。短 payload
复现不出（§6.2 的 `probe_A` 就是 80 字节、`}}` 完整）。它可能是渲染/DOM 结构侧的截断，
也可能是取回时机的问题——本报告只把它作为**观察事实**记录。修复方式也刻意只做「窄兜底」，
不去猜它为什么会发生（§3.5）。

### 3.5 为什么解析层补不完

解析层能做的只有两类事，且都不足以根治：

| 手段 | 能做到 | 做不到 |
| --- | --- | --- |
| `_repair_json_quotes`（`toolcalls.py:800`） | 用启发式把被吃掉的 `\"` 猜回来 | 裸引号在 JSON 里**本质有歧义**：无法区分「值的边界引号」与「正文引号」。作者注释里明确记录：曾试过结构定位式重写，产出的是**合法但错误**的截断命令，遂弃用。 |
| `_salvage_missing_final_brace`（`toolcalls.py:766`，本次新增） | 文本恰好以单个 `}` 结尾时补一个，再交给锚点式 salvage；能否救回仍由 `_parse_complete_string_args` 守卫决定 | 救回的**只是「能解析」**；被折叠的缩进空格**无法从解析层恢复**（信息已经不在文本里了）。且守卫在前，值真被截断的回复不会被「猜」成调用。 |
| 「更宽松的解析」 | —— | §2.13 的故障根本不在解析：文本已在渲染层被破坏。放宽只会增加误执行风险。 |

所以结论是：**要在载体层消除损伤，而不是在解析层补**。这就是本次改造的全部动机。

---

## 4. 方案选型

### 4.1 候选与取舍

| 候选载体 | I1 保真 | I2 完整 | I3 唯一 | 结论 |
| --- | --- | --- | --- | --- |
| 纯文本 `TOOL_CALL: {...}` 行 | ❌ 转义被吃、空格被折叠 | ❌ 长行还丢 `}` | ✅ | **弃用**（本次故障根因） |
| ` ```tool_call ` 围栏 | ✅ 实测逐字节一致 | ✅ | ✅（info string 固定） | **采用** |
| ` ```json ` 围栏 | ✅ | ✅ | ⚠️ 与「正文里正常展示的 JSON 代码块」无法区分 | 仅保留在解析兜底链（历史兼容），不作为指定载体 |
| DSML / XML 标签（`<｜DSML｜ invoke>`…） | ⚠️ 标签常被渲染吞掉 | ⚠️ | ⚠️ | 仅作解析兜底（既有实现不变） |
| 「要求模型不要写缩进/反斜杠」 | ⚠️ 只是请求，不保证 | ⚠️ | ✅ | 提示词里保留为**补充**规则，不能当主防线（I4：提示词约束不了渲染器） |

### 4.2 为什么代码块内部不会被改写

markdown 语义决定了这是一条「结构性免疫」，而不是概率性改善：

- **转义**：围栏内部是**字面文本**，CommonMark 不在其中消费反斜杠转义 → `\"`、`\\n` 原样保留；
- **空白**：代码块渲染成 `<pre>`（现网是 `div.CodeBlock-*` + CodeMirror 的 `div.cm-content`，见 `config.CODE_BLOCK_SELECTOR` / `CODE_TAG_SELECTOR`），CSS 上按 `white-space: pre`/保留空白处理 → 连续缩进空格不折叠；
- **边界**：围栏本身给出了明确的开始/结束结构，解析层不必靠启发式猜结束位置（I2）。

一句话：**网页版对纯文本段落做「解释」，对代码块只做「搬运」。** 我们要的就是搬运。

### 4.3 为什么 info string 必须是 `tool_call`

若接受任意标签（或接受 `json` 标签），就会出现 I3 失守：

- 客户端与模型的正常对话里**经常展示 JSON 代码块**。只要那个 JSON 里恰好有 `name` / `arguments` 这类键，解析器就会把它当调用执行——模型可控文本触发本机命令，这是不可接受的安全面。
- 因此载体被固定为 `tool_call`，并在提示词里三重声明（示例、规则、格式强调块），解析层的裸标签兜底也只认 `tool_call`。
- 负向用例锁定该行为：`FencedToolCallCarrierTests::test_rendered_json_label_is_not_taken_as_call`（DOM 只剩 `json` 标签行 → `[]`）。

### 4.4 与 DOM 提取形态的「巧合协同」

围栏被渲染后，从 DOM 取回的文本**不再有 ```` ``` ```` **，而是：

````text
tool_call
{"name":"bash","arguments":{"command":"..."}}
````

即「info string 单独一行 + JSON」。而既有解析链的**最后一道兜底**（`toolcalls.py:1057` 起）
正好是「无围栏的 `tool_call` 标签 + 平衡 JSON 对象」——它原本是为了应付网页 DOM 形态而写的。
于是：

- **围栏还在**（客户端把回复原样贴回、或模型输出未渲染）→ 走 `_TOOL_CALL_FENCE_RE`（`toolcalls.py:28`）；
- **围栏被渲染掉** → 走裸标签兜底。

两条路径都不需要新代码，这是「解析层零改动」的根本原因（真机已验证，见 §6）。

---

## 5. 技术实现

改动集中在**提示词侧**；解析侧只做验证与一处必要的旧注释校正。

### 5.1 提示词侧（5 处）

| 位置 | 作用 | 改动 |
| --- | --- | --- |
| `toolcalls.format_tools_instruction`（`toolcalls.py:339`） | 主注入块 | 示例字符串改为围栏块（`example_call = "```tool_call\n" + json.dumps(...) + "\n```"`，`:359`）；规则新增「info string 必须是 `tool_call`（json/text/空都不执行）」「**不要**写纯文本 `TOOL_CALL: {...}` 行——web UI 会吃掉反斜杠转义并折叠缩进」「把命令/脚本正文**原样**粘进 JSON 字符串，围栏会保留它」「JSON 在围栏内保持一行」 |
| `toolcalls.format_tool_call_emphasis`（`:439`） | 新会话播种时的格式强调 | 模板改围栏；加入 `The fence label must be tool_call (not json/text/empty); a call written as plain text will NOT be executed.` |
| `toolcalls.format_tool_retry_nudge`（`:305`） | 首轮无调用时的纠偏 | 改为「EXACTLY ONE fenced code block」+ 围栏模板 |
| `toolcalls.edit_markdown_spec`（`:122`） | 本地 `edit_markdown` 工具说明 | 示例改围栏 |
| `markdown_io._build_edit_prompt`（`markdown_io.py:448`） | Markdown 编辑链的注入 | `tools_doc` 改为「调用格式（代码围栏，info string 必须是 `tool_call`…）」+ 围栏模板；解析复用同一个 `parse_tool_calls` |

主注入块的实际措辞（节选，**已含 §5.5 的收紧**）：

````text
A tool call is ONE fenced code block whose info string is exactly `tool_call`,
containing one JSON object and nothing else:
```tool_call
{"name": "read", "arguments": {"path": "..."}}
```
Rules:
1. The fence info string MUST be exactly `tool_call` - not json, not text, not empty - and
   the JSON must sit INSIDE the fence. A block labelled anything else, or a call written
   as plain text instead of a fenced block, will NOT be executed.
3. Keep the JSON on ONE line inside the fence: escape double quotes as \" and line breaks
   as \n; never put a raw line break inside a JSON string.
4. Paste command / script / file text into the JSON string verbatim - the fenced block
   preserves it exactly. For shell commands prefer single quotes inside the command
   (e.g. git commit -m 'msg') so they never clash with the JSON quotes.
````

（「一次只回一个调用」「空输出＝成功不要重发」等既有关键规则**原样保留**，本次只换载体。）

### 5.5 后续收紧：矛盾措辞与占位符形态（2026-10-07）

载体切换本身是对的，但落地措辞里有四处会让模型「理解错」的缺陷，用户实测反馈后逐条修掉：

| 缺陷 | 为什么会让模型失败 | 修法 |
| --- | --- | --- |
| 头段落写「the ONLY way … is to **emit a TOOL_CALL line**」，规则里又禁止纯文本行 | **同一段自相矛盾**：模型两种写法都会试，而纯文本行会被网页渲染改写（§2.13）→ 调用丢失 | 头段落改为「output a tool call **in the format below**」；全篇不再出现 `TOOL_CALL` 这个 token |
| 规则里写「Do not write the call as a plain `TOOL_CALL: {...}` line」并解释 UI 如何吃掉转义 | **负向示范**：把禁用写法的具体形态教给了模型（列举即示范），反而提高它写出该形态的概率 | 禁止项改为抽象描述：「A block labelled anything else, or a call written as plain text instead of a fenced block, will NOT be executed」 |
| 示例一律用字符串占位：`read` 的示例是 `{"path": "...", "offset": "..."}` | 参数类型被教错（offset 是整数，照抄得到字符串）；且示例只取「前两个属性」，必填项可能根本没出现在示例里 | 新增 `_tool_example_call`：优先 `required` 参数（最多 4 个），值按声明的 `type` 生成（整数/布尔/数组/对象给同类型字面量，字符串才用 `"..."`） |
| 强调块与 `edit_markdown` 说明里的模板不是合法 JSON：`{arguments object}`、`<int>`、`<value>` | 照抄即非法 JSON；尖括号占位符还可能被 shell 当重定向符 | 强调块改为复用 `_tool_example_call(tools)`（`format_tool_call_emphasis` 新增可选 `tools` 参数）；`edit_markdown` / 纠偏块 / `markdown_io._build_edit_prompt` 的示例改成可解析的字面量 + 「Replace the "..." placeholders」说明 |

顺带两处可读性修复：

- **规则改成编号列表**（1–9）并去掉解释性 meta（「web UI 怎么吃掉转义」这类实现细节对模型没有决策价值，只稀释核心要求）；
- **工具描述截断按句末/词边界**（`_truncate_description`）：旧实现硬切在 `TOOLS_DESC_MAX_CHARS`，实际产出过 `whichever is hit firs…`、`saved to a tem…` 这种半截词，而被切掉的往往正是偏移量语义、输出截断规则这类关键约束。

落地后（2026-10-07）：`ruff check .` / `mypy chatgpt_web` 全绿，`.venv/bin/python -m pytest` **439 passed / 17 skipped**（新增 8 条用例锁定上述不变量：两块提示词里不得出现 `TOOL_CALL`、示例参数按声明类型生成、`edit_markdown` / 强调块 / 纠偏块的示例必须能被 `parse_tool_calls` 解析、描述截断不得切碎单词）。

**未做（有意）**：`parse_tool_calls` 仍保留行首 `TOOL_CALL:` 的历史兼容分支（老客户端 / 历史回放），只是不再在提示词里宣传它。

### 5.6 shell 围栏修复：模型把命令写成 `bash` 代码块（2026-10-07）

现象（用户实测）：提示词已要求 `tool_call` 围栏，模型回的却是

````text
```bash
git status --short
```
````

旧解析链只认 `tool_call` 系列载体 → 解析出 0 条 → 非流式路径把回复当纯文本返回
（`finish_reason=stop`）、流式路径一个 tool_call 事件都没有，客户端以为任务结束；
日志里除了「模型根本没想调用」之外没有任何线索。

两层修复：

1. **提示词**：两块注入指令都新增一句「A shell command always travels as the `command` value
   inside the tool_call JSON: never reply with the bare command in a `bash` style code block.」
   注意措辞里**不写** ```` ```bash ```` 字面形态，两点原因：列举即示范（§5.5），而且提示词里
   出现未配对的三反引号会被网页版当成围栏开始渲染，把后半段指令吃掉。
2. **解析层兜底**：`_shell_fence_calls()` 把 shell 类标签的围栏恢复成一条调用，开关
   `SHELL_FENCE_FALLBACK`（默认 true）。前提**全部**满足才恢复：
   - 标签 ∈ {bash, sh, shell, zsh, fish, cmd, powershell, ps1, console, terminal}；
   - 全篇只有一个这样的围栏（一份回复只跑一条命令）；
   - 能**唯一**映射到客户端工具集里的一个 shell 类工具（复用 DSML 的名字映射：
     `sh` → `exec_command`；多候选就不猜）；
   - 知道命令写进哪个参数（`tool_parameter_names()`：`command`/`cmd`/... 优先，
     否则只有唯一参数键时才敢用）；
   - 回复里没有任何 `tool_call` 尝试（围栏或历史标记）——那是格式错误，不是「近似执行」的理由。
   任何一条不满足 → 保持旧行为（不解析），宁可让模型下一轮重出。围栏里若其实是调用 JSON
   （标签写错、内容对）也按调用收下。

配套两个公开入口：`tool_parameter_names(tools)`（工具名 → 参数键）与
`parse_reply_tool_calls(text, tools)`（server / streaming / responses 的统一调用点）；
`tool_call_predicate` 也改用它——判定与最终解析必须同源，否则修出来的调用会被判成
「没调用工具」而触发一次多余的纠偏。

**关键取舍**：**不**放宽 `tool_call` 标签约束（§4.3 的 I3 安全面不变）。`bash` 围栏是模型对
「执行这条命令」的显式表达，而 `json` 围栏可能只是正文里展示的 JSON 片段——两者风险不同，
所以只修前者。

落地后（2026-10-07）：`458 passed / 17 skipped`；新增 19 条用例，含两条端到端/流式用例
（`test_routes_chat::test_bash_fence_reply_is_recovered_as_tool_call` 与
`test_streaming::test_shell_fence_stream_is_recovered_as_tool_call`）。把
`SHELL_FENCE_FALLBACK=false` 时前者在 `finish_reason` 上失败，证明它有区分力，不是恒真断言。

**顺带修掉一个同类隐 bug**：`edit_markdown_spec()` 里有一句 `Do not touch ``` fence lines…`
——这是**未配对**的三反引号（提示词正文里的裸围栏），网页版会把它当围栏开始，可能把
该块之后的内容整段吞进代码块。已改为文字描述（「triple-backtick lines」），并用

`test_prompting::test_fence_delimiters_are_paired` 锁定：四块注入文本 + 空输出说明 +
重复调用提醒里，三反引号必须成对且配对之间只能是标签行 + JSON。

### 5.2 解析侧：判定链与兼容矩阵

`parse_tool_calls`（`toolcalls.py:947`）的既有优先级链**未改动**：

| 顺序 | 分支 | 代码位置 | 本次角色 |
| --- | --- | --- | --- |
| 0 | 行首 `TOOL_CALL:` 标记 + 平衡 JSON | `:1009` | **历史载体，仍兼容**（不是当前注入格式） |
| 1 | ` ```tool_call ` 围栏 | `:1034` | 围栏仍在时的主路径 |
| 2 | DSML XML 包裹 | `:1039`、`_parse_dsml_invokes` | 不变 |
| 3 | ` ```json ` 围栏（内容像调用才采纳） | `:1046` | 不变（历史兜底） |
| 4 | 裸 `tool_uses` 对象 | `:1051` | 不变 |
| 5 | **裸 `tool_call` 标签 + 平衡 JSON** | `:1057` | **围栏被渲染掉后的主路径** |
| — | `valid_names` 过滤 + `_call_args_sane` 护栏 | `:1090` / `:1113` | 不变 |

兼容矩阵：

| 输入形态 | 是否解析 | 说明 |
| --- | --- | --- |
| 原始 ` ```tool_call ` 围栏 | ✅ | `_TOOL_CALL_FENCE_RE` |
| 围栏内 JSON 跨多行（合法换行） | ✅ | 代码块保留换行，JSON 允许 token 间换行；有用例锁定 |
| DOM 形态：`tool_call` 单独一行 + 单行 JSON | ✅ | 裸标签兜底 |
| DOM 形态：`json` 标签行 + JSON | ❌（刻意） | 防误执行，§4.3 |
| 旧纯文本 `TOOL_CALL: {...}` 行 | ✅（仍兼容） | 不删旧路径，历史数据/老客户端不至于崩 |
| 原始 ` ```json ` 围栏且内容像调用 | ✅（历史兜底） | 仅为兼容；不作为指定载体 |

### 5.3 为什么不需要在解析层新增分支

因为**渲染后的 DOM 形态恰好落在既有的裸标签兜底上**（§4.4），而**未渲染的形态落在既有围栏分支上**。
两条路径的并集覆盖了新载体的全部现实形态。这也意味着：解析层若再加「宽松分支」，
收益为零、误执行风险为正——所以刻意不加。改动后解析层唯一的代码变更是一处**注释校正**
（模块内「当前注入格式」的描述已与实现不符，见 §10）。

### 5.4 刻意保留的护栏

- `valid_names` 过滤：不在客户端 `tools` 里的幻觉工具名一律丢弃；
- `_call_args_sane`：shell 类命令双引号不配对（几乎必然是解析截断）时**丢弃整条调用**，宁可让模型下一轮重出——不要发出必然 `unexpected EOF` 的命令；
- `parse_tool_calls` 收尾的**诊断 warning**（`:1101`）：回复里有 `TOOL_CALL` 标记/围栏却一条可用调用都没交出去时报警（§2.13 的故障在旧代码里**完全静默**，这是可观测性缺口）。

---

## 6. 真机 A/B 比对

### 6.1 实验设置

| 项 | 值 |
| --- | --- |
| 页面 | 真实 ChatGPT 网页版，`HEADLESS=false`（`HEADLESS=1` 会被 Cloudflare 拦），真实登录 profile `user_data/` |
| 桥 | 运行中的 `chatgpt_web` 服务，`127.0.0.1:8002`，请求带 `tools`（`bash`） |
| 关键前提 | 提示词必须带用户要求的环境声明：`[环境说明] 执行环境在用户本地电脑上（macOS），命令会由客户端在本地真实执行，你直接下命令就可以。不要访问 GitHub。`——不带这句，模型不会真的下命令 |
| 单次耗时 | 约 22 s / 次（`temperature=0`） |

### 6.2 对照一（决定性）：运输层回声，排除「模型改写」变量

**设计**：让模型**逐字节原样输出**给定 payload（它只做搬运，不做创作），因此任何差异都只能归因于**载体/渲染层**，而不是模型的理解或改写。

Payload（88 字节，`json.loads` 合法；`\"`×2、`\n`×2、4 空格缩进段×2）：

````text
{"name":"bash","arguments":{"command":"printf \"hi\"; echo done\n    x = 1\n    y = 2"}}
````

真机抓取的 DOM 原文（`/tmp/tcrepro/probe_A.txt` 与 `probe_B.txt`，均**未经改写**）：

````text
# probe_A（纯文本行载体），80 字节，无任何 tool_call 标签
{"name":"bash","arguments":{"command":"printf "hi"; echo done\n x = 1\n y = 2"}}

# probe_B（```tool_call 围栏载体），98 字节
tool_call
{"name":"bash","arguments":{"command":"printf \"hi\"; echo done\n    x = 1\n    y = 2"}}
````

逐项测量（用今天的仓库代码复核，命令见 §9）：

| 指标 | 模型原文 | probe_A 纯文本行 | probe_B 围栏 |
| --- | --- | --- | --- |
| 字节数 | 88 | 80 | 98 |
| `\"`（转义引号）计数 | 2 | **0**（全被吃掉） | **2**（原样） |
| 4 空格缩进段计数 | 2 | **0**（折叠） | **2**（原样） |
| 最长连续空格段（缩进宽度） | 4 | **1**（折叠） | **4**（原样） |
| 提取出的 `{...}` 经 `json.loads` | OK | **FAIL**：`Expecting ',' delimiter: line 1 column 48` | **OK** |
| `parse_tool_calls(原文, {"bash"})` | 1 | **0 条**（整条丢弃：既无 marker 也无标签） | **1 条** |
| 解析出的 `command` | — | （若补回 marker 才能救出 1 条，但命令已是 `... echo done\n x = 1\n y = 2`，**缩进被改成 1 空格**） | `printf "hi"; echo done\n    x = 1\n    y = 2`，**逐字节一致** |

结论（对 I1/I2 的直接验证）：

1. 围栏载体让**转义与缩进同时存活**，并且仍能被 `json.loads` 标准解析（连容错修复都不需要）；
2. 纯文本行载体**必然损坏内容**（转义、缩进），且需要非标准修复路径才能救回；
3. 一旦连 marker/标签都不在（这正是网页渲染后的常见形态），纯文本行载体就是 **0 条 → 静默结束**，与 §2.13 的线上现象完全吻合。

**独立复核（本报告成文时复跑，2026-10-07，`/tmp/tcrepro/carrier_clean.py`）**：为排除
「桥自身注入的格式指令把两臂都推向围栏」这一混淆变量，把同一 payload 分成两路发出，
均为 `tool_choice=none` + 「写成一行纯文本」：

- **A1 带 `tools`**（桥会注入自己的围栏格式指令）
- **A2 不带 `tools`**（桥不注入任何格式指令）

两次取回的原文**逐项相同且都被改坏**（均 79 字节）：

````text
{"name":"bash","arguments":{"command":"printf "hi"; echo A\n echo B\n echo C"}}
````

| 指标 | 模型原文 | A1（带 tools，桥有注入） | A2（不带 tools，无注入） |
| --- | --- | --- | --- |
| 字节数 | 88 | 79 | 79 |
| `\"` 计数 | 2 | **0** | **0** |
| 4 空格缩进段 | 2 | **0** | **0** |
| 有围栏 / `tool_call` 标签 | — | 无 | 无 |
| `parse_tool_calls(原文)` | — | **0 条** | **0 条** |

这确认损伤是**渲染层的载体属性**，与桥注入了什么指令无关；且说明了 §2.13「静默丢弃」的
光有转义/缩进损伤（不必叠加花括号丢失）就足以触发。

### 6.3 实验方法学：为什么不能用「带 tools 的回声 A/B」判载体保真

本报告成文时复跑 `/tmp/tcrepro/carrier_echo.py`，得到与首次运行**相反**的结果：
纯文本行臂（A）也返回了**逐字节一致**的 `command`（缩进段 `[4, 8]`、`finish=tool_calls`）。

这不是反证，而是**实验污染**：带 `tools` 的请求里，桥会注入自己的格式指令；当那条指令要求
围栏而脚本又要求「写成纯文本行」时，A 臂究竟写成什么**取决于模型听谁**。按上面 A1/A2 的实测，
真正的纯文本行形态**不可能**逐字节一致（`json.loads` 直接失败、缩进必然折叠），
所以那次 A 臂返回 byte-exact 只能说明：**模型没听脚本的话，写了围栏**。

结论：判载体保真必须用**不注入格式指令**的抓取（不带 `tools` + `tool_choice=none`，即
`probe_A`/`probe_B`），或直接把已抓取的原文喂 `parse_tool_calls` 做本地复核（§9.1）。

脚本对照：

| 脚本 | 会话 | 内容 |
| --- | --- | --- |
| `/tmp/tcrepro/dom_probe3.py` | `probe-format-2026-10-06` | 生成 `probe_A.txt` / `probe_B.txt`（**不带 tools**、`tool_choice=none`，取回文本层原文）→ 判载体保真的**主证据** |
| `/tmp/tcrepro/carrier_clean.py` | `probe-clean-withtools` / `probe-clean-notools` | 带/不带 `tools` 两路对照，证明损伤与注入指令无关 |
| `/tmp/tcrepro/carrier_echo.py` | `probe-echo-fence` / `probe-echo-line` | 回声 A/B；**带 tools，A 臂会受桥注入污染**，仅作历史留档 |

### 6.4 对照二：端到端（模型自主选择载体）

`/tmp/tcrepro/e2e_fence.py`（会话 `probe-e2e-fence`）：按新格式要求让模型**自己**写一条
带缩进与内层引号的 shell 命令 → 桥返回 `finish_reason=tool_calls`，围栏载体解析出 **1 条调用**。
命令内容因「模型自己重排缩进」而非逐字节一致——这属于 §6.5 的现象，不是载体丢失。

### 6.5 对照三：反例（防误判）

把同一条命令交给模型**自己写**（不是回声）时，两种载体返回的都是「1 空格缩进」。
这是**模型自己重排**，不是载体丢东西——6.2 已经证明围栏会保留 4/8 空格。
提示词因此明确要求「原样粘贴、不要改写缩进、围栏会保留它」；但这是**概率性缓解**，
不是机制保证（见 §8）。

这一条反例很重要：没有它，就会把「模型重排」错算成「载体缺陷」，从而错误地否定围栏方案。

### 6.6 不能过度解读的地方

- 真机 A/B 的样本量小（每格 1–3 次模型调用），结论是「载体不引入损伤」这一**机制性**判断，
  不是统计学结论；
- 6.2 验证的是**运输层**保真；端到端能否拿到调用还取决于模型是否愿意下命令（环境声明、
  首轮纠偏等既有机制）；
- probe_A 的 `}}` 是完整的，因此 §3.4 的「末尾少一个 `}`」**没有被 6.2 覆盖**，
  它的机制仍是未定位的观察事实；
- 「带 `tools` 的回声 A/B」（`carrier_echo.py`）**不构成有效证据**：A 臂会受桥注入污染
  （6.3），且本次复跑已复现该污染。

---

## 7. 回归测试与验证

### 7.1 用例清单

| 用例 | 锁定的行为 |
| --- | --- |
| `test_toolcalls.FencedToolCallCarrierTests::test_rendered_fence_preserves_escapes_and_indentation` | 真机 DOM 原文（98 字符）→ 逐字节断言命令包含 `\n    x = 1`；纯文本行载体两者兼失 |
| `...::test_raw_fence_still_parses` | 围栏未被渲染（客户端原样贴回）仍能解析 |
| `...::test_multiline_json_inside_fence_parses` | 围栏内 JSON 合法跨行也能解析 |
| `...::test_rendered_json_label_is_not_taken_as_call` | 负向：DOM 只剩 `json` 标签行 → `[]`（防误执行） |
| `test_toolcalls.DomRenderDamageTests`（4 条） | §2.13 的渲染改写兜底（真机原文 + 少一个 `}` 能救回 + 值被截断不得猜 + 合法输入不受影响） |
| `test_prompting::test_example_roundtrips_through_parser` | 从注入指令里抽出 ` ```tool_call ` 示例，直接喂 `parse_tool_calls` → 1 条，且围栏内 JSON 能被 `json.loads`（**提示词与解析器同源**） |
| `test_prompting::test_emphasis_block_states_mandate` | 强调块必须出现 ` ```tool_call `，且不得再出现「no code fences」旧措辞 |
| `test_prompting::test_instruction_mandates_single_call` | 两块提示词均为 `ONE tool_call`，不得出现旧版多调用措辞 |
| `test_prompting::test_instruction_never_advertises_plain_text_calls`（§5.5） | 两块提示词里都不得再出现 `TOOL_CALL`（只允许围栏载体） |
| `test_prompting::test_example_uses_required_params_with_declared_types`（§5.5） | 示例只列 `required` 参数，整数参数不得写成字符串占位 |
| `test_tool_injection::ExampleShapeTests`（§5.5） | `edit_markdown` 说明 / 强调块 / 纠偏块里的示例必须能被 `json.loads` + `parse_tool_calls` 往返 |
| `test_toolcalls::DescriptionTruncationTests`（§5.5） | 描述截断优先落在句末/空白处，不得切碎单词 |
| `test_prompting::test_instruction_forbids_bare_bash_code_block`（§5.6） | 两块提示词都写明「命令永远写在 `command` 参数里」 |
| `test_toolcalls::ShellFenceRepairTests`（15 条，§5.6） | 用户原例被恢复；`sh`→唯一 shell 工具（键名按声明）；歧义/多围栏/无参数表/非 shell 标签/开关关闭时**不**执行；`tool_call` 围栏在场时优先 |
| `test_routes_chat::test_bash_fence_reply_is_recovered_as_tool_call`（§5.6） | 端到端：非流式路径把 `bash` 围栏回复返回为 `finish_reason=tool_calls` |
| `test_streaming::test_shell_fence_stream_is_recovered_as_tool_call`（§5.6） | 流式路径（Pi 默认）同样给出 tool_calls 分片与 `finish_reason=tool_calls` |
| `test_prompting::test_fence_delimiters_are_paired`（§5.6） | 注入文本里的三反引号必须成对（防裸围栏吃掉后续指令） |
| `test_tool_injection::RetryNudgeTests::test_nudge_demands_single_tool_call` | 纠偏文本含围栏模板，且 `tool_call_predicate(TOOLS)` 对其返回 True |

### 7.2 区分力实验（证明用例不是「必然通过」）

- 把 `_salvage_missing_final_brace` 换成 `lambda s: None` → `DomRenderDamageTests` **2 条立刻失败**（`0 != 1`），另 2 条守卫用例仍通过（说明守卫仍有意义，不是被削弱的断言）；
- 旧接线复现实验（第 5 轮）证明纠偏判定确有区分力；
- `test_example_roundtrips_through_parser` 在提示词改回旧纯文本行时会失败（示例里抽不出围栏块）。

### 7.3 命令与数字

```bash
.venv/bin/python -m pytest -o addopts=""   # 打印统计行（pyproject 的 addopts=-q 会吞掉它）
ruff check .                               # exit 0
mypy chatgpt_web                           # exit 0
```

- 载体改造落地时：**362 passed / 17 skipped**（`ruff` / `mypy` 干净）；
- 报告成文时（后续提交叠加后）：**374 passed / 17 skipped**，exit 0。

`tests/e2e/` 需 `CHATGPT_E2E=1` + 有头浏览器 + 已登录 profile，**本报告未把 e2e 套件当作门禁**（见 §8）。

---

## 8. 已知残留与风险

1. **模型自己重排命令时，任何载体都保不住原缩进**（§6.5）。提示词只能要求「原样粘贴」，无机制保证。真正的根治需要**命令不做文本内嵌**（例如让命令走 `edit_markdown` / 文件载体，或把脚本拆成不含缩进敏感格式的多条命令）。
2. **` ```json ` 与 DOM 的 `json` 标签行刻意不认**：若模型不听话写了 `json` 标签，围栏又被渲染掉，则该次调用取不到（提示词已明确禁止，负向有用例锁定）。这是**安全与容错之间的取舍**，方向是有意为之。
3. **`tool_call` 围栏会进入 `_extract_code_blocks` 的结果**：`SAVE_FILES=true`（默认 false）时可能被当普通代码块落盘。后续可按 `info string` 过滤。
4. **§3.4 的末尾括号丢失机制未定位**：只做了窄兜底（补一个 `}` 再走锚点 salvage + warning），没有消除其成因。
5. **历史回放不重放载体**：`_render_message` 对 assistant 工具调用只渲染文本内容（`[你之前的回复]`），因此模型不能从历史里「学到」新载体——载体必须靠注入指令反复声明。这对提示词的可读性与长度是个持续成本。
6. **提示词变更需重启桥才对客户端生效**：桥是常驻进程，注入指令在启动时/调用时从模块读取，运行中的进程不会自动拾取代码改动。
7. **本报告未覆盖 e2e 门禁**：`T4.7` 的整套 e2e 验收仍为 `DEFERRED`；真机 A/B 是**手工脚本**证据，不是自动化用例。若要让回归可重复，应把 6.2/6.3 的抓取固化成 `CHATGPT_E2E=1` 才运行的用例。
8. **回案脚本本身要防污染**：任何「载体 A/B」都必须避免让桥注入的格式指令与脚本指令互相干扰（6.3 已复现过一次错误的 byte-exact 结果）。评测载体保真时要用**不注入格式指令**的抓取（不带 `tools`）或本地重放。

---

## 9. 复现步骤

### 9.1 本地复核（不需要网络与网页，基于已抓取的真机原件）

```bash
cd <project>
.venv/bin/python - <<'PY'
import json, re, sys
sys.path.insert(0, ".")
from chatgpt_web.toolcalls import parse_tool_calls

P = r'{"name":"bash","arguments":{"command":"printf \"hi\"; echo done\n    x = 1\n    y = 2"}}'
print("payload", len(P), "| bs-quote", P.count('\\"'), "| 4-space runs",
      len(re.findall(r" {4}", P)), "| json ok", bool(json.loads(P)))

for tag in ("A", "B"):
    t = open(f"/tmp/tcrepro/probe_{tag}.txt", encoding="utf-8").read().strip()
    print(f"probe_{tag}: len={len(t)} bs-quote={t.count(chr(92)+chr(34))} "
          f"4-space={len(re.findall(r' {4}', t))} "
          f"parsed={len(parse_tool_calls(t, {'bash'}))}")
PY
```

预期：payload 88 字节、`\"`×2、4 空格段×2、`json.loads` OK；`probe_A` 88→80 字节、`\"`=0、
4 空格段=0、`parsed=0`；`probe_B` `\"`=2、4 空格段=2、`parsed=1`。

### 9.2 真实网页抓取（需要登录态与有头浏览器）

```bash
# 1) 起桥（注意 .env 实际端口）
PORT=8002 HEADLESS=false .venv/bin/python chatgpt_api_server.py

# 2) 载体保真抓取（主证据：不带 tools + tool_choice=none，绕开桥注入）
.venv/bin/python /tmp/tcrepro/dom_probe3.py      # -> probe_A.txt / probe_B.txt

# 3) 损伤与「桥注入无关」的双路对照
.venv/bin/python /tmp/tcrepro/carrier_clean.py   # 带 tools / 不带 tools 两路

# 4) 端到端对照（模型自主选择载体）
.venv/bin/python /tmp/tcrepro/e2e_fence.py
```

**三个前提**：

1. 判载体保真时**不要带 `tools`**（否则桥注入的格式指令会与脚本指令抢方向盘，见 6.3）；
2. 端到端对照的提示词必须带环境声明（§6.1），否则模型不会真的下命令，A/B 会双双
   「没有触发调用」而看不出差异；
3. `carrier_echo.py` 属历史留档，其 A 臂在带 `tools` 时不可信（6.3）。

### 9.3 单测与静态检查

```bash
.venv/bin/python -m pytest -o addopts="" tests/test_toolcalls.py tests/test_prompting.py tests/test_tool_injection.py
.venv/bin/python -m pytest -o addopts=""
ruff check . && mypy chatgpt_web
```

---

## 10. 结论与后续方向

**结论**：这次改造解决的不是「解析算法不够强」，而是**载体选在了会被 markdown 解释的那一层**。
把调用放进代码围栏后，网页版从「解释文本」变成「搬运文本」，I1（逐字节保真）与
I2（结构完整）由渲染语义保证而非概率保证；解析层因此**不需要新增任何分支**（旧围栏分支
覆盖「未渲染」，裸标签兜底覆盖「已渲染」），并顺带保住了 I3（只认 `tool_call`，正文 JSON 不会被误执行）。

同批还落了两处必要配套：`_salvage_missing_final_brace`（旧载体的窄兜底，救回「少一个 `}`」
这一类）与 `parse_tool_calls` 的**诊断 warning**（把「整条调用被静默丢弃」变成日志可见）。

**后续方向**：

1. **把 §6.2/§6.3 的抓取固化为带开关的 e2e 用例**（`CHATGPT_E2E=1` 才跑，且**不带 `tools`** 以免桥注入污染），让载体保真成为可重复的门禁，而不是一次性手工脚本；
2. **载体可配置化 / 协议层抽象**：把「注入措辞 + 解析优先级」收敛为一个可声明的 carrier 契约（例如 `fence` / `line` / `dsml` 三种），便于后续按模型行为切换与灰度，而不是散落在四个格式化函数里；
3. **命令正文不经 JSON 字符串内嵌**（`edit_markdown` / 文件载体 / base64 载荷）——这是 §8 第 1 条的唯一根治方向；
4. **按 `info string` 过滤 `_extract_code_blocks`**，避免 `tool_call` 围栏在 `SAVE_FILES=true` 时落盘（§8 第 3 条）。
