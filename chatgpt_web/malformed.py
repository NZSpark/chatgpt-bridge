"""坏结构检测：网页端把 tool_call 生成坏时的判据。

判据（参考 DeepseekBridge/deepseek_web/malformed.py 的实测结论）：出现了 tool_call
标记，但包裹的内容不是合法 JSON 调用 -> 判为损坏，需要让模型**重新发一次调用命令**。

合法 = 那段内容能被 ``json.loads`` 解析成 JSON 对象，且含非空字符串 ``name``
与对象型 ``arguments``。

与 :func:`chatgpt_web.tools.parser.has_tool_call_syntax_error` 的分工：

* 本模块是**载体级**判据：围栏没闭合、围栏是空的……这些情况下连「一段可解析的
  JSON」都拿不到；
* ``has_tool_call_syntax_error`` 是**语法级**判据：拿得到片段，但 ``json.loads``
  报的是语法错（漏逗号、字符串没闭合、换行没转义）。

两者任一成立，就应当要求模型严格按 JSON 重发，而不是把这段坏调用当最终答案。
"""
from __future__ import annotations

import json
import re

# 显式围栏：开围栏到闭合。body 可以为空（损坏形态）；未闭合以 \\Z 兜底。
_FENCED = re.compile(r"```[ \t]*(tool[-_]call)[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL | re.IGNORECASE)

# 只匹配开围栏本身，用来判断是否闭合。语言标记后不要求必须换行（同行/CRLF 都要能命中）。
TOOL_CALL_OPEN = re.compile(r"```[ \t]*(tool[-_]call)\b(?![0-9A-Za-z_])", re.IGNORECASE)

# 裸标记：只在行首/空白后出现才算。
BARE = re.compile(
    r"^[ \t]*tool[-_]?call\b(?![0-9A-Za-z_])[ \t]*[:：]?[ \t]*",
    re.MULTILINE | re.IGNORECASE,
)

# 重新调用提示词：用户实测有效的固定措辞（与 DeepseekBridge 对齐，勿随意改写）。
RETRY_PROMPT = (
    "调用失败，重新调用。需要调用工具时，只输出一个如下格式的代码块（arguments 必须是合法 JSON）：\\n"
    '```tool_call\\n"{"name": "工具名", "arguments": {参数对象}}"\\n```\\n'
    "一次只发一条指令，不要有多余解释的话或标记、标号。代码块要用```完成闭合。"
)


def has_unclosed_tool_fence(text: str) -> bool:
    """开围栏之后找不到闭合 ``` —— 网页端把输出截断时的典型形态。"""
    if not text:
        return False
    pos = 0
    while True:
        match = TOOL_CALL_OPEN.search(text, pos)
        if not match:
            return False
        close = text.find("```", match.end())
        if close < 0:
            return True
        pos = close + 3


def _loads_lenient(text: str):
    """先原样解析；失败再走解析层的确定性修复（引号/控制字符）。"""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    try:
        from .tools.parser import _repair_json_quotes

        fixed = _repair_json_quotes(text)
    except Exception:  # noqa: BLE001 解析层缺席/异常时退回「只认原样」
        return None
    return fixed


def _is_valid_call(body: str) -> bool:
    """这段围栏内容是否是一个结构完整的工具调用对象。"""
    text = (body or "").strip()
    if not text:
        return False
    obj = _loads_lenient(text)
    if not isinstance(obj, dict):
        return False
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return False
    return isinstance(obj.get("arguments"), dict)


def _bare_bodies(text: str):
    """裸标记之后、到下一个裸标记（或文末）之间的内容。

    body 允许跨行：``tool_call`` 独占一行、JSON 跟在下一行是最常见的形态，
    旧写法要求同行取 body，于是这种形态根本匹配不到。
    """
    matches = list(BARE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield text[match.end():end]


def _iter_bodies(text: str):
    fenced = False
    for match in _FENCED.finditer(text):
        fenced = True
        yield match.group(2)
    if fenced:
        return
    for body in _bare_bodies(text):
        yield body


def has_malformed_tool_call(text: str) -> bool:
    """回复里是否出现了「声称要调用工具、但结构坏掉」的内容。

    返回 True 意味着应当要求模型重新发一次调用命令（见 :data:`RETRY_PROMPT`）。
    刻意不看普通 python/bash/json 等其它语言围栏。
    """
    if not text:
        return False
    if has_unclosed_tool_fence(text):
        return True
    if any(not _is_valid_call(m.group(2)) for m in _FENCED.finditer(text)):
        return True
    if _FENCED.search(text):
        return False
    return any(not _is_valid_call(body) for body in _bare_bodies(text))


def has_no_usable_call(text: str, valid_names=None) -> bool:
    """出现了 tool_call 标记、却拿不到任何可用调用（含工具名不在列表里的情况）。

    与 :func:`has_malformed_tool_call` 的差别：这里允许解析层修出调用，只在
    「一个都交不出去」时返回 True——用于决定是否需要「重新调用」纠偏。
    """
    if not text:
        return False
    try:
        from .toolcalls import parse_tool_calls

        if parse_tool_calls(text, valid_names):
            return False
    except Exception:  # noqa: BLE001, S110 解析层缺席/异常不影响判据本身
        pass
    if has_unclosed_tool_fence(text):
        return True
    saw_marker = False
    for body in _iter_bodies(text):
        saw_marker = True
        if _is_valid_call(body):
            return False
    # 一个标记都没有 = 模型根本没提工具，不算「有调用但不可用」。
    return saw_marker
