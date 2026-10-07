import json
import unittest
from unittest import mock


def _fenced_example(text: str, with_json: bool = False):
    """从注入指令里取出 ```` ```tool_call ```` 示例块（可连同围栏内的 JSON 一并返回）。"""
    start = text.index("```tool_call\n") + len("```tool_call\n")
    end = text.index("\n```", start)
    body = text[start:end]
    if with_json:
        return f"```tool_call\n{body}\n```", body
    return body


class ToolResultFidelityTests(unittest.TestCase):
    """工具执行结果必须逐字节保留，供 edit 工具做 oldText 精确匹配。"""

    def test_tool_result_preserves_trailing_newline(self):
        from chatgpt_web.models import ChatMessage
        from chatgpt_web.prompting import _render_message

        raw = "1  # Title\n2  \n3  ## Section\n4  text\n"
        m = ChatMessage(role="tool", content=raw, tool_call_id="call_1")
        rendered = _render_message(m)
        body = rendered.split("\n", 1)[1]
        self.assertEqual(body, raw)

    def test_tool_result_preserves_leading_blank_lines(self):
        from chatgpt_web.models import ChatMessage
        from chatgpt_web.prompting import _render_message

        raw = "\n\n# Title\n"
        m = ChatMessage(role="tool", content=raw)
        rendered = _render_message(m)
        body = rendered.split("\n", 1)[1]
        self.assertTrue(body.startswith("\n\n# Title"))


class EmptyToolResultTests(unittest.TestCase):
    """空输出必须被显式说明（用户实测：`(no output)` 让模型反复重发同一条命令）。

    背景：命令成功但没有 stdout 时，客户端会渲染成 ``$ cmd`` + ``(no output)`` +
    ``Took 0.0s``。模型把这句理解成「命令没生效」，于是把同一条命令再发一遍，
    形成死循环。修复：把空输出等价形态替换成「已完成、无输出、请给下一条指令」。

    同时必须保证**非空**结果逐字节保留——edit 工具的 oldText 精确匹配依赖它。
    """

    @staticmethod
    def _body(content, tool_call_id="call_1"):
        from chatgpt_web.models import ChatMessage
        from chatgpt_web.prompting import _render_message

        message = ChatMessage(role="tool", content=content, tool_call_id=tool_call_id)
        rendered = _render_message(message)
        header, _, body = rendered.partition("\n")
        return header, body

    def test_blank_result_is_explained(self):
        _, body = self._body("   \n")
        self.assertIn("没有任何输出", body)
        self.assertIn("下一条指令", body)
        self.assertIn("不是失败", body)

    def test_harness_no_output_rendering_is_explained(self):
        raw = "$ git status --short\n\n(no output)\n\nTook 0.0s\n"
        header, body = self._body(raw)
        self.assertIn("call_1", header)
        self.assertIn("没有任何输出", body)
        # 歧义原文不再交给模型
        self.assertNotIn("(no output)", body)

    def test_codex_style_empty_result_is_explained(self):
        raw = (
            "Chunk ID: 20e42e\nWall time: 0.001 seconds\n"
            "Process exited with code 0\nOriginal token count: 0\nOutput:\n"
        )
        _, body = self._body(raw)
        self.assertIn("没有任何输出", body)

    def test_real_output_is_never_rewritten(self):
        # 关键回归：含真实内容的输出必须逐字节保留，否则 edit 的 oldText 匹配会失败。
        for raw in (
            "1  # Title\n2  \n3  ## Section\n4  text\n",   # 带行号的文件内容
            "$HOME\n",                                        # 真实输出以 $ 开头（不是回显）
            "no output here, just text\n",                    # 含关键词但非空标记
            "(no output)\nmore real text\n",                  # 标记 + 真实内容
            "Took 5.2s\nmodified: a.py\n",                    # 外壳 + 真实内容
        ):
            with self.subTest(raw=raw):
                _, body = self._body(raw)
                self.assertEqual(body, raw)


class _ToolHistoryBuilder:
    """构造「assistant 工具调用 → tool 空结果」的历史。"""

    TOOLS = [{
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }]

    @staticmethod
    def messages(commands):
        from chatgpt_web.models import ChatMessage, FunctionCall, ToolCall

        msgs = [ChatMessage(role="user", content="看看仓库状态")]
        for index, command in enumerate(commands):
            call = ToolCall(
                id=f"call_{index}",
                function=FunctionCall(
                    name="bash", arguments='{"command": "%s"}' % command
                ),
            )
            msgs.append(ChatMessage(role="assistant", content="", tool_calls=[call]))
            msgs.append(
                ChatMessage(
                    role="tool",
                    content="$ %s\n(no output)\nTook 0.0s" % command,
                    tool_call_id=call.id,
                )
            )
        return msgs


class RepeatedEmptyToolCallTests(unittest.TestCase):
    """死循环硬防线：同一条命令已重复（且每次空输出）时直接点名禁止重发。"""

    def _prompt(self, commands):
        from chatgpt_web.prompting import build_prompt

        msgs = _ToolHistoryBuilder.messages(commands)
        return build_prompt(msgs, tools=_ToolHistoryBuilder.TOOLS)

    def test_repeated_identical_command_is_called_out(self):
        prompt = self._prompt(["git status --short", "git status --short"])
        self.assertIn("[Repeated Empty Tool Call]", prompt)
        self.assertIn("called 2x", prompt)
        self.assertIn("git status --short", prompt)
        self.assertIn("NEVER produce output", prompt)

    def test_single_call_is_not_flagged(self):
        prompt = self._prompt(["git status --short"])
        self.assertNotIn("[Repeated Empty Tool Call]", prompt)
        # 但逐条说明仍必须在（空输出的正确解读）
        self.assertIn("没有任何输出", prompt)

    def test_different_commands_are_not_flagged(self):
        prompt = self._prompt(["git status --short", "git log --oneline -1"])
        self.assertNotIn("[Repeated Empty Tool Call]", prompt)

    def test_non_empty_result_is_not_flagged(self):
        from chatgpt_web.models import ChatMessage, FunctionCall, ToolCall
        from chatgpt_web.prompting import build_prompt

        first = ToolCall(function=FunctionCall(name="bash", arguments='{"command": "ls"}'))
        second = ToolCall(function=FunctionCall(name="bash", arguments='{"command": "ls"}'))
        msgs = [
            ChatMessage(role="user", content="看看目录"),
            ChatMessage(role="assistant", content="", tool_calls=[first]),
            ChatMessage(role="tool", content="a.py\nb.py", tool_call_id=first.id),
            ChatMessage(role="assistant", content="", tool_calls=[second]),
            ChatMessage(role="tool", content="a.py\nb.py", tool_call_id=second.id),
        ]
        prompt = build_prompt(msgs, tools=_ToolHistoryBuilder.TOOLS)
        self.assertNotIn("[Repeated Empty Tool Call]", prompt)
        self.assertIn("a.py", prompt)


class NoNudgeAfterTaskStartedTests(unittest.TestCase):
    """任务收尾不再被追发纠偏 prompt（用户实测：ChatGPT 已结束仍被推着再吐指令）。

    带工具的请求里，「模型回复没有工具调用」有两种完全不同的含义：

    * 本轮任务**一次工具都还没调用过** → 模型可能完全无视了工具、凭自身知识
      编了个结果（E2E C1/C2）——保留 T1.1 的一次纠偏；
    * 历史里已经有工具结果 / assistant tool_calls → 任务早已进入执行阶段，
      这次的纯文本回复是**收尾**（“已完成 / 工作区是干净的”）——**不能**再追发
      任何 prompt，否则等于把结论重新推成一条新命令，模型只能继续下指令，
      任务永远结束不了。

    无指令的回复直接作为最终答案返回（客户端看到没有 tool_calls 即判定任务
    结束）；若模型其实还在生成，chat_io 的静默窗口会继续等到内容出现。
    """

    TOOLS = _ToolHistoryBuilder.TOOLS
    CALL_TEXT = 'TOOL_CALL: {"name": "bash", "arguments": {"command": "ls"}}'

    def _plain_history(self):
        from chatgpt_web.models import ChatMessage

        return [
            ChatMessage(role="system", content="你是助手"),
            ChatMessage(role="user", content="看看仓库状态"),
            ChatMessage(role="assistant", content="仓库是干净的，任务完成。"),
            ChatMessage(role="user", content="那再确认一下分支"),
        ]

    # ---------- has_prior_tool_use ----------

    def test_has_prior_tool_use_false_for_plain_chat(self):
        from chatgpt_web.prompting import has_prior_tool_use

        self.assertFalse(has_prior_tool_use([]))
        self.assertFalse(has_prior_tool_use(self._plain_history()))

    def test_has_prior_tool_use_detects_tool_result(self):
        from chatgpt_web.models import ChatMessage
        from chatgpt_web.prompting import has_prior_tool_use

        history = self._plain_history() + [
            ChatMessage(role="tool", content="main\n", tool_call_id="call_1")
        ]
        self.assertTrue(has_prior_tool_use(history))

    def test_has_prior_tool_use_detects_assistant_tool_calls(self):
        from chatgpt_web.models import ChatMessage, FunctionCall, ToolCall
        from chatgpt_web.prompting import has_prior_tool_use

        history = self._plain_history() + [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(function=FunctionCall(name="bash", arguments="{}"))],
            )
        ]
        self.assertTrue(has_prior_tool_use(history))

    # ---------- tool_nudge_predicate ----------

    def test_no_tools_never_nudges(self):
        from chatgpt_web.prompting import tool_nudge_predicate

        self.assertIsNone(tool_nudge_predicate(self._plain_history(), None))
        self.assertIsNone(tool_nudge_predicate(self._plain_history(), []))

    def test_tool_choice_none_never_nudges(self):
        from chatgpt_web.prompting import tool_nudge_predicate

        self.assertIsNone(
            tool_nudge_predicate(self._plain_history(), self.TOOLS, "none")
        )

    def test_first_turn_without_tool_use_still_nudges(self):
        """T1.1 保留：一次工具都没调用过时，仍纠偏一次。"""
        from chatgpt_web.prompting import tool_nudge_predicate

        predicate = tool_nudge_predicate(self._plain_history(), self.TOOLS, "auto")
        self.assertIsNotNone(predicate)
        self.assertFalse(predicate("仓库是干净的，任务完成。"))
        self.assertTrue(predicate(self.CALL_TEXT))

    def test_tool_result_disables_nudge(self):
        """用户实测的 bug：任务已执行过工具，收尾时不能再追发 prompt。"""
        from chatgpt_web.models import ChatMessage
        from chatgpt_web.prompting import tool_nudge_predicate

        history = self._plain_history() + [
            ChatMessage(role="tool", content="(no output)", tool_call_id="call_1")
        ]
        self.assertIsNone(tool_nudge_predicate(history, self.TOOLS, "auto"))

    def test_prior_tool_calls_disable_nudge(self):
        from chatgpt_web.models import ChatMessage, FunctionCall, ToolCall
        from chatgpt_web.prompting import tool_nudge_predicate

        history = self._plain_history() + [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[ToolCall(function=FunctionCall(name="bash", arguments="{}"))],
            )
        ]
        self.assertIsNone(tool_nudge_predicate(history, self.TOOLS, "auto"))

    def test_config_off_disables_nudge_everywhere(self):
        """``TOOL_NUDGE_UNTIL_FIRST_CALL=false`` = 完全不纠偏（桥绝不自行追发 prompt）。"""
        from unittest.mock import patch

        from chatgpt_web import config
        from chatgpt_web.models import ChatMessage
        from chatgpt_web.prompting import tool_nudge_predicate

        with patch.object(config, "TOOL_NUDGE_UNTIL_FIRST_CALL", False):
            self.assertIsNone(
                tool_nudge_predicate(self._plain_history(), self.TOOLS, "auto")
            )
            self.assertIsNone(
                tool_nudge_predicate(
                    [ChatMessage(role="user", content="hi")],
                    self.TOOLS,
                    "auto",
                )
            )

    def test_config_on_is_the_default(self):
        from chatgpt_web import config

        self.assertTrue(config.TOOL_NUDGE_UNTIL_FIRST_CALL)


class ToolsInstructionDesignTests(unittest.TestCase):
    """工具调用指令的设计不变量（真实联网回归的固化）。

    背景：实测发现模型很容易「无视工具、直接凭知识作答」——问它查看当前目录，
    它会直接编一份 ls 输出，parse 结果为空。修好之后，以下要素必须保留，
    否则模型又会退回「直接作答」：
      1. 必须明确切断退路（“你没有直接的 shell/文件系统访问，唯一方式是 TOOL_CALL”）；
      2. 必须给出**具体到参数**的调用示例（只给格式模板不够）；
      3. 工具清单里每个工具的必填参数要带上（默认紧凑形态；完整 schema 由
         TOOLS_INSTRUCTION_VERBOSE 控制）；
      4. 工具说明要放在**用户任务之前**，别被用户消息隔开。
    """

    TOOLS = [{
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }]

    def test_instruction_forbids_answering_without_tool(self):
        from chatgpt_web.toolcalls import format_tools_instruction

        text = format_tools_instruction(self.TOOLS)
        # 切断退路：必须出现“没有直接访问 / 唯一方式”这类硬约束
        self.assertIn("NO direct access", text)
        self.assertIn("ONLY way", text)
        self.assertIn("FAILS", text)
        self.assertIn("Never fabricate", text)

    def test_instruction_contains_concrete_example(self):
        from chatgpt_web.toolcalls import format_tools_instruction

        text = format_tools_instruction(self.TOOLS)
        # 具体示例：应带工具名与至少一个参数键，而不是空模板
        self.assertIn('"name": "bash"', text)
        self.assertIn('"command"', text)

    def test_instruction_lists_required_params_compactly(self):
        """默认紧凑形态：只列 name(必填参数)，不 dump 完整 JSON Schema。"""
        from chatgpt_web import config
        from chatgpt_web.toolcalls import format_tools_instruction

        with mock.patch.object(config, "TOOLS_INSTRUCTION_VERBOSE", False):
            text = format_tools_instruction(self.TOOLS)
        self.assertIn("bash(command)", text)
        self.assertNotIn("parameters (JSON Schema)", text)

    def test_instruction_lists_parameters_schema_when_verbose(self):
        """打开 VERBOSE 时回退到完整 JSON Schema（调试用）。"""
        from chatgpt_web import config
        from chatgpt_web.toolcalls import format_tools_instruction

        with mock.patch.object(config, "TOOLS_INSTRUCTION_VERBOSE", True):
            text = format_tools_instruction(self.TOOLS)
        self.assertIn("parameters (JSON Schema)", text)
        self.assertIn('"required"', text)

    def test_tool_instructions_come_before_user_task(self):
        from chatgpt_web.models import ChatMessage
        from chatgpt_web.prompting import build_prompt

        msgs = [ChatMessage(role="user", content="请查看当前目录")]
        prompt = build_prompt(msgs, tools=self.TOOLS, seed=True)
        tool_pos = prompt.find("[Tool Calling Instructions]")
        task_pos = prompt.find("请查看当前目录")
        self.assertNotEqual(tool_pos, -1)
        self.assertNotEqual(task_pos, -1)
        self.assertLess(tool_pos, task_pos, "工具说明必须排在用户任务之前")

    def test_emphasis_block_states_mandate(self):
        from chatgpt_web.toolcalls import format_tool_call_emphasis

        text = format_tool_call_emphasis()
        self.assertIn("MUST use", text)
        # 载体是代码围栏（必须与解析器认得的形态一致）；旧的「纯文本行」措辞不得再出现。
        self.assertIn("```tool_call", text)
        self.assertNotIn("no code fences", text)

    def test_instruction_mandates_single_call(self):
        """提示词必须要求「一次只返回一个 TOOL_CALL」。

        客户端一次只能处理一个工具调用；若模型在同一回复里输出多条
        TOOL_CALL，用户端无法处理。两块提示词（工具说明 + 格式强调）都要
        明确禁止多调用，且不得再出现旧版「可以一次调用多个工具」的措辞。
        """
        from chatgpt_web.toolcalls import (
            format_tool_call_emphasis,
            format_tools_instruction,
        )

        for text in (
            format_tools_instruction(self.TOOLS),
            format_tool_call_emphasis(),
        ):
            self.assertIn("ONE tool_call", text)
            self.assertNotIn("several tool_call", text)
            self.assertNotIn("multiple tools at once", text)

    def test_instruction_never_advertises_plain_text_calls(self):
        """两块注入指令都只能宣传围栏块。

        2026-10-06 真机教训：旧措辞在头段落写「唯一方式是输出 TOOL_CALL 行」、
        又在规则里禁止纯文本行——同一段文字自相矛盾，模型两种写法都会试一遍。
        """
        from chatgpt_web.toolcalls import (
            format_tool_call_emphasis,
            format_tools_instruction,
        )

        for text in (
            format_tools_instruction(self.TOOLS),
            format_tool_call_emphasis(self.TOOLS),
        ):
            self.assertIn("```tool_call", text)
            self.assertNotIn("TOOL_CALL", text)

    def test_example_uses_required_params_with_declared_types(self):
        """示例只列必填参数，且非字符串参数不能写成字符串占位。

        旧版把第一个工具的前两个属性一律填成 ``"..."``——`read` 的示例就是
        ``{"path": "...", "offset": "..."}``，模型照抄即得到一个字符串 offset。
        """
        from chatgpt_web.toolcalls import format_tools_instruction

        tools = [{
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer"},
                        "with_line_numbers": {"type": "boolean"},
                    },
                    "required": ["path", "offset"],
                },
            },
        }]
        text = format_tools_instruction(tools)
        _, body = _fenced_example(text, with_json=True)
        args = json.loads(body)["arguments"]
        self.assertEqual(list(args), ["path", "offset"])
        self.assertEqual(args["path"], "...")
        self.assertEqual(args["offset"], 0)

    def test_instruction_forbids_bare_bash_code_block(self):
        """必须写明「命令永远写在 command 参数里」（2026-10-07 用户实测）。

        用户实测：模型把命令直接回成 `bash` 代码块（`git status --short`），
        解析链拿不到 tool_call 载体。两块提示词都要提前说清楚，解析层的
        shell 围栏修复（SHELL_FENCE_FALLBACK）只是兜底，不是正路。
        """
        from chatgpt_web.toolcalls import (
            format_tool_call_emphasis,
            format_tools_instruction,
        )

        for text in (
            format_tools_instruction(self.TOOLS),
            format_tool_call_emphasis(self.TOOLS),
        ):
            self.assertIn("A shell command always travels as the `command` value", text)

    def test_instruction_explains_empty_output(self):
        """工具说明必须写明「空输出 = 成功，不要重发同一条命令」
        （用户实测死循环的第一道防线）。"""
        from chatgpt_web.toolcalls import format_tools_instruction

        text = format_tools_instruction(self.TOOLS)
        self.assertIn("EMPTY", text)
        self.assertIn("no output", text)
        self.assertIn("Never re-run the exact same command", text)
        self.assertIn("loops forever", text)
        self.assertIn("move on to the NEXT", text)

    def test_fence_delimiters_are_paired(self):
        """四块注入文本里的三反引号必须成对，且配对之间只能是标签行 + JSON。

        背景：提示词里出现**未配对**的三反引号会被网页版当成围栏开始，把后半段指令
        整段吃掉（同类事故见 doc/code_block_fence.md）。因此正文里只能写内联反引号
        （`tool_call`），不能出现裸的 ```` ``` ````。
        """
        from chatgpt_web.toolcalls import (
            edit_markdown_spec,
            format_tool_call_emphasis,
            format_tool_retry_nudge,
            format_tools_instruction,
        )

        for text in (
            format_tools_instruction(self.TOOLS),
            format_tool_call_emphasis(self.TOOLS),
            edit_markdown_spec(),
            format_tool_retry_nudge(),
        ):
            parts = text.split("```")
            self.assertEqual(len(parts) % 2, 1, f"三反引号不成对：\n{text}")
            for inside in parts[1::2]:
                self.assertTrue(
                    inside.strip().startswith(("tool_call", "tool-call")),
                    f"围栏里出现非标签内容（多半是正文里写了裸反引号）：{inside!r}",
                )

    def test_example_placeholder_is_not_angle_bracket(self):
        """示例里的占位符不能用 <command> 这种形式。

        实测：模型会把 ``<command>`` 原样照抄进参数，harness 执行时 shell 收到
        字面量 ``<command>``，``<`` 被解析成重定向符 → 语法报错。占位符必须
        是一眼就知道要替换、且照抄也不会被 shell 误解析的形式。
        """
        from chatgpt_web.toolcalls import format_tools_instruction

        text = format_tools_instruction(self.TOOLS)
        example = _fenced_example(text)
        self.assertNotIn("<command>", example)
        self.assertIn("placeholder", text)

    def test_example_roundtrips_through_parser(self):
        """示例行的形态必须能被解析器认出来，避免文档与实现脱节。"""
        from chatgpt_web.toolcalls import (
            format_tools_instruction,
            parse_tool_calls,
        )

        text = format_tools_instruction(self.TOOLS)
        example, example_json = _fenced_example(text, with_json=True)
        parsed = parse_tool_calls(example, valid_names={"bash"})
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "bash")
        # 围栏里的 JSON 必须能被标准 json 解析（格式说明与解析器同源）。
        self.assertEqual(json.loads(example_json)["name"], "bash")


if __name__ == "__main__":
    unittest.main()
