"""PI-901：tools 子包拆分后的模块级回归测试。

验证拆分后每个子模块可独立导入，且核心行为与历史 facade 一致：

* parser   解析（围栏 / shell 围栏修复 / 类型化边界）；
* validator 校验 / 规范化 / 去重；
* policy   允许列表 / 路径沙箱 / 写权限；
* ledger   会话隔离去重；
* executor 执行管线；
* serializer 结果序列化。

同时确认 ``chatgpt_web.toolcalls`` 仍作为兼容 facade 暴露同样的对象。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web import toolcalls  # noqa: E402
from chatgpt_web.errors import (  # noqa: E402
    ToolCallPolicyError,
    ToolCallSerializationError,
    ToolCallValidationError,
)
from chatgpt_web.tools import executor, ledger, parser, policy, serializer, validator  # noqa: E402

_FENCE_CALL = '```tool_call\n{"name": "bash", "arguments": {"command": "ls"}}\n```'


def _bash_tools():
    return [{
        "type": "function",
        "function": {
            "name": "bash",
            "parameters": {"properties": {"command": {"type": "string"}}},
        },
    }]


class ParserModuleTest(unittest.TestCase):
    def test_package_imports(self):
        # 每个子模块都能独立导入（不触发循环依赖）。
        self.assertTrue(all(m is not None for m in (parser, validator, policy, executor, ledger, serializer)))

    def test_parse_fence(self):
        calls = parser.parse_tool_calls(_FENCE_CALL)
        self.assertEqual(calls, [{"name": "bash", "arguments": {"command": "ls"}}])

    def test_typed_boundary(self):
        requests = parser.parse_tool_call_requests(_FENCE_CALL)
        self.assertEqual(len(requests), 1)
        self.assertIsInstance(requests[0], parser.ToolCallRequest)
        self.assertEqual(requests[0].name, "bash")

    def test_shell_fence_fallback(self):
        calls = parser._shell_fence_calls(
            "```bash\ngit status --short\n```",
            {"bash"},
            {"bash": ["command"]},
        )
        self.assertEqual(calls, [{"name": "bash", "arguments": {"command": "git status --short"}}])

    def test_tool_parameter_names(self):
        self.assertEqual(parser.tool_parameter_names(_bash_tools()), {"bash": ["command"]})


class ValidatorModuleTest(unittest.TestCase):
    def test_rejects_non_request(self):
        with self.assertRaises(ToolCallValidationError):
            validator.validate_tool_call_requests([{"name": "bash"}])

    def test_normalize_strips_names(self):
        req = parser.ToolCallRequest(id="  c1  ", name="  bash  ", arguments={"command": "ls"})
        out = validator.normalize_tool_call_requests([req])
        self.assertEqual((out[0].id, out[0].name), ("c1", "bash"))

    def test_deduplicate_ids(self):
        a = parser.ToolCallRequest(id="same", name="bash", arguments={})
        b = parser.ToolCallRequest(id="same", name="bash", arguments={})
        self.assertEqual(len(validator.deduplicate_tool_call_requests([a, b])), 1)


class PolicyModuleTest(unittest.TestCase):
    def test_disallowed_tool(self):
        req = parser.ToolCallRequest(id="c1", name="bash", arguments={})
        with self.assertRaises(ToolCallPolicyError):
            policy.check_tool_call_policy([req], allowed_tools={"edit_markdown"})

    def test_path_sandbox(self):
        _, err = policy.resolve_edit_path("/etc/passwd")
        self.assertIsNotNone(err)
        _, err2 = policy.resolve_edit_path("../escape.md")
        self.assertIsNotNone(err2)

    def test_write_denied_by_default(self):
        pol = policy.ToolPolicy(write_enabled=False)
        req = parser.ToolCallRequest(
            id="c1",
            name="edit_markdown",
            arguments={"path": "README.md", "start": 1, "end": 1, "new_text": "x", "write": True},
        )
        with self.assertRaises(ToolCallPolicyError):
            pol.validate_request(req)


class LedgerModuleTest(unittest.TestCase):
    def test_claim_owner_then_duplicate(self):
        led = ledger.ToolExecutionLedger()
        _, _, owner = led.claim("s1", "c1")
        self.assertTrue(owner)
        led.record(
            session_key="s1",
            tool_call_id="c1",
            tool_name="bash",
            normalized_arguments={"command": "ls"},
            started_at=0.0,
            success=True,
            error_type=None,
            result={"ok": True},
        )
        record, _, owner2 = led.claim("s1", "c1")
        self.assertFalse(owner2)
        self.assertIsNotNone(record)
        self.assertEqual(record.result, {"ok": True})

    def test_session_isolation(self):
        led = ledger.ToolExecutionLedger()
        _, _, owner_a = led.claim("s1", "c1")
        _, _, owner_b = led.claim("s2", "c1")
        self.assertTrue(owner_a)
        self.assertTrue(owner_b)


class ExecutorModuleTest(unittest.TestCase):
    def test_execute_without_executor_passthrough(self):
        req = parser.ToolCallRequest(id="c1", name="bash", arguments={"command": "ls"})
        out = executor.execute_tool_call_requests([req])
        self.assertEqual(out, [{"id": "c1", "name": "bash", "arguments": {"command": "ls"}}])

    def test_pipeline_parse_only(self):
        out = executor.run_tool_call_pipeline(
            _FENCE_CALL,
            allowed_tools={"bash"},
        )
        self.assertEqual(out, [{"id": out[0]["id"], "name": "bash", "arguments": {"command": "ls"}}])


class SerializerModuleTest(unittest.TestCase):
    def test_serializes_and_detaches(self):
        original = [{"ok": True, "nested": {"a": 1}}]
        out = serializer.serialize_tool_call_results(original)
        out[0]["nested"]["a"] = 2
        self.assertEqual(original[0]["nested"]["a"], 1)

    def test_rejects_non_serializable(self):
        with self.assertRaises(ToolCallSerializationError):
            serializer.serialize_tool_call_results([{"bad": object()}])


class FacadeCompatibilityTest(unittest.TestCase):
    def test_facade_reexports_implementation(self):
        self.assertIs(toolcalls.ToolCallRequest, parser.ToolCallRequest)
        self.assertIs(toolcalls.parse_tool_calls, parser.parse_tool_calls)
        self.assertIs(toolcalls.ToolExecutionLedger, ledger.ToolExecutionLedger)
        self.assertIs(toolcalls.ToolPolicy, policy.ToolPolicy)
        self.assertIs(toolcalls.run_local_edit_markdown, executor.run_local_edit_markdown)

    def test_facade_parse_matches_module(self):
        self.assertEqual(toolcalls.parse_tool_calls(_FENCE_CALL), parser.parse_tool_calls(_FENCE_CALL))


if __name__ == "__main__":
    unittest.main()
