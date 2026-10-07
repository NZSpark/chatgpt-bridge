"""工具调用解析：从模型回复文本还原为结构化调用。

本模块是 ``chatgpt_web.toolcalls`` 的解析子模块（PI-901-2），承载：

* 代码围栏 / 历史纯文本标记 / DSML XML / shell 围栏等**多载体解析**；
* 非法 JSON 的确定性修复（引号、控制字符、锚点式 salvage）；
* 统一的 :class:`ToolCallRequest` 类型化边界；
* 工具名 / 参数键的规范化与过滤。

对外的兼容入口仍是 ``chatgpt_web.toolcalls``（facade），历史 import 路径不变。
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .. import config
from ..metrics import metrics
from ..models import FunctionCall, ToolCall

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """Normalized internal representation of one model-emitted tool call.

    ``source_span`` is populated only when a parser can identify an exact source
    range without guessing; malformed/repaired DOM output therefore uses ``None``.
    ``raw_text`` is retained solely for diagnostics and must not be consumed by
    execution code.
    """

    id: str
    name: str
    arguments: Dict[str, Any]
    source_span: Optional[tuple[int, int]] = None
    raw_text: str = ""

    @classmethod
    def from_mapping(
        cls,
        call: Dict[str, Any],
        *,
        raw_text: str = "",
        source_span: Optional[tuple[int, int]] = None,
    ) -> "ToolCallRequest":
        if not isinstance(call, dict):
            raise TypeError("tool call must be a mapping")
        name = call.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool call name is required")
        arguments = call.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("tool call arguments must be an object")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_{uuid.uuid4().hex[:16]}"
        return cls(
            id=call_id,
            name=name,
            arguments=dict(arguments),
            source_span=source_span,
            raw_text=raw_text,
        )

    def as_mapping(self) -> Dict[str, Any]:
        """Return the legacy dict shape used by the compatibility facade."""
        return {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }


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

# ==================== shell 围栏修复（2026-10-07 用户实测） ====================
# 现象：模型不写 tool_call JSON，而是直接回一个 ```` ```bash ```` / ```` ```sh ```` 代码块，
# 内容就是裸命令（用户报的原例：`git status --short`）。旧解析链只认 tool_call 系列载体，
# 于是整条回复被当成纯文本、任务静默结束。
#
# 恢复成调用有严格前提（任一不满足就不解析——宁可让模型下一轮重出，也不要执行一条
# 猜出来的命令）：
#   * 标签是 shell 类标签（见 _SHELL_FENCE_LABELS）；
#   * 全篇只有**一个**这样的围栏（一份回复只跑一条命令）；
#   * 能**唯一**对应到客户端工具集里的某个 shell 类工具（复用 DSML 的名字映射）；
#   * 该工具声明了明确的命令参数键（command / cmd / ...），或全局只有一个参数键；
#   * 回复里没有任何 tool_call 尝试（否则那是格式错误，按格式错误处理，不去跑近似命令）。
_SHELL_FENCE_LABELS = frozenset({
    "bash", "sh", "shell", "zsh", "fish", "cmd", "powershell", "ps1", "console", "terminal",
})
# 通用围栏提取（标签 → 内容）；是否采纳由 _SHELL_FENCE_LABELS 决定。
_SHELL_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+.-]+)[ \t]*\n(.*?)```", re.DOTALL)
# 命令参数键的优先顺序：声明里命中哪个就把命令写进哪个。
_COMMAND_KEYS = ("command", "cmd", "script", "input", "code", "shell", "cmdline")
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


def tool_parameter_names(tools: Optional[List[Dict[str, Any]]]) -> Dict[str, List[str]]:
    """从 OpenAI tools 描述里收集「工具名 → 声明的参数键（按声明顺序）」。

    供 shell 围栏修复使用：把 ```` ```bash ```` 块恢复成调用时必须知道命令该写进哪个
    参数（`command`？`cmd`？），否则只能猜键名——猜错等于发出一条参数非法、
    必然执行失败的调用。
    """
    out: Dict[str, List[str]] = {}
    for tool in tools or []:
        fn = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = fn.get("name") if isinstance(fn, dict) else None
        if not name:
            continue
        params = fn.get("parameters") if isinstance(fn, dict) else None
        params = params if isinstance(params, dict) else {}
        props = params.get("properties")
        keys = [str(k) for k in props] if isinstance(props, dict) else []
        if not keys:
            raw_required = params.get("required")
            if isinstance(raw_required, list):
                keys = [str(k) for k in raw_required if isinstance(k, str)]
        out.setdefault(str(name), keys)
    return out


def parse_reply_tool_calls(
    text: str, tools: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """``parse_tool_calls`` 的「带完整 tools 描述」入口（server / streaming / responses 共用）。

    一次提供两样东西：名字过滤（防幻觉工具名）+ 参数键表（shell 围栏修复需要）。
    必须与 :func:`tool_call_predicate` 用同一条路径，否则会出现「纠偏判定说不合格、
    外面却解析出了 tool_calls」的不一致。
    """
    return parse_tool_calls(
        text, _tool_names(tools), tool_parameter_names(tools)
    )


def _dedent_command(body: str) -> str:
    """去掉命令块的首尾空行，并消除「整块被缩进」的共同前导空白。

    放在列表 / 引用里的围栏会被整体缩进几格，而 shell 对缩进敏感情形（heredoc、
    续行）会因此变形；只在所有非空行都有**相同**前导空白时才统一左移。
    """
    lines = body.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    indents = [len(line) - len(line.lstrip(" \t")) for line in lines if line.strip()]
    if indents and min(indents) > 0:
        cut = min(indents)
        lines = [(line[cut:] if line.strip() else "") for line in lines]
    return "\n".join(lines)


def _shell_command_key(
    name: str, parameter_names: Optional[Mapping[str, Sequence[str]]]
) -> Optional[str]:
    """该工具的哪个参数键用来装命令；无法确定时返回 None（不猜）。"""
    keys = list((parameter_names or {}).get(name) or [])
    if not keys:
        return None
    for preferred in _COMMAND_KEYS:
        if preferred in keys:
            return preferred
    # 只有一个参数键时才敢用（多键时无法判断哪个装命令）
    return keys[0] if len(keys) == 1 else None


def _shell_fence_calls(
    text: str,
    valid_names: Optional[set],
    parameter_names: Optional[Mapping[str, Sequence[str]]],
) -> List[Dict[str, Any]]:
    """把 ```` ```bash ```` / ```` ```sh ```` 代码块恢复成一条工具调用（前提见 _SHELL_FENCE_LABELS）。

    :return: 0 或 1 条调用；任何前提不满足都返回空列表（保持旧行为：不解析）。
    """
    if not valid_names:
        return []
    # 回复里已经有 tool_call 尝试（围栏或历史标记）时不走修复：那是格式错误，
    # 应该按格式错误处理（下一轮重出），而不是去跑一个“近似”命令。
    if _TOOL_CALL_FENCE_RE.search(text) or _TOOL_CALL_LINE_RE.search(text):
        return []
    fences = [
        (match.group(1).strip().lower(), match.group(2))
        for match in _SHELL_FENCE_RE.finditer(text)
        if match.group(1).strip().lower() in _SHELL_FENCE_LABELS
    ]
    if len(fences) != 1:
        return []
    label, body = fences[0]
    command = _dedent_command(body)
    if not command.strip():
        return []
    name = _resolve_dsml_name(label, valid_names)
    if name not in valid_names:
        return []
    # 形态 A：围栏里其实是调用 JSON（标签写错、内容对）——直接按调用收下。
    if command.lstrip().startswith("{"):
        try:
            data = json.loads(command)
        except Exception:
            data = None
        if isinstance(data, dict):
            normalized = _normalize_tool_entry(data)
            if normalized and normalized.get("name") in valid_names:
                return [normalized]
    # 形态 B：围栏里是裸命令——写进该工具声明的命令参数。
    key = _shell_command_key(name, parameter_names)
    if key is None:
        return []
    logger.warning(
        "shell 围栏修复：模型用 ```%s 代码块代替了 tool_call JSON，"
        "已按唯一匹配的工具 %s(%s) 恢复调用（SHELL_FENCE_FALLBACK=false 可关闭）",
        label,
        name,
        key,
    )
    return [{"name": name, "arguments": {key: command}}]


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


def parse_tool_calls(
    text: str,
    valid_names: Optional[set] = None,
    parameter_names: Optional[Mapping[str, Sequence[str]]] = None,
) -> List[Dict[str, Any]]:
    """从模型回复中解析出工具调用列表。返回 [{"name": ..., "arguments": {...}}, ...]

    需要兼容多种形态：
      0. **当前注入格式**：带围栏的 ```tool_call ... ```代码块（围栏内一条 JSON）；
      1. 行首 ``TOOL_CALL: {...}`` 纯文本标记——**历史载体**，仍兼容；
         网页版会把这行当 markdown 渲染并改坏它，见 doc/code_block_fence.md；
      2. **无围栏**的 ``tool_call`` 标签 + JSON 对象——这是从 ChatGPT 网页 DOM
         提取 inner_text 后的常见形态：代码块被渲染成 <pre>，围栏退化为标题文字，
         于是只剩 ``tool_call`` 标签与裸 JSON（当前注入格式被渲染后的实际形态）；
      3. ```` ```bash ```` / ```` ```sh ```` 代码块（内容为裸命令）——模型写错载体的
         修复路径，需要 ``parameter_names`` 才能确定命令参数键，见
         :func:`_shell_fence_calls`。

    :param valid_names: 客户端声明的合法工具名（过滤幻觉；为空时不启用 shell 围栏修复）。
    :param parameter_names: ``{工具名: [参数键, ...]}``，来自 :func:`tool_parameter_names`；
        只用于 shell 围栏修复（确定命令写进哪个参数），缺省时该修复不生效。
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

    if not calls and config.SHELL_FENCE_FALLBACK:
        # 最后一道修复：模型写错载体（```bash + 裸命令，2026-10-07 用户实测）。放在
        # 所有正规载体分支**之后**——只有前面的分支一条都没解析出来时才尝试，
        # 避免把一个“近似命令”抢在真正要跑的调用前面执行。
        calls.extend(_shell_fence_calls(text, valid_names, parameter_names))

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
        metrics.inc("tool_parse_failure_total")
        logger.warning(
            "回复里有 %d 个 TOOL_CALL 标记，但没解析出任何可用调用，整条调用已丢弃"
            "（客户端只会收到纯文本，可能把回复当成最终答案并结束任务）。"
            "常见原因：网页渲染吃掉了反斜杠转义 / 折叠了连续空格（update.md §2.13）、"
            "工具名不在客户端 tools 里、或参数引号不配对。",
            len(matches),
        )

    return calls


def parse_tool_call_requests(
    text: str, valid_names: Optional[set] = None
) -> List[ToolCallRequest]:
    """Parse model output into the typed internal tool-call representation.

    ``parse_tool_calls`` remains the compatibility facade returning dictionaries;
    new execution code should consume this typed boundary instead of raw text.
    """
    calls = parse_tool_calls(text, valid_names)
    requests: List[ToolCallRequest] = []
    for call in calls:
        try:
            requests.append(ToolCallRequest.from_mapping(call))
        except (TypeError, ValueError) as exc:
            logger.warning("忽略无效工具调用：%r", exc)
    return requests


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


def to_tool_call_models(
    calls: List[Dict[str, Any] | ToolCallRequest],
) -> List[ToolCall]:
    return [
        ToolCall(
            id=call.id if isinstance(call, ToolCallRequest) else call.get("id") or f"call_{uuid.uuid4().hex[:16]}",
            function=FunctionCall(
                name=call.name if isinstance(call, ToolCallRequest) else call["name"],
                arguments=json.dumps(
                    call.arguments if isinstance(call, ToolCallRequest) else call["arguments"],
                    ensure_ascii=False,
                ),
            ),
        )
        for call in calls
    ]
