"""工具注入与纠偏重试的单测（对应 doc/tasks.md T2.3 与 T1.1）。

* **T2.3**：注入块的标题常量化——「生成」与「去重 / 泄漏检测」必须同源。
  历史 bug：去重判定用中文标签（`[工具调用说明]`），而真实生成块首行是
  `[Tool Calling Instructions]`，两者永不相等 → 去重恒不生效。
* **T1.1**：播种路径的工具说明必须**紧贴本轮任务之前**（近因效应）；当首轮
  回复没有调用工具时，`send_chat` 必须在同一会话追发一次纠偏指令（仅一次），
  而不是把纯文本当最终答案返回。
"""

import asyncio
import json
import unittest
from unittest import mock

from chatgpt_web import config
from chatgpt_web.models import ChatMessage
from chatgpt_web.prompting import build_prompt
from chatgpt_web.toolcalls import (
    EDIT_MD_HEADER,
    EMPHASIS_HEADER,
    RETRY_HEADER,
    TOOLCALL_HEADER,
    edit_markdown_spec,
    format_tool_call_emphasis,
    format_tool_retry_nudge,
    format_tools_instruction,
    tool_call_predicate,
)

from .test_end_detection import EndDetectionTestCase, FakePage

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行 shell 命令",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]

EDIT_TOOLS = TOOLS + [{
    "type": "function",
    "function": {
        "name": "edit_markdown",
        "description": "按行号区间编辑 Markdown",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}, "start": {"type": "integer"},
                "end": {"type": "integer"}, "new_text": {"type": "string"},
            },
            "required": ["path", "start", "end", "new_text"],
        },
    },
}]


class HeaderConstantTests(unittest.TestCase):
    """T2.3：生成块与判定共用同一份标题常量。"""

    def test_generated_blocks_start_with_exported_headers(self) -> None:
        self.assertTrue(format_tools_instruction(TOOLS).startswith(TOOLCALL_HEADER))
        self.assertTrue(edit_markdown_spec().startswith(EDIT_MD_HEADER))

    def test_build_prompt_contains_real_header(self) -> None:
        prompt = build_prompt([ChatMessage(role="user", content="看看目录")], tools=TOOLS)
        self.assertIn(TOOLCALL_HEADER, prompt)
        # 旧的（错误的）中文标签不应再出现在判定路径上
        self.assertNotIn("[工具调用说明]", prompt)

    def test_inbound_system_with_real_header_is_not_duplicated(self) -> None:
        """harness 已内联一份工具说明时，不得再追加第二份（去重必须真实生效）。"""
        messages = [
            ChatMessage(role="system", content=f"{TOOLCALL_HEADER}\nYou have a bash tool."),
            ChatMessage(role="user", content="查看当前目录"),
        ]
        for seed in (False, True):
            with self.subTest(seed=seed):
                prompt = build_prompt(messages, tools=TOOLS, seed=seed)
                self.assertEqual(
                    prompt.count(TOOLCALL_HEADER), 1,
                    f"seed={seed} 时工具说明被重复注入",
                )

    def test_inbound_system_with_edit_md_header_is_not_duplicated(self) -> None:
        messages = [
            ChatMessage(role="system", content=f"{EDIT_MD_HEADER}\nPrefer edit_markdown."),
            ChatMessage(role="user", content="改一下 README"),
        ]
        prompt = build_prompt(messages, tools=EDIT_TOOLS, seed=True)
        self.assertEqual(prompt.count(EDIT_MD_HEADER), 1)


class ExampleShapeTests(unittest.TestCase):
    """注入块里的示例必须是**可解析的合法 JSON**。

    背景：旧版示例把占位符写成尖括号（`edit_markdown` 的 `"start": <int>`、
    强调块的 `{arguments object}`），模型会把占位符原样照抄 → JSON 非法 / 参数类型错
    （shell 收到字面量 `<` 直接语法报错）。示例必须与解析器同源、可往返。
    """

    def _fenced_body(self, text: str) -> str:
        start = text.index("```tool_call\n") + len("```tool_call\n")
        end = text.index("\n```", start)
        return text[start:end]

    def test_edit_markdown_spec_example_parses(self) -> None:
        from chatgpt_web.toolcalls import parse_tool_calls

        body = self._fenced_body(edit_markdown_spec())
        self.assertEqual(json.loads(body)["name"], "edit_markdown")
        parsed = parse_tool_calls(
            f"```tool_call\n{body}\n```", {"edit_markdown"}
        )
        self.assertEqual([c["name"] for c in parsed], ["edit_markdown"])

    def test_emphasis_block_example_parses_when_tools_given(self) -> None:
        from chatgpt_web.toolcalls import parse_tool_calls

        body = self._fenced_body(format_tool_call_emphasis(TOOLS))
        self.assertEqual(json.loads(body)["name"], "bash")
        parsed = parse_tool_calls(f"```tool_call\n{body}\n```", {"bash"})
        self.assertEqual([c["name"] for c in parsed], ["bash"])

    def test_retry_nudge_example_parses(self) -> None:
        body = self._fenced_body(format_tool_retry_nudge())
        # 占位符形式必须是合法 JSON（否则模型照抄时整条调用失效）
        self.assertIsInstance(json.loads(body), dict)
        self.assertNotIn("<", body)


class SeedOrderTests(unittest.TestCase):
    """T1.1：播种路径下工具说明紧贴本轮任务之前。"""

    def test_seed_tools_block_adjacent_to_last_message(self) -> None:
        messages = [
            ChatMessage(role="user", content="第一条消息"),
            ChatMessage(role="assistant", content="好的"),
            ChatMessage(role="user", content="请查看当前目录"),
        ]
        prompt = build_prompt(messages, tools=TOOLS, seed=True)
        tool_pos = prompt.rfind(TOOLCALL_HEADER)
        task_pos = prompt.rfind("请查看当前目录")
        self.assertNotEqual(tool_pos, -1, "未注入工具说明")
        self.assertNotEqual(task_pos, -1, "未保留本轮任务")
        self.assertLess(tool_pos, task_pos, "工具说明必须在本轮任务之前")
        between = prompt[tool_pos + len(TOOLCALL_HEADER):task_pos]
        for marker in ("[你之前的回复]", "[系统指令]", "[工具执行结果]"):
            self.assertNotIn(marker, between, f"工具说明与任务之间夹了 {marker}")

    def test_seed_keeps_emphasis_guard_at_top(self) -> None:
        """格式强调块仍在播种开头兜底（顶部），作为第二道保险。"""
        prompt = build_prompt(
            [ChatMessage(role="user", content="查看当前目录")], tools=TOOLS, seed=True
        )
        self.assertLess(prompt.find(EMPHASIS_HEADER), prompt.find(TOOLCALL_HEADER))

    def test_incremental_tools_block_stays_first(self) -> None:
        prompt = build_prompt(
            [
                ChatMessage(role="user", content="先前的问话"),
                ChatMessage(role="assistant", content="先前的回答"),
                ChatMessage(role="user", content="请查看当前目录"),
            ],
            tools=TOOLS,
        )
        self.assertTrue(prompt.startswith(TOOLCALL_HEADER))


class ToolRetryTests(EndDetectionTestCase):
    """T1.1：首轮未调用工具时的单次纠偏重试。"""

    PLAIN = "我根据已有知识回答：目录里大概是这些文件。"
    CALL = 'TOOL_CALL: {"name": "bash", "arguments": {"command": "ls -la"}}'

    def setUp(self) -> None:
        # 阈值显式固定，避免受仓库 .env（STABLE_POLLS=5）影响导致轮询步数不确定。
        super().setUp()
        for name, value in (("STABLE_POLLS", 2), ("LEN_STABLE_POLLS", 2)):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def _page(self) -> FakePage:
        # 时间线（query_selector_all 的调用序）：
        #   send#1 baseline(空) → 4 次轮询均为纯文本（走稳定判定收尾）
        #   send#2 baseline(上一轮的纯文本) → 4 次轮询均为工具调用
        script = [[self.PLAIN]] * 4 + [[self.PLAIN]] + [[self.CALL]] * 4
        return FakePage(baseline=[], script=script, generating=[False])

    def test_retry_once_when_first_reply_has_no_tool_call(self) -> None:
        page = self._page()
        driver = self.driver_for(page)
        text, _ = asyncio.run(
            driver.send_chat("看看当前目录", validate_reply=tool_call_predicate(TOOLS))
        )
        self.assertIn("TOOL_CALL", text, "纠偏后仍未拿到工具调用")
        # 第二次发送的内容必须是纠偏指令（而不是把原 prompt 重发一遍）
        self.assertIn(RETRY_HEADER, page._input.text)
        self.assertNotIn("看看当前目录", page._input.text)

    def test_no_retry_when_first_reply_is_accepted(self) -> None:
        page = self._page()
        driver = self.driver_for(page)
        text, _ = asyncio.run(
            driver.send_chat("看看当前目录", validate_reply=lambda t: True)
        )
        self.assertEqual(text, self.PLAIN)
        self.assertNotIn(RETRY_HEADER, page._input.text)
        self.assertIn("看看当前目录", page._input.text)

    def test_no_retry_without_validator(self) -> None:
        page = self._page()
        driver = self.driver_for(page)
        text, _ = asyncio.run(driver.send_chat("看看当前目录"))
        self.assertEqual(text, self.PLAIN)

    def test_task_end_sends_no_second_prompt(self) -> None:
        """驱动层回归（用户实测）：任务已执行过工具、模型用纯文本收尾时，
        桥不得再往输入框发任何 prompt。

        旧行为：工具模式下 `validate_reply` 恒为 `tool_call_predicate`，收尾的
        纯文本被判为「没调用工具」→ 立刻追发纠偏指令 → 模型只能又吐一条新指令，
        任务永远收不了尾。

        当前接线（`prompting.tool_nudge_predicate`）在历史里已有工具调用 /
        工具结果时返回 `None`：`send_chat` 直接把纯文本当最终答案，
        输入框里仍是最初那条用户任务（脚本中为第二轮准备的工具调用回复根本没被读到）。
        """
        from chatgpt_web.prompting import tool_nudge_predicate

        history = [
            ChatMessage(role="user", content="看看仓库状态"),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "bash", "arguments": "{}"},
                }],
            ),
            ChatMessage(role="tool", content="(no output)", tool_call_id="call_1"),
        ]
        validate = tool_nudge_predicate(history, TOOLS, "auto")
        self.assertIsNone(validate, "任务已进入执行阶段后不应再纠偏")

        page = self._page()
        driver = self.driver_for(page)
        text, _ = asyncio.run(
            driver.send_chat("看看当前目录", validate_reply=validate)
        )
        self.assertEqual(text, self.PLAIN, "收尾文本必须原样作为最终答案返回")
        self.assertNotIn(RETRY_HEADER, page._input.text)
        self.assertIn("看看当前目录", page._input.text)


class RetryNudgeTests(unittest.TestCase):
    def test_nudge_demands_single_tool_call(self) -> None:
        text = format_tool_retry_nudge()
        self.assertTrue(text.startswith(RETRY_HEADER))
        self.assertIn("ONE", text)
        self.assertIn("FAILED", text)
        # 纠偏要求的载体必须与解析器一致：代码围栏（旧版纯文本行会被网页渲染改写）。
        self.assertIn("```tool_call", text)
        fenced = '```tool_call\n{"name": "bash", "arguments": {"command": "ls"}}\n```'
        self.assertTrue(tool_call_predicate(TOOLS)(fenced), "纠偏给出的格式必须能被解析器认出")

    def test_predicate_matches_parsed_calls(self) -> None:
        predicate = tool_call_predicate(TOOLS)
        self.assertTrue(predicate(ToolRetryTests.CALL))
        self.assertFalse(predicate("这里没有任何工具调用"))
        self.assertFalse(predicate(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
