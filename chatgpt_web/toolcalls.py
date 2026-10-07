"""工具调用（function calling）桥接层——**兼容 facade**。

ChatGPT 网页版并不原生支持 OpenAI 的 function calling，因此这里采用
“提示词注入 + 结构化解析”的方式模拟：
  1. 把客户端传来的 tools 描述注入到 prompt，要求模型用 ```tool_call 代码块回话；
  2. 解析模型输出里的代码块，还原为 OpenAI 的 tool_calls；
  3. 下一轮请求里 role=tool 的执行结果再拼回 prompt 喂给网页版。

实现已于 PI-901 拆分到 :mod:`chatgpt_web.tools` 子包（parser / validator /
policy / executor / ledger / serializer）。本模块只保留：

* 面向 prompt 的**格式化**函数（注入说明、强调块、纠偏/重复提醒）；
* 对 :mod:`chatgpt_web.tools` 的**再导出**，保证历史 import 路径不变。
"""

import json
from typing import Any, Callable, Dict, List, Optional

from . import config
from .errors import (  # noqa: F401 - 兼容再导出：历史调用方从 toolcalls 取这些异常
    ToolCallExecutionError,
    ToolCallParseError,
    ToolCallPolicyError,
    ToolCallSerializationError,
    ToolCallValidationError,
)

# ---- 迁移到 tools 子包的实现（再导出，保持兼容） ----
from .tools.executor import (  # noqa: F401
    execute_edit_markdown,
    execute_tool_call_requests,
    run_local_edit_markdown,
    run_tool_call_pipeline,
    tool_call_request_mappings,
)
from .tools.ledger import (  # noqa: F401
    TOOL_EXECUTION_LEDGER,
    ToolExecutionLedger,
    ToolExecutionRecord,
)
from .tools.parser import (  # noqa: F401
    BUILTIN_TOOLS,
    EDIT_MARKDOWN_TOOL,
    EDIT_MARKDOWN_TOOL_NAME,
    ToolCallRequest,
    _call_args_sane,
    _dedent_command,
    _dsml_attr,
    _dsml_blocks,
    _dsml_param_value,
    _escape_control_chars_in_strings,
    _iter_balanced_objects,
    _normalize_tool_entry,
    _parse_dsml_invokes,
    _repair_json_quotes,
    _resolve_dsml_name,
    _salvage_missing_final_brace,
    _salvage_string_args,
    _shell_command_key,
    _shell_fence_calls,
    _shell_quotes_balanced,
    _strip_redundant_value_quotes,
    _tool_names,
    parse_reply_tool_calls,
    parse_tool_call_requests,
    parse_tool_calls,
    to_tool_call_models,
    tool_parameter_names,
)
from .tools.policy import (  # noqa: F401
    ToolPolicy,
    check_tool_call_policy,
    resolve_edit_path,
)
from .tools.serializer import serialize_tool_call_results  # noqa: F401
from .tools.validator import (  # noqa: F401
    deduplicate_tool_call_requests,
    normalize_tool_call_requests,
    validate_tool_call_requests,
)

# ==================== 注入块标题常量（生成与判定必须同源）====================
# 这些标题是「去重判定」（prompting.build_prompt：入站 system 已带同类说明时
# 不再重复追加）与「泄漏检测」（tests/e2e/test_parity.assert_no_injection_leak）
# 的唯一依据。此前去重用的中文标签与实际生成块首行永不相等，导致去重恒不生效
# 而检测形同虚设（见 doc/tasks.md T2.3）。只允许从这里取标题。
TOOLCALL_HEADER = "[Tool Calling Instructions]"
EMPHASIS_HEADER = "[Output Format Emphasis]"
EDIT_MD_HEADER = "[edit_markdown notes]"
# 首轮未调用工具时追加的纠偏指令块（见 T1.1）
RETRY_HEADER = "[Tool Call Correction]"
# 同一命令被重复调用且每次都是空输出时的点名提醒（防死循环，见 prompting）
REPEAT_HEADER = "[Repeated Empty Tool Call]"


def builtin_tool_names() -> set:
    return {t["function"]["name"] for t in BUILTIN_TOOLS}


def edit_markdown_spec() -> str:
    """Usage notes for edit_markdown injected into the prompt (anchors / fences caveats)."""
    return "\n".join([
        EDIT_MD_HEADER,
        "When editing a Markdown file, prefer edit_markdown over rewriting the whole file and",
        "doing plain-text matching:",
        "```tool_call",
        '{"name": "edit_markdown", "arguments": {"path": "README.md", "start": 12, '
        '"end": 14, "new_text": "..."}}',
        "```",
        "The line numbers and \"...\" above are placeholders: replace them with the real path,",
        "start/end and replacement text.",
        "start/end are 1-based inclusive line numbers; content outside the range (including blank lines, indentation, trailing whitespace) is preserved verbatim.",
        "Do not touch the fence lines themselves (triple-backtick lines); content inside a fence",
        "does not participate in structural positioning.",
        "By default only a diff is returned (dry-run); pass write=true to persist to disk.",
    ])


def format_repeat_call_hint(repeats: List[tuple]) -> str:
    """同一命令已重复调用（且每次都是空输出）时的点名提醒。

    背景（用户实测死循环）：客户端把空 stdout 渲染成 ``(no output)``，模型以为
    命令没生效，把同一条命令反复重发——每次都是同样的空输出。逐条说明（见
    ``prompting.EMPTY_TOOL_RESULT_NOTE``）已经给出正确理解，这里再针对**重复**行为
    直接点名：报出命令与次数，并明说再发一遍永远不会得到输出。

    :param repeats: ``[(命令签名, 出现次数)]``，来自 ``prompting._repeated_empty_calls``。
    """
    lines = [
        f"{REPEAT_HEADER} You have already issued the SAME tool call more than once, and it",
        "returned EMPTY output every time:",
    ]
    for signature, count in repeats:
        lines.append(f"- called {count}x, always empty: {signature}")
    lines += [
        "An empty result means the command SUCCEEDED and printed nothing. Running it again will",
        "NEVER produce output — repeating it is an infinite loop and the task will fail.",
        "Do NOT repeat any call listed above. Either use a DIFFERENT command that prints the state",
        "you need (another flag, a narrower path, or an explicit echo), or stop and summarise what",
        "you already know as the final answer.",
    ]
    return "\n".join(lines)


def format_tool_retry_nudge() -> str:
    """首轮回复没有调用工具时，追加到同一网页会话的纠偏指令（T1.1）。

    实测：即便注入了工具说明与格式强调，模型在**播种首轮**仍可能无视工具、
    直接凭自身知识作答（E2E C1/C2）。此时只把这段短指令作为会话里的后续
    消息再发一次（不重放历史、不重播种），让模型在已看到自己上一轮回复的
    上下文里被迫给出结构化调用。整段只重试一次，避免与模型拉锯。
    """
    return "\n".join([
        f"{RETRY_HEADER} Your previous reply did not call any tool.",
        "You are not expected to have tools of your own: this conversation is read by a local",
        "client on the user's machine, and it executes whatever tool call you write, then sends the",
        "real output back to you. Refusing because you \"have no tools\", or answering from your own",
        "knowledge instead of calling a tool, means the task FAILED.",
        "Reply now with EXACTLY ONE fenced code block in this form (no other text):",
        "```tool_call",
        '{"name": "...", "arguments": {"param": "..."}}',
        "```",
        "Replace the \"...\" placeholders with the real tool name and its arguments - do NOT copy",
        "the example literally.",
    ])


def tool_call_predicate(tools: Optional[List[Dict[str, Any]]]) -> Callable[[str], bool]:
    """构造「回复里是否包含至少一个工具调用」的判定函数。

    供 ``chat_io.send_chat`` 的纠偏重试使用：判定失败就追发一次纠偏指令。
    与最终解析共用同一个 ``parse_tool_calls``，不会出现「重试判定说不合格、
    外面却解析出了 tool_calls」的不一致。
    """
    def _has_call(text: str) -> bool:
        # 与最终解析共用同一条路径（含 shell 围栏修复）：否则修复出的调用会被判为
        # “没调用工具”，桥会追发一次多余的纠偏指令。
        return bool(parse_reply_tool_calls(text or "", tools))

    return _has_call


def _example_arg_value(schema: Any) -> Any:
    """示例里给某个参数用的占位值：按声明的类型给一个 JSON 合法的最小值。

    历史 bug：示例一律用字符串占位（``"offset": "..."``），而真实参数是整数——
    模型照抄即得到类型错误的参数。整数/布尔参数直接给同类型字面量，字符串参数
    才用 ``"..."``（尖括号占位符会被原样照抄，见 :func:`_tool_example_call`）。
    """
    kind = schema.get("type") if isinstance(schema, dict) else None
    if kind in ("integer", "number"):
        return 0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return "..."


def _tool_example_call(tools: List[Dict[str, Any]]) -> str:
    """按第一个工具的真实名字 / 参数键生成一个 ```` ```tool_call ```` 示例块。

    示例要带真实工具名和参数键，否则模型不认；但值必须是「一眼就知道要替换」的
    占位符——用 ``<command>`` 这种形式模型会原样照抄，把占位符当命令发出来
    （实测：shell 收到字面量 ``<`` 直接语法报错）。优先用 ``required`` 列出的参数
    （必填项最容易漏），没有 required 才退回 properties 的前几个键。
    """
    first = tools[0] if tools else {}
    fn0 = first.get("function", first) if isinstance(first, dict) else {}
    example_name = fn0.get("name") or "tool_name"
    raw_params = fn0.get("parameters") if isinstance(fn0, dict) else None
    params: Dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    raw_props = params.get("properties")
    props: Dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}
    raw_required = params.get("required")
    required: List[Any] = raw_required if isinstance(raw_required, list) else []
    keys = [k for k in required if isinstance(k, str)][:4] or list(props.keys())[:4]
    example_args = {key: _example_arg_value(props.get(key)) for key in keys}
    return "```tool_call\n" + json.dumps(
        {"name": example_name, "arguments": example_args}, ensure_ascii=False
    ) + "\n```"


def _truncate_description(desc: str, limit: int) -> str:
    """按句末/词边界截断工具描述，避免出现 ``whichever is hit firs…`` 这类半截词。

    半截词既可疑又丢信息：被切断的往往正是关键约束（read 的 offset 语义、bash 的
    输出截断规则）。优先在预算内的最后一个句末标点处收尾，退而求其次取最后一个
    空格，都没有才硬截。
    """
    if not limit or len(desc) <= limit:
        return desc
    window = desc[:limit]
    cut = max(window.rfind(". "), window.rfind("。"), window.rfind("; "))
    if cut >= limit // 2:
        return window[: cut + 1] + " …"
    space = window.rfind(" ")
    if space >= limit // 2:
        window = window[:space]
    return window.rstrip() + " …"


def format_tools_instruction(tools: List[Dict[str, Any]]) -> str:
    """把 OpenAI tools 描述转换成注入网页版的自然语言指令。

    实测（联网真机验证）：模型很容易**无视工具、直接凭知识作答**——例如问它
    “查看当前目录”时，它会直接编一份 ls 输出，parse 结果为空。让模型真正调用
    工具的关键有四点：
      1. 明确切断退路：“你没有别的 shell/文件系统访问，唯一的方式是输出
         ```` ```tool_call ```` 围栏块，直接作答＝任务失败”；
      2. 给一个**具体到参数**的调用示例（只给格式模板不够）；
      3. **载体只有一种**。旧措辞在同一段里既说“唯一方式是输出 TOOL_CALL 行”，
         又在规则里禁止纯文本行（自相矛盾），实测模型会在这两种写法之间二选一
         失败。现在全篇不再出现纯文本行的写法，禁止项也只做抽象描述——不给出
         具体形态，避免“负向示范”把禁用写法教给模型（列举即示范）；
      4. 指令要短、聚焦，长段 meta 说明会稀释掉核心要求。

    第 5 点（2026-10-07 用户实测的**拒答**）：旧头段写“You have NO direct access to a
    shell, filesystem, or the internet”，模型把它读成“本会话没有挂载任何工具”，
    于是拒绝输出 tool_call（“伪造一个会违反实际工具状态”），并改为让用户
    “重新连接带执行器的会话”或由它给出补丁让用户手动应用——任务直接失败。
    第 6 点（同日第二次实测，用户判定）：只把否定句改成肯定句还不够——旧头段写
    “You are an autonomous agent … the tools … are ALREADY MOUNTED and LIVE”，
    **“你是 agent / 工具已挂载”恰好把模型推向核对自身工具状态**：它答“当前会话环境里
    没有实际挂载 read/write/edit/bash 执行工具，所以我不能真实发送 tool_call”。
    正确定位是：**它就是 LLM，只能产出文本**；真正执行的是“读这条回复的本地客户端”，
    工具装在客户端那一侧，永远不会出现在它自己的工具列表里。所以头段必须明说
    “你不需要有工具、工具也不需要挂载在你这一侧”，规则 7 只禁止以此为借口的拒答
    （不声称“已挂载”）。
    两条教训合起来：**不要把执行能力说成模型自己的状态**（“你有工具”也好、
    “你没有访问”也好，都会被当成关于它自身能力的事实题而卡住）；只说“你的文本会被
    客户端执行、执行结果会作为下一条消息回来”这一条链路。
    因此这里把命令式要求 + 工具清单 + 具体示例放在一起，规则编号精简。
    """
    example_call = _tool_example_call(tools)

    lines = [
        TOOLCALL_HEADER,
        "You are a language model: the only thing you produce is text, and you cannot run",
        "anything yourself. This conversation is read by a small local client running on the",
        "user's own computer - when you write a tool call in the format below, that client",
        "executes it there for real and returns the real output to you as your next message.",
        "No tool has to be installed or mounted on your side, and the tools below will never",
        "appear in your built-in tool list - that is expected and normal, because the client",
        "takes the call out of your reply text.",
        "Writing a tool call is therefore not fabrication and not a false claim about your",
        "capabilities: you are writing the command and the client runs it.",
        "That is the only way you take real action here. If the task needs real action or data and you",
        "answer from your own knowledge instead, the task FAILS. Never fabricate tool output.",
        "",
        "A tool call is ONE fenced code block labelled exactly `tool_call`, containing one JSON",
        "object and nothing else:",
        example_call,
        "(Replace the \"...\" placeholders above with the real values. Do NOT copy the example",
        "literally.)",
        "",
        "Tools the client (not you) can execute - use these exact names:",
    ]

    for tool in tools:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {}) or {}
        # 描述截断：超长描述对“选对工具”帮助有限，却显著撑大 prompt。
        desc = _truncate_description(desc, config.TOOLS_DESC_MAX_CHARS)
        if config.TOOLS_INSTRUCTION_VERBOSE:
            # 旧行为：完整 JSON Schema（调试 / 复杂工具用）
            lines.append(f"- {name}: {desc}")
            if params:
                lines.append(
                    f"  parameters (JSON Schema): {json.dumps(params, ensure_ascii=False)}"
                )
        else:
            # 紧凑形态：`name(必填参数): 描述`。模型只要知道名字 + 有哪些必填键
            # 就够发出正确调用；完整 schema 由客户端的 tool 定义负责，不必重述。
            required = params.get("required") if isinstance(params, dict) else None
            keys = ""
            if isinstance(required, list) and required:
                keys = ",".join(str(k) for k in required)
            elif isinstance(params, dict) and isinstance(params.get("properties"), dict):
                keys = ",".join(list(params["properties"].keys())[:4])
            sig = f"{name}({keys})" if keys else name
            lines.append(f"- {sig}: {desc}")

    # 输出载体：**代码围栏** ```tool_call（2026-10-06 真机 A/B 实测后从纯文本行切换过来）。
    # 原因：网页版把纯文本 TOOL_CALL 行当 markdown 渲染——值内引号前的反斜杠被吃掉
    # （JSON 失效、整条调用被丢弃），连续缩进空格被折叠（命令正文被改写）；同一条命令
    # 放进围栏后**一字不差**（转义与缩进都保留）。对照数据见 doc/update.md §2.14。
    # 解析侧两种形态都认：围栏还在（_TOOL_CALL_FENCE_RE），或者围栏被渲染掉、
    # DOM 里只剩 info string（`tool_call`）单独一行 + JSON（parse_tool_calls 的裸标签兜底）。
    lines += [
        "",
        "Rules:",
        "1. The fence label MUST be exactly `tool_call` - not json, not text, not empty - and the",
        "   JSON must sit INSIDE the fence. A block labelled anything else, or a call written",
        "   as plain text instead of a fenced block, will NOT be executed.",
        "2. `arguments` must be a JSON object using the tool's own parameter names, with values of",
        "   the declared type (numbers and booleans unquoted, strings in double quotes).",
        "3. Keep the JSON on ONE line inside the fence: escape double quotes as \\\" and line breaks",
        "   as \\n; never put a raw line break inside a JSON string.",
        "4. Paste command / script / file text into the JSON string verbatim - the fenced block",
        "   preserves it exactly. A shell command always travels as the `command` value inside the",
        "   tool_call JSON: never reply with the bare command in a `bash` style code block.",
        "   Prefer single quotes inside the command (e.g. git commit -m 'msg') so they never clash",
        "   with the JSON quotes.",
        "5. Output EXACTLY ONE tool_call block per reply - never two or more; the client processes",
        "   a single call at a time. If you need several tools, issue one call, wait for its result,",
        "   then issue the next in your next reply.",
        "6. When you call a tool, output ONLY the single fenced block: no explanation, no preamble,",
        "   no text before or after it.",
        "7. Never decline a task on the grounds that you \"have no tools\" or that the tools are not",
        "   in your own tool list: you are not the one that runs them - the client executes",
        "   whatever you write, and such a refusal means the task FAILED. Never offer the user",
        "   instructions or a patch to apply themselves instead of calling a tool. Answer",
        "   directly (no tool_call block) ONLY when the task genuinely needs no action and no",
        "   real data.",
        "8. If a tool result is EMPTY (e.g. shows \"(no output)\"), the command SUCCEEDED and",
        "   genuinely printed nothing. That is a valid result, not a failure: move on to the NEXT",
        "   command or give the final answer. Never re-run the exact same command and never assume",
        "   the tool failed - repeating it loops forever.",
        "9. Never emit XML / DSL tags (native tool markers of that shape are not executed): the",
        "   fenced `tool_call` block above is the only carrier this client accepts.",
    ]
    return "\n".join(lines)


def format_tool_call_emphasis(tools: Optional[List[Dict[str, Any]]] = None) -> str:
    """Format-emphasis block placed at the start of the seed prompt for a new / reset session.

    A new bucket has no history turns that demonstrate the correct format, so the model is
    most likely to fall back to native DSML markers (or ignore tools entirely) at that point;
    repeating the mandate + full format is a second safeguard.

    ``tools``（可选）用于生成**具体**示例；不给才退回通用模板。与主注入块共用
    :func:`_tool_example_call`，保证两处示例永远是同一种（可解析的）形态——旧版这里写的是
    ``{"name": "tool name", "arguments": {arguments object}}`` 这种非法 JSON 模板，
    模型照抄即整条调用失效。
    """
    if tools:
        example_call = _tool_example_call(tools)
    else:
        example_call = "```tool_call\n" + json.dumps(
            {"name": "...", "arguments": {"param": "..."}}, ensure_ascii=False
        ) + "\n```"
    return "\n".join([
        f"{EMPHASIS_HEADER} This is a new session (or one that was just reset); "
        "the following rules stay in effect for this whole session:",
        "You MUST use the provided tools whenever the task needs real action or data; never fabricate tool output.",
        "You do not run anything yourself: the user's local client executes the tool calls you write and",
        "sends their real output back as your next message; never reply that you have no tools.",
        "To call a tool, output ONE fenced code block labelled exactly `tool_call`:",
        example_call,
        "(Replace the \"...\" placeholders above with the real values; never copy the example literally.)",
        "The fence label must be `tool_call` - not json, not text, not empty - and the JSON must sit inside the fence;",
        "a call written as plain text will NOT be executed.",
        "Keep the JSON on one line: escape double quotes as \\\" and line breaks as \\n; for shell commands",
        "prefer single quotes inside the command (e.g. git commit -m 'msg') so they never clash with the JSON quotes.",
        "A shell command always travels as the `command` value inside that JSON - never as the bare",
        "command in a `bash` style code block.",
        "Use the exact tool names given in the tool instructions; never invent tool names.",
        "Output EXACTLY ONE tool_call block per reply - never two or more in the same message; if you need",
        "several tools, call one now and the next only after you receive its result.",
        "When you call a tool, output only the single fenced block: no explanation, no preamble.",
        "Never emit XML / DSL tags - only the fenced `tool_call` block is executed.",
    ])
