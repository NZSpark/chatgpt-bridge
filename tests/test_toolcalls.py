"""工具调用注入与解析的回归测试。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web.toolcalls import (  # noqa: E402
    ToolCallRequest,
    ToolCallExecutionError,
    ToolCallParseError,
    ToolCallPolicyError,
    ToolCallSerializationError,
    ToolCallValidationError,
    _normalize_tool_entry,
    _tool_names,
    check_tool_call_policy,
    deduplicate_tool_call_requests,
    execute_tool_call_requests,
    format_tools_instruction,
    normalize_tool_call_requests,
    parse_tool_call_requests,
    parse_tool_calls,
    run_tool_call_pipeline,
    serialize_tool_call_results,
    to_tool_call_models,
    validate_tool_call_requests,
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


class FormatInstructionTests(unittest.TestCase):
    def test_contains_name_and_description(self):
        text = format_tools_instruction(TOOLS)
        self.assertIn("get_weather", text)
        self.assertIn("查询天气", text)

    def test_contains_call_template(self):
        text = format_tools_instruction(TOOLS)
        self.assertIn("TOOL_CALL", text.upper())

    def test_template_is_stable(self):
        self.assertEqual(
            format_tools_instruction(TOOLS), format_tools_instruction(TOOLS)
        )

    def test_empty_tools(self):
        self.assertIsInstance(format_tools_instruction([]), str)


class ToolNamesTests(unittest.TestCase):
    def test_collects_names(self):
        self.assertEqual(_tool_names(TOOLS), {"get_weather"})

    def test_none_and_empty(self):
        self.assertEqual(_tool_names(None), set())
        self.assertEqual(_tool_names([]), set())


class NormalizeEntryTests(unittest.TestCase):
    def test_non_dict_returns_none(self):
        self.assertIsNone(_normalize_tool_entry("nope"))
        self.assertIsNone(_normalize_tool_entry(123))

    def test_missing_name_returns_none(self):
        self.assertIsNone(_normalize_tool_entry({"arguments": {}}))

    def test_flat_entry(self):
        out = _normalize_tool_entry({"name": "f", "arguments": {"a": 1}})
        self.assertEqual(out["name"], "f")
        self.assertEqual(out["arguments"], {"a": 1})

    def test_nested_function_key(self):
        entry = {"function": {"name": "f", "arguments": {"a": 1}}}
        out = _normalize_tool_entry(entry)
        self.assertEqual(out["name"], "f")
        self.assertEqual(out["arguments"], {"a": 1})

    def test_string_arguments_decoded(self):
        out = _normalize_tool_entry({"name": "f", "arguments": '{"a": 1}'})
        self.assertEqual(out["arguments"], {"a": 1})

    def test_multiline_command_with_inner_quotes(self):
        # Real newlines plus unescaped inner quotes inside a JSON string value.
        raw_command = "\n".join([
            "python3 -c '",
            "import os, glob",
            'files = [y for x in os.walk(".") for y in glob.glob(os.path.join(x[0], "*"))]',
            "print('done')",
            "'",
        ])
        text = 'TOOL_CALL: {"name": "bash", "arguments": {"command": "' + raw_command + '"}}'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        cmd = calls[0]["arguments"]["command"]
        self.assertIn('os.walk(".")', cmd)
        self.assertIn("glob.glob", cmd)


class ParseToolCallsTests(unittest.TestCase):
    def test_fenced_call(self):
        text = '```tool_call\n{"name": "get_weather", "arguments": {"city": "SF"}}\n```'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "get_weather")
        self.assertEqual(calls[0]["arguments"], {"city": "SF"})

    def test_multiple_fenced_calls(self):
        text = (
            '```tool_call\n{"name": "a", "arguments": {}}\n```\n'
            '```tool_call\n{"name": "b", "arguments": {}}\n```'
        )
        calls = parse_tool_calls(text)
        self.assertEqual([c["name"] for c in calls], ["a", "b"])

    def test_line_marker(self):
        text = 'TOOL_CALL: {"name": "get_weather", "arguments": {"city": "NY"}}'
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["name"], "get_weather")

    def test_tool_calls_wrapper_key(self):
        payload = {"tool_calls": [{"name": "a", "arguments": {"x": 1}}]}
        text = "```tool_call\n" + json.dumps(payload) + "\n```"
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["name"], "a")

    def test_tool_uses_wrapper_key(self):
        payload = {"tool_uses": [{"name": "a", "arguments": {}}]}
        text = "```tool_call\n" + json.dumps(payload) + "\n```"
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["name"], "a")

    def test_invalid_json_is_ignored(self):
        text = "```tool_call\n{not json}\n```"
        self.assertEqual(parse_tool_calls(text), [])

    def test_plain_text_returns_empty(self):
        self.assertEqual(parse_tool_calls("just a normal answer"), [])

    def test_empty_returns_empty(self):
        self.assertEqual(parse_tool_calls(""), [])

    def test_string_arguments_decoded(self):
        text = '```tool_call\n{"name": "a", "arguments": "{\\"x\\": 1}"}\n```'
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["arguments"], {"x": 1})

    def test_valid_names_filter_drops_hallucinations(self):
        text = '```tool_call\n{"name": "ghost", "arguments": {}}\n```'
        self.assertEqual(parse_tool_calls(text, {"real"}), [])

    def test_valid_names_filter_keeps_known(self):
        text = '```tool_call\n{"name": "real", "arguments": {}}\n```'
        self.assertEqual(len(parse_tool_calls(text, {"real"})), 1)

    def test_parameters_key_alias(self):
        text = '```tool_call\n{"name": "a", "parameters": {"y": 2}}\n```'
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["arguments"], {"y": 2})

    def test_unescaped_inner_quotes_repaired(self):
        # 模型把 shell 命令里的引号原样写进 JSON 字符串（未转义），
        # 标准 json.loads 会失败；解析器应尽力修复并保留引号原意。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "git commit -m "Update logic" && git push"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git commit -m "Update logic" && git push',
        )

    def test_unescaped_inner_quotes_repaired_full_command(self):
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": '
            '"git add a.py b.py && git commit -m "msg here" && git push"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["name"], "exec_command")
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git add a.py b.py && git commit -m "msg here" && git push',
        )

    def test_wellformed_json_still_parses(self):
        # 修复逻辑只在解析失败时触发，合法输入不受影响。
        text = 'TOOL_CALL: {"name": "a", "arguments": {"cmd": "echo \\"hi\\""}}'
        calls = parse_tool_calls(text)
        self.assertEqual(calls[0]["arguments"]["cmd"], 'echo "hi"')

    def test_markdown_escaped_marker_recovered(self):
        # ChatGPT 网页版 markdown 渲染会插入反斜杠：TOOL\_CALL / exec\_command
        text = 'TOOL\\_CALL: {"name": "exec\\_command", "arguments": {"cmd": "mkdir -p doc"}}'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "exec_command")
        self.assertEqual(calls[0]["arguments"]["cmd"], "mkdir -p doc")

    def test_properly_escaped_inner_quotes(self):
        # 注入指令要求模型把值内双引号转义为 \" ；这是首选、合法的形态。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "python -c \\"import os\\""}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], 'python -c "import os"')

    def test_redundant_boundary_quotes_stripped(self):
        # 模型给值又包了一层引号："cmd": ""git status""；应还原为无多余引号。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": ""git status && git log -n 5 --oneline""}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git status && git log -n 5 --oneline',
        )

    def test_single_stray_open_quote_recovered(self):
        # 值开头多一个引号（平衡扫描会失败）："cmd": ""git status"
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": ""git status && git log -n 5 --oneline"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git status && git log -n 5 --oneline',
        )

    def test_single_stray_close_quote_recovered(self):
        # 值结尾多一个引号："cmd": "git status""
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "git status && git log -n 5 --oneline""}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["cmd"],
            'git status && git log -n 5 --oneline',
        )

    def test_body_quote_before_bracket_kept(self):
        # ChatGPT 网页渲染会把 \" 消耗掉，DOM 取回后值里是裸引号：
        # [contenteditable="true"]。true 后面的引号紧跟 ]，再下一个是正文字符，
        # 不能被当成字符串结束，否则字符串提前闭合、整条调用被丢弃。
        text = (
            'TOOL_CALL: {"name": "edit", "arguments": {"edits": [{"oldText": '
            '"- `READY_SELECTOR`：`textarea, [contenteditable="true"]`，判定可输入。"}], '
            '"path": "doc/update.md"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["arguments"]["edits"][0]["oldText"],
            '- `READY_SELECTOR`：`textarea, [contenteditable="true"]`，判定可输入。',
        )
        self.assertEqual(calls[0]["arguments"]["path"], "doc/update.md")

    def test_body_quote_with_raw_newlines_repaired(self):
        # 裸引号 + 真实换行（多行 Markdown 未转义）叠加：仍应完整还原。
        old = '- `A`：x\n- `READY_SELECTOR`：`textarea, [contenteditable="true"]`，判定。\n- `B`：y'
        text = (
            'TOOL_CALL: {"name": "edit", "arguments": {"edits": [{"oldText": "'
            + old +
            '"}]}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["edits"][0]["oldText"], old)


class ControlCharRepairTests(unittest.TestCase):
    """多行命令（heredoc）里的真实换行：JSON 字符串内裸控制字符需转义。"""

    def test_heredoc_newlines_repaired(self):
        # 模型把多行命令的真实换行直接写进 JSON 字符串值。
        # 手工拼一个"含裸换行"的 JSON（不走 json.dumps，否则会转义掉换行）。
        cmd = "cat << 'EOF' > x.py\nprint('hi')\nEOF\n"
        raw = '{"name": "exec_command", "arguments": {"cmd": "' + cmd + '"}}'
        calls = parse_tool_calls("TOOL_CALL: " + raw, {"exec_command"})
        self.assertEqual(len(calls), 1)
        got = calls[0]["arguments"]["cmd"]
        self.assertIn("cat << 'EOF' > x.py\n", got)
        self.assertIn("print('hi')\n", got)

    def test_tab_control_char_repaired(self):
        text = 'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "echo\thi"}}'
        calls = parse_tool_calls(text, {"exec_command"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "echo\thi")

    def test_wellformed_multiline_still_parses(self):
        # 已正确转义 \n 的合法输入不受影响。
        text = 'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "a\\nb"}}'
        calls = parse_tool_calls(text, {"exec_command"})
        self.assertEqual(calls[0]["arguments"]["cmd"], "a\nb")


class ShellGuardTests(unittest.TestCase):
    """护栏：shell 类命令引号不配对时丢弃，避免把坏命令发给 bash。"""

    def test_unbalanced_double_quote_dropped(self):
        # 模型/解析把命令尾部闭引号弄丢："git commit -m "msg"
        # 引号数为奇数 → 丢弃（否则 bash 报 unexpected EOF）。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "git commit -m "msg"}}'
        )
        self.assertEqual(parse_tool_calls(text), [])

    def test_balanced_double_quotes_kept(self):
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            '{"cmd": "git commit -m "msg here" && git push"}}'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "exec_command")

    def test_single_quoted_command_kept(self):
        # prompt 建议命令内部用单引号：这是首选形态，不应被护栏误伤。
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": '
            "{\"cmd\": \"git commit -m 'msg here'\"}}"
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "git commit -m 'msg here'")

    def test_non_shell_tool_not_guarded(self):
        # 非 shell 工具的字符串参数即使引号不配对也不受影响（不被护栏丢弃）。
        text = 'TOOL_CALL: {"name": "write_note", "arguments": {"text": "a \\" b"}}'
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["text"], 'a " b')


class ToolCallLineContractTests(unittest.TestCase):
    """首选形态 TOOL_CALL: 的“一行一调用”契约与重复消费回归（对照 README「Codex CLI 接入」）。"""

    def test_multi_line_multi_calls(self):
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "ls"}}\n'
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "pwd"}}\n'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [c["arguments"]["cmd"] for c in calls], ["ls", "pwd"]
        )

    def test_two_objects_same_line_second_dropped(self):
        text = (
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "ls"}} '
            '{"name": "exec_command", "arguments": {"cmd": "pwd"}}\n'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "ls")

    def test_marker_without_object_does_not_duplicate_next(self):
        # 第一个标记后没跟对象，只有解释文字；第二个标记才有对象。
        # 修复前：第一个标记会消费第二个标记的对象，导致同一调用出现两次。
        text = (
            "TOOL_CALL: 我改主意了，先说明一下\n"
            'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "ls"}}\n'
        )
        calls = parse_tool_calls(text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "ls")


class DomRenderDamageTests(unittest.TestCase):
    """ChatGPT 网页渲染改写 TOOL_CALL 行后的兜底解析（2026-10-06 真机回归，update.md §2.13）。

    线上现象：Pi 收到的是纯文本、把回复当成最终答案、任务**静默结束**，桥的日志里
    没有任何线索。根因是网页版把这条纯文本 TOOL_CALL 行当 markdown 渲染，渲染过程

      1. 吃掉一层反斜杠转义（值内引号前的反斜杠被吞掉 → JSON 不再合法）；
      2. 折叠连续空格（缩进 4 空格 -> 1 空格）→ 命令内容被改写；
      3. 取回的文本在值的闭引号后只剩**一个** ``}``（外层对象的收尾花括号丢失）。

    结果：括号不平衡 → 抽不出对象；``_salvage_string_args`` 又要求 ``endswith("}}")``
    → 直接放弃，整条调用被丢掉。这里锁住修复：少一个 ``}`` 时补上再 salvage。
    """

    # 下面这行是**真机抓到原文**（Pi 会话记录里桥实际返回的那条 assistant text），
    # 未做任何改写：值里是裸引号、末尾只有一个 }。
    RECEIVED = r'''
TOOL_CALL: {"name":"bash","arguments":{"command":"python3 -c 'from pathlib import Path; p=Path("chatgpt_web/session_store.py"); s=p.read_text(); old="_STATE_FILE_LOCK = threading.Lock()\n\n\n@dataclass"; new="_STATE_FILE_LOCK = threading.Lock()\n\nSESSION_STATE_SCHEMA_VERSION = 2\n\n\ndef _validate_v2_state(data: Dict[str, Any]) -> Dict[str, Any]:\n \"\"\"Validate the v2 session-state envelope.\"\"\"\n if not isinstance(data, dict):\n raise ValueError(\"session state root must be a JSON object\")\n sessions = data.get(\"sessions\")\n if sessions is not None and not isinstance(sessions, dict):\n raise ValueError(\"session state \'sessions\' must be an object\")\n if isinstance(sessions, dict):\n for key, payload in sessions.items():\n if not isinstance(key, str) or not isinstance(payload, dict):\n raise ValueError(\"session state contains an invalid bucket payload\")\n return data\n\n\ndef migrate_v2(data: Dict[str, Any]) -> Dict[str, Any]:\n \"\"\"Migrate the legacy session-state envelope to schema v2.\"\"\"\n if not isinstance(data, dict):\n raise ValueError(\"session state root must be a JSON object\")\n raw_version = data.get(\"schema_version\", data.get(\"version\", 1))\n if isinstance(raw_version, bool) or not isinstance(raw_version, int):\n raise ValueError(\"session state schema_version must be an integer\")\n if raw_version > SESSION_STATE_SCHEMA_VERSION:\n raise ValueError(\"unsupported session state schema_version=%s\" % raw_version)\n migrated = dict(data)\n migrated.pop(\"version\", None)\n migrated[\"schema_version\"] = SESSION_STATE_SCHEMA_VERSION\n return _validate_v2_state(migrated)\n\n\n@dataclass"; assert old in s; p.write_text(s.replace(old,new,1))'"}
'''

    def test_missing_outer_brace_recovers_real_received_text(self):
        calls = parse_tool_calls(self.RECEIVED, {"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        command = calls[0]["arguments"]["command"]
        # 渲染只吃掉了 JSON 转义，命令正文本身仍是模型写的原文（内容不被改写）。
        self.assertIn('Path("chatgpt_web/session_store.py")', command)
        self.assertIn('SESSION_STATE_SCHEMA_VERSION = 2', command)
        self.assertTrue(command.endswith("p.write_text(s.replace(old,new,1))'"))

    def test_synthetic_missing_outer_brace_recovers(self):
        # 只少外层收尾花括号：补一个 } 即应救回，而不是整条丢弃。
        text = 'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "echo hi"}'
        calls = parse_tool_calls(text, {"exec_command"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "echo hi")

    def test_truncated_value_is_not_fabricated(self):
        # 值本身被截断（连一个 } 都没有）时不得“猜”出调用：宁可丢弃，让模型重出。
        text = 'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "echo hi'
        self.assertEqual(parse_tool_calls(text, {"exec_command"}), [])

    def test_complete_object_still_not_rescued_by_brace_repair(self):
        # 合法输入不受影响：正常两个 } 走原路径，值原样保留。
        text = 'TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "echo hi"}}'
        calls = parse_tool_calls(text, {"exec_command"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["cmd"], "echo hi")



class FencedToolCallCarrierTests(unittest.TestCase):
    """工具调用的**载体**：代码围栏 ```tool_call（2026-10-06 真机 A/B 实测后从纯文本行切换）。

    为什么换载体（同一 payload、同一页面、同一模型，只改输出形态）：

    | 载体 | 值内引号前的反斜杠 | 4 空格缩进 | 能解析出调用 |
    | --- | --- | --- | --- |
    | 纯文本 `TOOL_CALL: {...}` 行 | 被吃掉（变裸引号 → JSON 失效） | 被折叠成 1 空格 | **否**（整条丢弃） |
    | ```tool_call 围栏 | 原样保留 | 原样保留 | 是，命令**逐字节一致** |

    原理：网页版把纯文本行当 markdown 渲染（转义消耗 + 空格折叠），代码块内部
    不做这层处理。围栏在 DOM 取回时只剩 info string（`tool_call`）单独一行，
    所以下面两条用例分别锁定「围栏被渲染掉」与「围栏还在」两种形态。
    """

    # 真机取回的原文（与网页渲染结果一致）：`tool_call` 标签 + 换行 + JSON。
    RECEIVED_RENDERED = r'''
tool_call
{"name":"bash","arguments":{"command":"printf \"hi\"; echo done\n    x = 1\n    y = 2"}}
'''

    q = chr(10)
    EXPECTED = 'printf "hi"; echo done' + q + '    x = 1' + q + '    y = 2'

    def test_rendered_fence_preserves_escapes_and_indentation(self):
        calls = parse_tool_calls(self.RECEIVED_RENDERED, {"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")
        command = calls[0]["arguments"]["command"]
        # 逐字节一致：JSON 转义按 JSON 语义解码后的真换行 + 4 空格缩进都必须在
        # （纯文本行会两者兼失：引号前的反斜杠被吃掉、缩进被折叠成 1 空格）。
        self.assertEqual(command, self.EXPECTED)
        self.assertIn(self.q + "    x = 1", command)
        self.assertIn('printf \"hi\"', command)

    def test_raw_fence_still_parses(self):
        # 围栏没被渲染掉时（例如客户端把回复原样贴回）同样认。
        text = '```tool_call\n{"name": "bash", "arguments": {"command": "echo hi"}}\n```'
        calls = parse_tool_calls(text, {"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["command"], "echo hi")

    def test_multiline_json_inside_fence_parses(self):
        # 代码块保留换行，所以围栏里 JSON 合法地跨行也必须能解析（JSON 允许 token 间换行）。
        text = (
            "tool_call\n"
            "{\n"
            '  "name": "bash",\n'
            '  "arguments": {\n'
            '    "command": "echo hi"\n'
            "  }\n"
            "}"
        )
        calls = parse_tool_calls(text, {"bash"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["arguments"]["command"], "echo hi")

    def test_rendered_json_label_is_not_taken_as_call(self):
        """负向约束：DOM 里只剩 `json` 标签行（围栏被渲染掉）时**不**当调用执行。

        这是刻意的：若接受任意标签，正文里展示的 JSON 片段只要带 name/arguments
        就会被误执行。因此载体固定为 `tool_call`（提示词里已明确要求）。
        """
        text = 'json\n{"name": "bash", "arguments": {"command": "echo hi"}}'
        self.assertEqual(parse_tool_calls(text, {"bash"}), [])



class ToolCallRequestTests(unittest.TestCase):
    def test_from_mapping_normalizes_and_generates_id(self):
        request = ToolCallRequest.from_mapping(
            {"name": "f", "arguments": {"a": 1}},
            raw_text='{"name":"f"}',
        )
        self.assertTrue(request.id.startswith("call_"))
        self.assertEqual(request.name, "f")
        self.assertEqual(request.arguments, {"a": 1})
        self.assertEqual(request.raw_text, '{"name":"f"}')
        self.assertEqual(request.source_span, None)

    def test_from_mapping_preserves_explicit_id_and_span(self):
        request = ToolCallRequest.from_mapping(
            {"id": "call_123", "name": "f", "arguments": {}},
            source_span=(10, 20),
        )
        self.assertEqual(request.id, "call_123")
        self.assertEqual(request.source_span, (10, 20))

    def test_from_mapping_rejects_non_object_arguments(self):
        with self.assertRaises(ValueError):
            ToolCallRequest.from_mapping({"name": "f", "arguments": "bad"})

    def test_as_mapping_keeps_legacy_shape(self):
        request = ToolCallRequest(
            id="call_123",
            name="f",
            arguments={"a": 1},
            source_span=(1, 2),
            raw_text="raw",
        )
        self.assertEqual(
            request.as_mapping(),
            {"id": "call_123", "name": "f", "arguments": {"a": 1}},
        )


class TypedParserTests(unittest.TestCase):
    def test_parse_tool_call_requests_returns_typed_objects(self):
        text = '```tool_call\n{"name": "get_weather", "arguments": {"city": "SF"}}\n```'
        requests = parse_tool_call_requests(text, {"get_weather"})
        self.assertEqual(len(requests), 1)
        self.assertIsInstance(requests[0], ToolCallRequest)
        self.assertEqual(requests[0].name, "get_weather")
        self.assertEqual(requests[0].arguments, {"city": "SF"})

    def test_parse_tool_call_requests_drops_invalid_entries(self):
        requests = parse_tool_call_requests(
            '```tool_call\n{"name": "real", "arguments": {}}\n```',
            {"real"},
        )
        self.assertEqual([request.name for request in requests], ["real"])


class ToolCallPipelineTests(unittest.TestCase):
    def _request(self, call_id="call_1", name="f", arguments=None):
        return ToolCallRequest(call_id, name, arguments or {})

    def test_validate_accepts_typed_requests(self):
        request = self._request()
        self.assertEqual(validate_tool_call_requests([request]), [request])

    def test_validate_rejects_bad_request_type(self):
        with self.assertRaises(ToolCallValidationError):
            validate_tool_call_requests([{"name": "f", "arguments": {}}])

    def test_validate_runs_per_tool_validator(self):
        request = self._request(arguments={"x": 1})
        validate_tool_call_requests([request], {"f": lambda args: args["x"] == 1})
        with self.assertRaises(ToolCallValidationError):
            validate_tool_call_requests([request], {"f": lambda args: False})

    def test_normalize_strips_name_and_id(self):
        request = self._request(" call_1 ", " f ", {"x": 1})
        normalized = normalize_tool_call_requests([request])[0]
        self.assertEqual(normalized.id, "call_1")
        self.assertEqual(normalized.name, "f")

    def test_deduplicate_keeps_first_call_id(self):
        calls = [self._request(arguments={"x": 1}), self._request(arguments={"x": 2}), self._request("call_2")]
        result = deduplicate_tool_call_requests(calls)
        self.assertEqual([call.arguments for call in result], [{"x": 1}, {}])

    def test_policy_rejects_denied_tool(self):
        with self.assertRaises(ToolCallPolicyError):
            check_tool_call_policy([self._request()], {"other"})

    def test_execute_wraps_executor_error(self):
        with self.assertRaises(ToolCallExecutionError):
            execute_tool_call_requests([self._request()], lambda calls: (_ for _ in ()).throw(RuntimeError("boom")))

    def test_execute_uses_injected_executor(self):
        result = execute_tool_call_requests(
            [self._request()],
            lambda calls: [{**calls[0], "result": {"ok": True}}],
        )
        self.assertEqual(result[0]["result"], {"ok": True})

    def test_serialize_rejects_non_json_result(self):
        with self.assertRaises(ToolCallSerializationError):
            serialize_tool_call_results([{"result": object()}])

    def test_pipeline_runs_in_order_and_serializes(self):
        seen = []
        text = '```tool_call\n{"id":"call_1","name":"f","arguments":{"x":1}}\n```'

        def validate(args):
            seen.append(("validate", dict(args)))
            return True

        def execute(calls):
            seen.append(("execute", calls[0]["name"], calls[0]["arguments"]))
            return [{"ok": True, "name": calls[0]["name"]}]

        result = run_tool_call_pipeline(
            text,
            allowed_tools={"f"},
            validators={"f": validate},
            executor=execute,
        )
        self.assertEqual(result, [{"ok": True, "name": "f"}])
        self.assertEqual(seen[0][0], "validate")
        self.assertEqual(seen[1][0], "execute")

    def test_pipeline_parse_error_is_distinct(self):
        with self.assertRaises(ToolCallParseError):
            run_tool_call_pipeline("```tool_call\n{not json}\n```")


class ToToolCallModelsTests(unittest.TestCase):
    def test_arguments_serialized_as_json_string(self):
        calls = [{"name": "f", "arguments": {"a": 1}}]
        models = to_tool_call_models(calls)
        self.assertEqual(models[0].function.name, "f")
        self.assertEqual(json.loads(models[0].function.arguments), {"a": 1})

    def test_id_prefix(self):
        models = to_tool_call_models([{"name": "f", "arguments": {}}])
        self.assertTrue(models[0].id.startswith("call_"))

    def test_accepts_typed_request_and_preserves_id(self):
        request = ToolCallRequest(
            id="call_fixed",
            name="f",
            arguments={"a": 1},
        )
        models = to_tool_call_models([request])
        self.assertEqual(models[0].id, "call_fixed")
        self.assertEqual(models[0].function.name, "f")
        self.assertEqual(json.loads(models[0].function.arguments), {"a": 1})


if __name__ == "__main__":
    unittest.main()
