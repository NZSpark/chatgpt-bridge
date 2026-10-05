import unittest




class ToolResultFidelityTests(unittest.TestCase):
    """工具执行结果必须逐字节保留，供 edit 工具做 oldText 精确匹配。"""

    def test_tool_result_preserves_trailing_newline(self):
        from chatgpt_web.prompting import _render_message
        from chatgpt_web.models import ChatMessage

        raw = "1  # Title\n2  \n3  ## Section\n4  text\n"
        m = ChatMessage(role="tool", content=raw, tool_call_id="call_1")
        rendered = _render_message(m)
        body = rendered.split("\n", 1)[1]
        self.assertEqual(body, raw)

    def test_tool_result_preserves_leading_blank_lines(self):
        from chatgpt_web.prompting import _render_message
        from chatgpt_web.models import ChatMessage

        raw = "\n\n# Title\n"
        m = ChatMessage(role="tool", content=raw)
        rendered = _render_message(m)
        body = rendered.split("\n", 1)[1]
        self.assertTrue(body.startswith("\n\n# Title"))


class ToolsInstructionDesignTests(unittest.TestCase):
    """工具调用指令的设计不变量（真实联网回归的固化）。

    背景：实测发现模型很容易「无视工具、直接凭知识作答」——问它查看当前目录，
    它会直接编一份 ls 输出，parse 结果为空。修好之后，以下要素必须保留，
    否则模型又会退回「直接作答」：
      1. 必须明确切断退路（“你没有直接的 shell/文件系统访问，唯一方式是 TOOL_CALL”）；
      2. 必须给出**具体到参数**的调用示例（只给格式模板不够）；
      3. 工具清单里每个工具的参数 schema 要带上；
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

    def test_instruction_lists_parameters_schema(self):
        from chatgpt_web.toolcalls import format_tools_instruction

        text = format_tools_instruction(self.TOOLS)
        self.assertIn("parameters (JSON Schema)", text)
        self.assertIn('"required"', text)

    def test_tool_instructions_come_before_user_task(self):
        from chatgpt_web.prompting import build_prompt
        from chatgpt_web.models import ChatMessage

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
        self.assertIn("TOOL_CALL:", text)

    def test_example_placeholder_is_not_angle_bracket(self):
        """示例里的占位符不能用 <command> 这种形式。

        实测：模型会把 ``<command>`` 原样照抄进参数，harness 执行时 shell 收到
        字面量 ``<command>``，``<`` 被解析成重定向符 → 语法报错。占位符必须
        是一眼就知道要替换、且照抄也不会被 shell 误解析的形式。
        """
        from chatgpt_web.toolcalls import format_tools_instruction

        text = format_tools_instruction(self.TOOLS)
        example = next(
            line for line in text.splitlines() if line.startswith("TOOL_CALL: ")
        )
        self.assertNotIn("<command>", example)
        self.assertIn("placeholder", text)

    def test_example_roundtrips_through_parser(self):
        """示例行的形态必须能被解析器认出来，避免文档与实现脱节。"""
        from chatgpt_web.toolcalls import (
            format_tools_instruction,
            parse_tool_calls,
        )

        text = format_tools_instruction(self.TOOLS)
        example = next(
            line for line in text.splitlines() if line.startswith("TOOL_CALL: ")
        )
        parsed = parse_tool_calls(example, valid_names={"bash"})
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "bash")


if __name__ == "__main__":
    unittest.main()
