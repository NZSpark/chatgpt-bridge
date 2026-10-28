"""工具调用 JSON 格式错误 -> 要求模型重新发一次调用命令。

用户报的原例：模型发出的 ``tool_call`` 里，shell 命令带真实换行（JSON 字符串未转义），
``json.loads`` 报 ``Invalid control character``。旧行为是把回复当纯文本返回，客户端
（Pi/Codex）误以为任务完成；现在必须判为「不可接受」，触发一次纠偏重发，
并明确提醒模型严格遵循 JSON 格式。
"""
from __future__ import annotations

import json

from chatgpt_web.malformed import (
    RETRY_PROMPT,
    has_malformed_tool_call,
    has_no_usable_call,
    has_unclosed_tool_fence,
)
from chatgpt_web.toolcalls import (
    format_tool_json_format_nudge,
    tool_call_predicate,
)
from chatgpt_web.tools.parser import has_tool_call_syntax_error

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}

# 用户原例的形态：裸 tool_call 标记 + JSON，command 字符串里夹着真实换行。
USER_REPORTED = (
    "tool_call\n"
    ' {"name":"bash","arguments":{"command":"pwd; ls -la; printf \'\\n--- MainActivity ---\\n\'; '
    "sed -n '1,260p'\n"
    ' app/src/main/java/nz/org/aotearoa/guandanscore/MainActivity.kt"}}'
)


def test_user_reported_payload_has_json_syntax_error():
    assert has_tool_call_syntax_error(USER_REPORTED) is True


def test_predicate_rejects_json_syntax_error():
    predicate = tool_call_predicate([BASH_TOOL])
    assert predicate(USER_REPORTED) is False


def test_predicate_accepts_valid_call():
    predicate = tool_call_predicate([BASH_TOOL])
    good = "```tool_call\n" + json.dumps(
        {"name": "bash", "arguments": {"command": "pwd"}}
    ) + "\n```"
    assert predicate(good) is True


def test_unclosed_fence_is_malformed():
    text = '```tool_call\n{"name":"bash","arguments":{"command":"pwd"}}'
    assert has_unclosed_tool_fence(text) is True
    assert has_malformed_tool_call(text) is True
    assert tool_call_predicate([BASH_TOOL])(text) is False


def test_empty_fence_is_malformed():
    text = "```tool_call\n```"
    assert has_malformed_tool_call(text) is True
    assert tool_call_predicate([BASH_TOOL])(text) is False


def test_plain_text_is_not_malformed():
    # 纯文本收尾不该被当成坏调用（否则任务永远收不了尾）。
    assert has_malformed_tool_call("全部完成，没有需要再执行的了。") is False
    assert tool_call_predicate([BASH_TOOL])("全部完成。") is False


def test_no_usable_call_detects_broken_marker():
    assert has_no_usable_call("```tool_call\n```") is True
    assert has_no_usable_call("完全没提工具") is False


def test_retry_prompt_mentions_strict_json_and_refence():
    prompt = format_tool_json_format_nudge()
    assert prompt == RETRY_PROMPT
    # 必须点名「重新调用」和 JSON 合法性，且给出代码块形态。
    assert "重新调用" in prompt
    assert "合法 JSON" in prompt
    assert "```tool_call" in prompt
