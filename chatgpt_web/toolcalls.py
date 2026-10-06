"""工具调用（function calling）桥接层。

ChatGPT 网页版并不原生支持 OpenAI 的 function calling，因此这里采用
“提示词注入 + 结构化解析”的方式模拟：
  1. 把客户端传来的 tools 描述注入到 prompt，要求模型用 ```tool_call 代码块回话；
  2. 解析模型输出里的代码块，还原为 OpenAI 的 tool_calls；
  3. 下一轮请求里 role=tool 的执行结果再拼回 prompt 喂给网页版。
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import config
from .models import FunctionCall, ToolCall

logger = logging.getLogger(__name__)

# 历史载体（仍兼容，不再是注入格式）：行首 ``TOOL_CALL:`` 纯文本标记（大小写不敏感）。
# 2026-10-06 起注入格式改为 ```tool_call 代码围栏（原因：网页版把纯文本行当 markdown
# 渲染，会吃掉反斜杠转义并折叠连续空格，见 doc/code_block_fence.md）。
# 只认行首（允许前导空白），避免正文里偶然出现的 "TOOL_CALL:" 被误触发；
# 后续 JSON 由 _iter_balanced_objects 从冒号之后开始扫。
_TOOL_CALL_LINE_RE = re.compile(r"^[ \t]*TOOL_CALL\s*:\s*", re.IGNORECASE | re.MULTILINE)
# 仅匹配 "tool_call" / "tool-call" 围栏，避免误伤普通 ```json 代码块。
# 允许围栏被 DOM/引用符号包裹： ``> ```tool_call `` 这类形态也要能识别。
_TOOL_CALL_FENCE_RE = re.compile(r"```\s*(tool[-_]call)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# ChatGPT 网页版 markdown 渲染会在标识符里插入转义反斜杠：
#   TOOL_CALL -> TOOL\_CALL, exec_command -> exec\_command
# 取回 inner_text 时就带着这些反斜杠。解析前先去掉“反斜杠 + 下划线”的转义，
# 否则行首标记正则匹配不到，整条调用被丢弃。
_MD_ESCAPED_CHAR_RE = re.compile(r"\\([_*`~\[\]()#+.!\-])")
# "json" 围栏仅在内容明显是工具调用时才采纳（兜底，兼容模型不听话的情况）
_JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
# 网页版偶尔会输出 DSML 风格的工具调用 XML（全角竖线 ｜｜ 包裹的标签），
# 形如： <｜｜DSML｜｜ calls>{"tool_uses": [...]}</｜｜DSML｜｜ calls>
# 这里只取标签之间的 JSON 对象，交由 _consume 解析。
_DSML_TOOL_RE = re.compile(
    r"<\s*[｜|]{2}\s*DSML\s*[｜|]{2}[^>]*>(.*?)<\s*/\s*[｜|]{2}\s*DSML\s*[｜|]{2}",
    re.DOTALL | re.IGNORECASE,
)
# 无标签但带 "tool_uses" 键的裸 JSON 对象（DOM 提取后标签可能丢失）
_TOOL_USES_RE = re.compile(r"tool_uses\s*\"?\s*:", re.IGNORECASE)

# DSML **结构化**形态（ChatGPT 原生工具 DSL，模型不听指令时会退回这种写法）：
#   <｜｜DSML｜｜ calls>
#   <｜｜DSML｜｜ invoke name="bash">
#   <｜｜DSML｜｜ parameter name="command" string="true">cd /tmp && ls</｜｜DSML｜｜ parameter>
#   </｜｜DSML｜｜ invoke>
#   </｜｜DSML｜｜ calls>
# 竖线数量不固定（DOM 提取后 1~3 个都出现过），标签内允许空白；
# 闭标签还可能缺失/错位（回复被截断），解析时按开标签切块兜底。
_BAR = r"[｜|]{1,4}"
_DSML_INVOKE_OPEN_RE = re.compile(rf"<\s*{_BAR}\s*DSML\s*{_BAR}\s*invoke\b([^>]*)>", re.IGNORECASE)
_DSML_INVOKE_CLOSE_RE = re.compile(rf"<\s*/\s*{_BAR}\s*DSML\s*{_BAR}\s*invoke\s*>", re.IGNORECASE)
_DSML_PARAM_OPEN_RE = re.compile(rf"<\s*{_BAR}\s*DSML\s*{_BAR}\s*parameter\b([^>]*)>", re.IGNORECASE)
_DSML_PARAM_CLOSE_RE = re.compile(rf"<\s*/\s*{_BAR}\s*DSML\s*{_BAR}\s*parameter\s*>", re.IGNORECASE)
# 参数值的 JSON 标量识别（string="true" 时不参与）
_DSML_SCALAR_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null", re.IGNORECASE)
# ChatGPT 偶尔不用我们的工具名，而用 bash/shell 这类通用名；只有能唯一对应时才映射
_DSML_GENERIC_NAMES = {
    "bash", "sh", "shell", "terminal", "command", "cmd", "exec", "execute",
    "run_command", "run_commands", "execute_command",
}
_SHELL_NAME_KEYWORDS = ("shell", "exec", "bash", "command", "term")


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


# ==================== 内置工具：edit_markdown ====================
# 桥接层内置的 Markdown 锚点编辑工具。模型只需给出行号区间与新文本，
# 桥接层用 markdown_io 做围栏安全的定位/校验/保真写回，避免整段文本匹配。
EDIT_MARKDOWN_TOOL_NAME = "edit_markdown"

EDIT_MARKDOWN_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": EDIT_MARKDOWN_TOOL_NAME,
        "description": (
            "按行号区间编辑本地 Markdown 文件：保留围栏代码块结构，"
            "只替换 [start, end] 行，区间外字节级保真。默认只返回 diff（dry-run），"
            "传 write=true 才落盘，落盘前自动备份。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标 Markdown 文件路径"},
                "start": {"type": "integer", "description": "起始行号（1-based，闭区间）"},
                "end": {"type": "integer", "description": "结束行号（1-based，闭区间）"},
                "new_text": {"type": "string", "description": "替换 [start, end] 的新文本"},
                "write": {
                    "type": "boolean",
                    "description": "true 才落盘；默认 false 仅返回 diff",
                },
            },
            "required": ["path", "start", "end", "new_text"],
        },
    },
}

# 所有可由桥接层本地执行的内置工具。供响应层按需注入/执行。
BUILTIN_TOOLS: List[Dict[str, Any]] = [EDIT_MARKDOWN_TOOL]


def builtin_tool_names() -> set:
    return {t["function"]["name"] for t in BUILTIN_TOOLS}


def edit_markdown_spec() -> str:
    """Usage notes for edit_markdown injected into the prompt (anchors / fences caveats)."""
    return "\n".join([
        EDIT_MD_HEADER,
        "When editing a Markdown file, prefer edit_markdown over rewriting the whole file and doing plain-text matching:",
        "```tool_call",
        '{"name": "edit_markdown", "arguments": {"path": "README.md", '
        '"start": <int>, "end": <int>, "new_text": "<replacement text>"}}',
        "```",
        "start/end are 1-based inclusive line numbers; content outside the range (including blank lines, indentation, trailing whitespace) is preserved verbatim.",
        "Do not touch ``` fence lines; content inside a fence does not participate in structural positioning.",
        "By default only a diff is returned; once confirmed, pass write=true to persist to disk.",
    ])


def resolve_edit_path(path: Any) -> tuple[Optional[Path], Optional[str]]:
    """把模型给出的 path 解析到沙箱之内，返回 ``(沙箱内的绝对路径, 错误原因)``。

    安全约束（T1.3）：edit_markdown 是**模型可控**的写文件工具，而模型可能
    被网页内容 / 工具结果注入。因此：

    * 拒绝绝对路径（不管它是否在沙箱内）；
    * 拒绝含 ``..`` 段的路径；
    * 解析后必须落在 ``config.EDIT_MARKDOWN_ROOT``（默认项目根）之内。

    返回的第二个值非空时，调用方应直接以 ``{"ok": false, "error": ...}`` 回传。
    """
    if not isinstance(path, str) or not path.strip():
        return None, "edit_markdown 需要 path"
    raw = Path(path.strip())
    if raw.is_absolute():
        return None, f"路径越界（不允许绝对路径）：{path}"
    if ".." in raw.parts:
        return None, f"路径越界（不允许 .. 段）：{path}"
    root = Path(config.EDIT_MARKDOWN_ROOT or config.PROJECT_ROOT).resolve()
    resolved = (root / raw).resolve()
    try:
        inside = resolved.is_relative_to(root)
    except AttributeError:  # pragma: no cover - Python < 3.9 的兜底
        inside = str(resolved).startswith(str(root) + os.sep)
    if not inside:
        return None, f"路径越界（必须位于 {root} 之内）：{path}"
    # 返回**解析后的绝对路径**：读写必须落在沙箱解析结果上，
    # 而不是让 markdown_io 拿相对路径去拼当前工作目录（可能落到沙箱之外）。
    return resolved, None


def run_local_edit_markdown(
    tool_calls: List[Dict[str, Any]],
    *,
    backup_dir: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """本地执行 edit_markdown：把结果挂回对应调用（server / responses 共用）。

    非 ``edit_markdown`` 的调用原样透传，顺序与内容不变。未开启本地执行或
    没有调用时原样返回。
    """
    if enabled is None:
        enabled = config.EDIT_MARKDOWN_LOCAL
    if backup_dir is None:
        backup_dir = config.EDIT_MARKDOWN_BACKUP_DIR
    if not enabled or not tool_calls:
        return tool_calls
    out: List[Dict[str, Any]] = []
    for call in tool_calls:
        if call.get("name") == EDIT_MARKDOWN_TOOL_NAME:
            result = execute_edit_markdown(
                call.get("arguments") or {}, backup_dir=backup_dir
            )
            out.append({**call, "result": result})
        else:
            out.append(call)
    return out


def execute_edit_markdown(args: Dict[str, Any], *, backup_dir: str = "output/backups") -> Dict[str, Any]:
    """桥接层本地执行 edit_markdown。返回可直接回传的结构化结果。

    - 路径沙箱：只接受沙箱内的相对路径（见 :func:`resolve_edit_path`）。
    - 默认 dry-run：只返回统一 diff，不落盘。
    - ``write=true`` 仅在 ``EDIT_MARKDOWN_WRITE=true`` 时真正落盘；否则降级为
      dry-run 并在结果里说明（模型不能自己“申请”写权限）。
    - 任何结构性错误（围栏不配对、行号越界）都作为 error 返回，不抛给上层。
    """
    from . import markdown_io

    requested_path = args.get("path")
    path, path_error = resolve_edit_path(requested_path)
    if path_error:
        return {"ok": False, "error": path_error}
    assert path is not None
    try:
        start = int(args["start"])
        end = int(args["end"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "edit_markdown 需要合法的 start/end"}
    new_text = args.get("new_text", "")
    if not isinstance(new_text, str):
        return {"ok": False, "error": "edit_markdown 的 new_text 必须是字符串"}
    write_requested = bool(args.get("write", False))
    do_write = write_requested and config.EDIT_MARKDOWN_WRITE
    write_refused = write_requested and not do_write

    try:
        doc = markdown_io.read_md(path)
    except FileNotFoundError:
        return {"ok": False, "error": f"文件不存在：{path}"}
    except markdown_io.MarkdownError as exc:
        return {"ok": False, "error": str(exc)}

    total = len(doc.lines)
    if start < 1 or end > total or start > end:
        return {"ok": False, "error": f"行号越界：{start}-{end}（共 {total} 行）"}

    try:
        edited = markdown_io.apply_edit(doc, start, end, new_text)
    except markdown_io.MarkdownError as exc:
        return {"ok": False, "error": str(exc)}

    issues = markdown_io.verify(edited)
    if issues:
        return {
            "ok": False,
            "error": "编辑后结构校验未通过",
            "issues": [{"code": i.code, "message": i.message, "line": i.line} for i in issues],
        }

    backup_path = None
    if do_write:
        try:
            backup = markdown_io.backup_md(path, backup_dir=backup_dir)
            backup_path = str(backup.backup_path)
        except OSError as exc:
            logger.warning("edit_markdown 备份失败（%s）：未写盘。", path, exc_info=True)
            return {"ok": False, "error": f"备份失败：{exc}"}

    diff = markdown_io.write_md(edited, path=path, dry_run=not do_write)
    result: Dict[str, Any] = {
        "ok": True,
        "path": requested_path,
        "resolved_path": str(path),
        "start": start,
        "end": end,
        "write_requested": write_requested,
        "written": do_write,
        "backup": backup_path,
        "diff": diff,
    }
    if write_refused:
        result["note"] = (
            "已忽略 write=true：EDIT_MARKDOWN_WRITE=false（默认只做 dry-run，"
            "不落盘）。需要落盘请在 .env 里显式打开。"
        )
    return result


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
        "You have NO direct access to a shell, filesystem or the internet. Answering from",
        "your own knowledge instead of calling a tool means the task FAILED.",
        "Reply now with EXACTLY ONE fenced code block in this form (no other text):",
        "```tool_call",
        '{"name": "<exact tool name>", "arguments": {"<param>": <value>}}',
        "```",
    ])


def tool_call_predicate(tools: Optional[List[Dict[str, Any]]]) -> Callable[[str], bool]:
    """构造「回复里是否包含至少一个工具调用」的判定函数。

    供 ``chat_io.send_chat`` 的纠偏重试使用：判定失败就追发一次纠偏指令。
    与最终解析共用同一个 ``parse_tool_calls``，不会出现「重试判定说不合格、
    外面却解析出了 tool_calls」的不一致。
    """
    names = _tool_names(tools)

    def _has_call(text: str) -> bool:
        return bool(parse_tool_calls(text or "", names))

    return _has_call


def format_tools_instruction(tools: List[Dict[str, Any]]) -> str:
    """把 OpenAI tools 描述转换成注入网页版的自然语言指令。

    实测（联网真机验证）：模型很容易**无视工具、直接凭知识作答**——例如问它
    “查看当前目录”时，它会直接编一份 ls 输出，parse 结果为空。让模型真正调用
    工具的关键有三点：
      1. 明确切断退路：“你没有直接的 shell/文件系统访问，唯一方式是输出
         TOOL_CALL 行，直接作答＝任务失败”；
      2. 给一个**具体到参数**的调用示例（只给格式模板不够）；
      3. 指令要短、聚焦，长段 meta 说明会稀释掉核心要求。
    因此这里把命令式要求 + 工具清单 + 具体示例放在一起，规则尽量精简。
    """
    # 示例要带真实工具名和参数键，否则模型不认；但值必须是「一眼就知道要替换」的
    # 占位符——用 <command> 这种形式模型会原样照抄，把占位符当命令发出来
    # （实测：shell 收到字面量 <command> 直接语法报错）。这里用 "..." 并显式声明。
    first = tools[0] if tools else {}
    fn0 = first.get("function", first) if isinstance(first, dict) else {}
    example_name = fn0.get("name") or "tool_name"
    props = (fn0.get("parameters") or {}).get("properties") or {}
    example_args = {key: "..." for key in list(props.keys())[:2]} or {}
    example_call = "```tool_call\n" + json.dumps(
        {"name": example_name, "arguments": example_args}, ensure_ascii=False
    ) + "\n```"

    lines = [
        TOOLCALL_HEADER,
        "You are an agent connected to external tools. You have NO direct access to a shell,",
        "filesystem, or the internet — the ONLY way to perform an action or fetch real data is",
        "to emit a TOOL_CALL line. If the task needs a tool and you answer from your own",
        "knowledge instead, the task FAILS. Never fabricate tool output.",
        "",
        "Available tools (use these exact names):",
    ]
    from . import config

    for tool in tools:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name", "")
        desc = fn.get("description", "")
        params = fn.get("parameters", {}) or {}
        # 描述截断：超长描述对“选对工具”帮助有限，却显著撑大 prompt。
        limit = config.TOOLS_DESC_MAX_CHARS
        if limit and len(desc) > limit:
            desc = desc[:limit] + "…"
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
        "To call a tool, output a fenced code block whose info string is exactly `tool_call`,",
        "containing ONE JSON object and nothing else:",
        example_call,
        "(In the example above, \"...\" is a placeholder: replace it with the real value.",
        "Do NOT copy the example literally.)",
        "Rules:",
        "- The fence info string MUST be exactly `tool_call` (not json, not text, not empty):",
        "  a block labelled anything else, or a JSON object written as plain text, will NOT be",
        "  executed. Do not write the call as a plain `TOOL_CALL: {...}` line — the web UI",
        "  mangles plain-text lines (it eats backslash escapes and collapses indentation).",
        "- `arguments` must be a valid JSON object matching the tool's parameters.",
        "- Inside JSON strings, escape double quotes as \\\" and newlines as \\n; keep the JSON on",
        "  one line inside the fence.",
        "- Paste command/script text into the JSON string verbatim - the fenced block preserves it.",
        "- For shell commands, prefer single quotes inside the command.",
        "- Output EXACTLY ONE tool_call block per reply. Never emit two or more blocks",
        "  together; the client can only process a single call at a time. If you need several",
        "  tools, issue one call, wait for its result, then issue the next in your next reply.",
        "- When you call a tool, output ONLY the single fenced block: no explanation, no preamble.",
        "- Only if the task needs no tool at all, answer directly with no tool_call block.",
        "- If a tool result is EMPTY (e.g. shows \"(no output)\"), the command SUCCEEDED and",
        "  genuinely printed nothing. That is a valid result, not a failure: move on to the NEXT",
        "  command or give the final answer. Never re-run the exact same command and never assume",
        "  the tool failed — repeating it loops forever.",
        "- Do not output XML/DSL markers such as <｜DSML｜ ...>, <invoke>/<parameter> — they will not be executed.",
    ]
    return "\n".join(lines)


def format_tool_call_emphasis() -> str:
    """Format-emphasis block placed at the start of the seed prompt for a new / reset session.

    A new bucket has no history turns that demonstrate the correct format, so the model is
    most likely to fall back to native DSML markers (or ignore tools entirely) at that point;
    repeating the mandate + full format is a second safeguard.
    """
    return "\n".join([
        f"{EMPHASIS_HEADER} This is a new session (or one that was just reset); "
        "the following rules stay in effect for this whole session:",
        "You MUST use the provided tools whenever the task needs real action or data; never fabricate tool output.",
        "To call a tool, output a fenced code block whose info string is exactly `tool_call`:",
        "```tool_call",
        "{\"name\": \"tool name\", \"arguments\": {arguments object}}",
        "```",
        "The fence label must be `tool_call` (not json/text/empty); a call written as plain text will NOT be executed.",
        "arguments must be valid JSON: escape double quotes inside strings as \\\" and newlines as \\n; never write a bare double quote or a raw newline inside a JSON string.",
        "if an argument is a shell command, **switch to single quotes** inside the command (e.g. git commit -m 'msg'), "
        "to avoid a clash between double quotes in the command and the JSON boundary quotes.",
        "Use the exact tool names given in the tool instructions; do not invent generic names like bash / shell.",
        "Output EXACTLY ONE tool_call block per reply - never two or more in the same message.",
        "If you need several tools, call one now and the next only after you receive its result.",
        "Output only the single fenced tool_call block when calling a tool: no explanation, no preamble.",
        "Do not output XML/DSL markers such as <｜DSML｜ ...>, <invoke>/<parameter>, <tool_calls> - they will not be executed.",
    ])


def _dsml_blocks(open_re, close_re, text: str):
    """按 DSML 开标签切出 (属性串, 块体)。

    闭标签缺失/错位（回复被截断、DOM 吞标签）时，退化为取到下一个开标签或文末。
    """
    pos = 0
    while True:
        opened = open_re.search(text, pos)
        if not opened:
            return
        start = opened.end()
        closed = close_re.search(text, start)
        nxt = open_re.search(text, start)
        if closed and (nxt is None or closed.start() < nxt.start()):
            yield opened.group(1), text[start:closed.start()]
            pos = closed.end()
        elif nxt:
            yield opened.group(1), text[start:nxt.start()]
            pos = nxt.start()
        else:
            yield opened.group(1), text[start:]
            return


def _dsml_attr(attrs: str, key: str) -> Optional[str]:
    """从开标签的属性串里取 ``key="value"``。"""
    match = re.search(rf'(?:^|\s){re.escape(key)}\s*=\s*"([^"]*)"', attrs)
    return match.group(1) if match else None


def _dsml_param_value(raw: str, string_attr: Optional[str]) -> Any:
    """DSML parameter 内容 -> Python 值。"""
    value = raw.strip()
    if (string_attr or "").strip().lower() == "true":
        return value
    if value[:1] in "{[" or _DSML_SCALAR_RE.fullmatch(value):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _resolve_dsml_name(name: str, valid_names: Optional[set]) -> str:
    """把 DSML invoke 的工具名对齐到客户端工具名；对不上就原样返回（后续过滤）。"""
    if not name or not valid_names:
        return name
    if name in valid_names:
        return name
    lowered = name.strip().lower()
    for candidate in valid_names:
        if candidate.lower() == lowered:
            return candidate
    if lowered in _DSML_GENERIC_NAMES:
        hits = [c for c in valid_names if any(k in c.lower() for k in _SHELL_NAME_KEYWORDS)]
        if len(hits) == 1:
            return hits[0]
    return name


def _parse_dsml_invokes(text: str, valid_names: Optional[set] = None) -> List[Dict[str, Any]]:
    """解析 DSML 结构化工具调用（invoke/parameter 形态）。"""
    calls: List[Dict[str, Any]] = []
    for attrs, body in _dsml_blocks(_DSML_INVOKE_OPEN_RE, _DSML_INVOKE_CLOSE_RE, text):
        name = _dsml_attr(attrs, "name")
        if not name:
            continue
        arguments: Dict[str, Any] = {}
        for pattrs, pvalue in _dsml_blocks(_DSML_PARAM_OPEN_RE, _DSML_PARAM_CLOSE_RE, body):
            pname = _dsml_attr(pattrs, "name")
            if not pname:
                continue
            arguments[pname] = _dsml_param_value(pvalue, _dsml_attr(pattrs, "string"))
        calls.append({"name": _resolve_dsml_name(name, valid_names), "arguments": arguments})
    return calls


def _normalize_tool_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    function = entry.get("function") or {}
    name = entry.get("name") or entry.get("tool") or function.get("name")
    arguments = entry.get("arguments")
    if arguments is None:
        arguments = entry.get("parameters")
    if arguments is None:
        arguments = function.get("arguments")
    if arguments is None:
        arguments = {}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            pass
    if not name:
        return None
    return {"name": name, "arguments": arguments}


def _tool_names(tools: Optional[List[Dict[str, Any]]]) -> set:
    """从 OpenAI tools 描述里收集合法工具名，用于过滤误报。"""
    names = set()
    for tool in tools or []:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name")
        if name:
            names.add(name)
    return names


def _strip_redundant_value_quotes(raw: str) -> str:
    """折叠字符串值边界上多余的引号：``"cmd": ""git ...""`` -> ``"cmd": "git ..."``。

    模型（尤其 ChatGPT）常把参数值**又用一对引号包了一层**，或在值首/尾多写一个
    引号。这类输入括号是平衡的，但 JSON 非法；表现就是参数值被从第一个引号处
    截断（解析成空串）或整体解析失败。这里只在**值的开头**（``:`` 之后）和
    **值的结尾**（``,``/``}``/``]`` 之前）各折叠连续引号，正文中的引号不动。
    """
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        # 值开头：冒号后跳过空白，若连续 >=2 个引号则只留一个（保留真正的开引号）
        if ch == ":":
            out.append(ch)
            i += 1
            # 跳过空白
            while i < n and raw[i] in " \t\r\n":
                out.append(raw[i])
                i += 1
            if i < n and raw[i] == '"':
                j = i
                while j < n and raw[j] == '"':
                    j += 1
                if j - i >= 2:
                    # 折叠为单个开引号
                    out.append('"')
                    i = j
                    continue
            continue
        # 值结尾：连续 >=2 个引号且后面是结构符/结尾，只留一个（真正的闭引号）。
        # 注意：若前一个输出字符是反斜杠（转义引号），说明这是正文引号，跳过折叠。
        if ch == '"':
            j = i
            while j < n and raw[j] == '"':
                j += 1
            run = j - i
            k = j
            while k < n and raw[k] in " \t\r\n":
                k += 1
            escaped_prefix = bool(out) and out[-1] == "\\"
            if run >= 2 and (k >= n or raw[k] in ",}]") and not escaped_prefix:
                out.append('"')
                i = j
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_control_chars_in_strings(raw: str) -> str:
    """把 JSON 字符串**内部**的裸控制字符转义（`\\n`/`\\r`/`\\t` 等）。

    JSON 规范禁止字符串字面量里出现未转义的控制字符。模型写多行 shell
    命令（如 heredoc）时，常把真实换行直接写进值里，导致：
        json.loads: Invalid control character at ...
    这类损伤**没有歧义**——字符串内的裸控制字符一律转义即可，是安全修复。

    逐字符扫描，用 `in_string`/`escaped` 跟踪状态，只在字符串内部替换。
    """
    out = []
    in_string = False
    escaped = False
    for ch in raw:
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            continue
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            out.append(ch)
            in_string = False
            continue
        # 字符串内部：裸控制字符转义
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


_NAME_ARG_COMMAND_RE = re.compile(
    r'^\s*{\s*"name"\s*:\s*"(?P<name>[^"\\]+)"\s*,\s*"arguments"\s*:\s*{\s*"(?P<argkey>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"',
    re.DOTALL,
)


# 前面已完整闭合的字符串参数对，形如 `"path": "README.md",`。
_STRING_ARG_PAIR_RE = re.compile(r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"(?P<val>(?:[^"\\]|\\.)*)"\s*,')


def _parse_complete_string_args(raw: str):
    """Parse leading ``"key": "value",`` pairs; return None if anything is off.

    Used by :func:`_salvage_string_args` to recover the well-formed arguments
    that precede an object's broken final string value.
    """
    text = raw.strip()
    if text.endswith(","):
        text = text[:-1].rstrip()
    if not text:
        return {}
    out = {}
    pos = 0
    while pos < len(text):
        m = _STRING_ARG_PAIR_RE.match(text, pos)
        if not m:
            return None
        try:
            out[m.group("key")] = json.loads('"' + m.group("val") + '"')
        except Exception:
            return None
        pos = m.end()
    return out


def _salvage_string_args(raw: str):
    """Salvage objects shaped like {"name": X, "arguments": {"<key>": "<body>"[, ...]}}.

    When a value string contains raw newlines or unescaped inner quotes (the
    common case for markdown / shell content) that defeat JSON repair, locate
    structurally: anchor ``name`` and the leading string-valued argument key(s)
    with a regex, take everything from that key's opening quote up to the
    object-closing quote before ``}}``, and re-serialize. Only objects whose
    preceding arguments are all well-formed quoted strings are accepted, which
    keeps multi-key shapes like the ``write`` tool (path + content) working
    while refusing to guess at nested/array values.
    """
    m = _NAME_ARG_COMMAND_RE.match(raw)
    if not m:
        return None
    name = m.group("name")
    argkey = m.group("argkey")
    body_start = m.end()  # first char of the anchored key's value
    # Prefix covers any fully-quoted args before the anchored key; it starts
    # right after the `{` that opens the arguments object (the one immediately
    # preceding the anchored key), so the slice holds `"path": "...",` pairs.
    brace = raw.rfind("{", 0, body_start)
    if brace < 0:
        return None
    head = brace + 1
    stripped = raw.rstrip()
    if not stripped.endswith("}}"):
        return None
    close = stripped.rindex('"')
    if close < body_start:
        return None

    # The prefix ends with the anchored key's own `"<argkey>": "` fragment
    # (the regex consumed up to its opening quote). Drop that trailing fragment
    # so only complete preceding `"key": "value",` pairs remain.
    prefix = raw[head:body_start]
    anchor_frag = '"' + argkey + '"'
    anchor_pos = prefix.rfind(anchor_frag)
    if anchor_pos >= 0:
        prefix = prefix[:anchor_pos]
    prefix_args = _parse_complete_string_args(prefix)
    if prefix_args is None:
        return None

    value = raw[body_start:close]
    try:
        value = json.loads('"' + value + '"')
    except Exception:
        escaped = value.replace("\\", "\\\\")
        escaped = escaped.replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        try:
            value = json.loads('"' + escaped + '"')
        except Exception:
            return None
    prefix_args[argkey] = value
    return {"name": name, "arguments": prefix_args}


def _salvage_missing_final_brace(segment: str) -> Optional[Dict[str, Any]]:
    """最后一道兜底：DOM 取回的文本若只剩一个收尾 ``}``（少一个），也试着解析。

    背景（2026-10-06 真机）：ChatGPT 网页版把模型写的纯文本 ``TOOL_CALL:`` 行当
    markdown 渲染，渲染过程会**吃掉一层反斜杠转义**并把连续空格折叠成一个
    （见 doc/update.md §2.13）：

    * 值内引号前的反斜杠被吞掉：JSON 里的转义引号到 DOM 里变成裸引号；
    * 双反斜杠被吞掉一个：字面量 ``\\n`` 到 DOM 里会变成真换行；
    * 连续缩进空格被折叠。

    这类文本既不是合法 JSON，括号也不平衡，只能走 :func:`_salvage_string_args`
    的锚点式 salvage；但实测**取回的文本在值的闭引号之后往往只剩一个 ``}``**
    （外层对象的收尾花括号丢失），而那个函数要求 ``endswith("}}")``，于是直接
    放弃 → 整条调用被丢掉 → 客户端只收到纯文本、把回复当成最终答案、任务静默结束。

    这里只做一件极窄的事：文本恰好以**单个** ``}`` 结尾时，补一个 ``}`` 再交给
    :func:`_salvage_string_args`；能否救回仍由它的锚点匹配与
    :func:`_parse_complete_string_args` 守卫决定，救不回依旧返回 ``None``。
    真正缺失内容（值本身被截断）的回复不会因此变成“猜出来的调用”。
    """
    stripped = segment.rstrip()
    if not stripped.endswith("}") or stripped.endswith("}}"):
        return None
    salvaged = _salvage_string_args(stripped + "}")
    if salvaged is not None:
        logger.warning(
            "工具调用 JSON 缺少外层的收尾花括号（少一个 }），已按锚点式 salvage 兜底"
            "解析。这通常意味着 ChatGPT 的 markdown 渲染改写了这条 TOOL_CALL 行"
            "（吃掉一层反斜杠转义 / 折叠连续空格，见 doc/update.md §2.13）。"
        )
    return salvaged


def _repair_json_quotes(raw: str) -> Optional[Any]:
    """尽力修复模型输出的非法 JSON。

    常见损伤：
      1. 字符串值里未转义的裸引号：
         {"cmd": "git commit -m "Update logic" && git push"}
      2. 值边界多余引号（模型给值又包了一层）：
         {"cmd": ""git status && git log""}
         表现为参数值从第一个引号处被截断、或整体解析失败。
      3. 值内含嵌套的 shell 双引号，且末尾闭引号看似"丢失"：
         {"cmd": "git commit -m "msg"}
         逐字符启发式会把它当"字符串结束"而截断值、丢掉尾引号。
      4. 值内有裸控制字符（多行命令/heredoc 的真实换行）：
         json.loads: Invalid control character at ...
         这类无歧义，优先修复。

    处理顺序：先折叠边界冗余引号、转义字符串内控制字符，再用逐字符
    状态机修内层裸引号。只在首次 json.loads 失败后调用。
    """
    raw = _strip_redundant_value_quotes(raw)

    # 无歧义修复优先：字符串内裸控制字符（多行命令的真实换行等）。
    # 很多长指令只因这一项就无法解析，单独先试一次。
    ctrl_fixed = _escape_control_chars_in_strings(raw)
    if ctrl_fixed != raw:
        try:
            return json.loads(ctrl_fixed)
        except Exception:
            raw = ctrl_fixed  # 控制字符已修，继续尝试引号修复

    # 说明：曾尝试用"结构定位"重写值内嵌套引号，但 JSON 值内嵌 shell 双引号
    # 本质有歧义（无法区分"值的边界引号"与"正文引号"），实验版本会产出"合法
    # 但错误"的截断命令。改为在 prompt 层要求命令内部用单引号从源头消除歧义，
    # 解析层只保留确定性修复 + 下方护栏。
    out = []
    in_string = False
    escaped = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
                escaped = False
            i += 1
            continue
        # 处于字符串内部
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if ch == '"':
            # 向后看第一个非空白字符，判断是否为字符串真结束
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j >= n or raw[j] in ":,":
                out.append(ch)
                in_string = False
            elif raw[j] in "}]":
                # 引号后是 } / ] 时再看一层：值真结束时，} / ] 之后必然是
                # 结构符（, } ]）或输入结束；若是正文引号（如
                # `[contenteditable="true"]` 中 true 后面那个 `"`），紧跟的
                # 是正文字符，不能当作字符串结束——否则字符串被提前闭合、
                # 后续修复失败，整条工具调用被丢弃。
                k = j + 1
                while k < n and raw[k] in " \t\r\n":
                    k += 1
                if k >= n or raw[k] in ",}]":
                    out.append(ch)
                    in_string = False
                else:
                    out.append('\\"')
            else:
                # 正文引号：转义后保留
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    repaired = "".join(out)
    # 引号修复后字符串体内可能仍留着裸控制字符（多行命令的真实换行）：
    # 逐字符修复阶段不会转义它们，这里补一次，否则最终 json.loads 仍会报
    # "Invalid control character" 而返回 None、整条调用被丢弃。
    repaired = _escape_control_chars_in_strings(repaired)
    try:
        return json.loads(repaired)
    except Exception:
        return None


def _shell_quotes_balanced(cmd: str) -> bool:
    """粗判 shell 命令里的双引号是否成对（忽略 \\" 转义引号）。

    解析出的命令若引号不配对，几乎必然是 JSON 修复阶段把值截断/丢尾引号，
    直接发给 shell 只会得到 `unexpected EOF`。宁可在桥接层拦下，让模型重出。
    """
    count = 0
    i = 0
    n = len(cmd)
    while i < n:
        if cmd[i] == "\\":
            i += 2
            continue
        if cmd[i] == '"':
            count += 1
        i += 1
    return count % 2 == 0


def _iter_balanced_objects(text: str):
    """扫描文本，产出顶层、括号平衡的 JSON 对象字面量（能正确处理字符串与转义）。"""
    in_string = False
    escaped = False
    depth = 0
    start = -1
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = index
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:index + 1]
                    start = -1


def parse_tool_calls(text: str, valid_names: Optional[set] = None) -> List[Dict[str, Any]]:
    """从模型回复中解析出工具调用列表。返回 [{"name": ..., "arguments": {...}}, ...]

    需要兼容多种形态：
      0. **当前注入格式**：带围栏的 ```tool_call ... ```代码块（围栏内一条 JSON）；
      1. 行首 ``TOOL_CALL: {...}`` 纯文本标记——**历史载体**，仍兼容；
         网页版会把这行当 markdown 渲染并改坏它，见 doc/code_block_fence.md；
      2. **无围栏**的 ``tool_call`` 标签 + JSON 对象——这是从 ChatGPT 网页 DOM
         提取 inner_text 后的常见形态：代码块被渲染成 <pre>，围栏退化为标题文字，
         于是只剩 ``tool_call`` 标签与裸 JSON（当前注入格式被渲染后的实际形态）。
    """
    if not text:
        return []

    # ChatGPT 网页版 markdown 渲染会在下划线等字符前插入反斜杠（TOOL\_CALL、
    # exec\_command），取回 inner_text 时带着这些转义。先还原，再解析。
    text = _MD_ESCAPED_CHAR_RE.sub(r"\1", text)

    calls: List[Dict[str, Any]] = []

    def _consume(raw: str, allow_bare_object: bool) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            # 模型常把 shell 命令里的引号原样写进 JSON 字符串（未转义），
            # 标准解析失败；退回尽力修复（见 _repair_json_quotes）。
            data = _repair_json_quotes(raw)
            if data is None:
                salvaged = _salvage_string_args(raw)
                if salvaged is not None:
                    calls.append(salvaged)
                return
        if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
            entries = data["tool_calls"]
        elif isinstance(data, dict) and isinstance(data.get("tool_uses"), list):
            # DSML / 部分网页版形态：键名是 tool_uses
            entries = data["tool_uses"]
        elif isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            if not allow_bare_object:
                return
            entries = [data]
        else:
            return
        for entry in entries:
            normalized = _normalize_tool_entry(entry)
            if normalized:
                calls.append(normalized)

    # 0. 兼容分支（历史载体）：行首 TOOL_CALL: 后跟一个平衡 JSON 对象。
    #
    #    契约：**每个 TOOL_CALL: 标记只取其后第一个平衡 JSON 对象；一行一调用；
    #    多个调用必须写成多行**。同行第二个对象会被丢弃（这是有意为之——
    #    避免把标记之后的无关 {..} 当成调用吞进来）。
    #
    #    segment 必须截到**下一个 TOOL_CALL 标记之前**：否则某个标记后面若没跟
    #    对象（模型写了标记又改主意），它会把下一个标记的对象当成自己的消费掉，
    #    轮到下一个标记时又消费同一个对象 → 同一次调用重复出现两次。
    matches = list(_TOOL_CALL_LINE_RE.finditer(text))
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[match.end():next_start]
        objs = list(_iter_balanced_objects(segment))
        if not objs:
            # 平衡扫描失败：多半是值边界多/少了一个引号，导致字符串状态错乱、
            # depth 回不到 0。先用边界引号归一化再扫一遍。
            objs = list(_iter_balanced_objects(_strip_redundant_value_quotes(segment)))
        if not objs:
            # 值内出现裸 `{`（如 markdown 里的 {"a":1}）会让字符串状态提前错乱，
            # 平衡扫描切不出完整对象。改用锚点式 salvage：按 name/arguments/首个
            # 键定位，一直取到对象收尾，绕开括号配对。
            salvaged = _salvage_string_args(segment)
            if salvaged is None:
                # 渲染改写过文本时，DOM 里经常只剩一个收尾 `}`（见该函数 docstring）。
                salvaged = _salvage_missing_final_brace(segment)
            if salvaged is not None:
                calls.append(salvaged)
                continue
        for obj in objs:
            _consume(obj, allow_bare_object=True)
            break

    if not calls:
        for match in _TOOL_CALL_FENCE_RE.finditer(text):
            _consume(match.group(2), allow_bare_object=True)

    if not calls:
        # DSML 风格 XML 包裹的工具调用（网页版偶发输出）
        for match in _DSML_TOOL_RE.finditer(text):
            _consume(match.group(1), allow_bare_object=True)

    if not calls:
        calls.extend(_parse_dsml_invokes(text, valid_names))

    if not calls:
        for match in _JSON_FENCE_RE.finditer(text):
            _consume(match.group(1), allow_bare_object=True)

    if not calls and _TOOL_USES_RE.search(text):
        # 标签丢失、只剩 {"tool_uses": [...]} 的裸对象
        for obj in _iter_balanced_objects(text):
            _consume(obj, allow_bare_object=True)
            if calls:
                break

    if not calls:
        # 兜底：无围栏的 "tool_call" 标签 + 平衡 JSON 对象（网页 DOM 提取后的形态）。
        # ChatGPT 把 ``` 围栏渲染成 <pre> 后 inner_text 常退化成：
        #   > tool_call            （markdown 引用/渲染残留）
        #   tool_call\n{...}
        #   ｜｜tool_call｜｜\n{...}
        # 因此 marker 与 JSON 之间可能夹着 > 、竖线、空白等噪声，需要跳过它们再找 JSON。
        # 不能用 \b：中文/全角字符（如 丨 ｜）在 Python re 里算 \w，
        # 会让 "tool_call丨" 这种边界匹配失败。改用「后面不是 ASCII 标识符字符」判定。
        marker_re = re.compile(r"tool[-_]?call(?![A-Za-z0-9_])", re.IGNORECASE)
        pos = 0
        while True:
            marker_match = marker_re.search(text, pos)
            if not marker_match:
                break
            segment = text[marker_match.end():]
            # marker 与 JSON 之间可能夹着任意噪声：`">`、`>`、竖线、`` ` ``、
            # "Copy"/"Download" 渲染文字、空白换行……不要逐种枚举，
            # 直接跳到第一个 `{`，从那里起用平衡扫描找 JSON 对象。
            brace = segment.find("{")
            if brace < 0:
                pos = marker_match.end()
                continue
            probe = segment[brace:]
            parsed = False
            for obj in _iter_balanced_objects(probe):
                _consume(obj, allow_bare_object=True)
                pos = marker_match.end() + brace + probe.index(obj) + len(obj)
                parsed = True
                break
            if not parsed:
                pos = marker_match.end()

    # 护栏：若传入了 valid_names，则过滤掉不在其中的幻觉工具名；否则保留全部解析出的工具调用。
    if valid_names is not None:
        calls = [c for c in calls if c.get("name") in valid_names]

    # 护栏：shell 类命令若双引号不配对，几乎必然是解析阶段把值截断/丢尾引号。
    # 这类命令发给 shell 只会得到 `unexpected EOF`，宁可在桥接层丢弃，
    # 让模型下一轮重新输出完整命令。
    calls = [c for c in calls if _call_args_sane(c)]

    # 诊断：模型**确实想调用工具**（写了 TOOL_CALL 标记 / 围栏），但我们一个可用的
    # 调用都没能交出去。以前这里是静默的：客户端只收到纯文本、把回复当最终答案，
    # 任务就悄悄结束了，日志里没有任何线索（2026-10-06 真机回归，见 update.md §2.13）。
    if not calls and (matches or _TOOL_CALL_FENCE_RE.search(text)):
        logger.warning(
            "回复里有 %d 个 TOOL_CALL 标记，但没解析出任何可用调用，整条调用已丢弃"
            "（客户端只会收到纯文本，可能把回复当成最终答案并结束任务）。"
            "常见原因：网页渲染吃掉了反斜杠转义 / 折叠了连续空格（update.md §2.13）、"
            "工具名不在客户端 tools 里、或参数引号不配对。",
            len(matches),
        )

    return calls


def _call_args_sane(call: Dict[str, Any]) -> bool:
    """对 shell 类调用做最低限度健全性检查（当前：命令引号配对）。"""
    name = (call.get("name") or "").lower()
    if not any(k in name for k in _SHELL_NAME_KEYWORDS):
        return True
    args = call.get("arguments")
    if not isinstance(args, dict):
        return True
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and not _shell_quotes_balanced(value):
            return False
    return True


def to_tool_call_models(calls: List[Dict[str, Any]]) -> List[ToolCall]:
    return [
        ToolCall(
            function=FunctionCall(
                name=call["name"],
                arguments=json.dumps(call["arguments"], ensure_ascii=False),
            )
        )
        for call in calls
    ]
