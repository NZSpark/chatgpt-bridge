"""PI-023 property / invariant regression tests.

These tests make the six task invariants explicit without requiring real Playwright.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chatgpt_web import config
from chatgpt_web.chat_io import ChatIOMixin
from chatgpt_web.models import ChatMessage
from chatgpt_web.prompting import tool_nudge_predicate
from chatgpt_web.toolcalls import ToolExecutionLedger, normalize_tool_call_requests, parse_tool_call_requests
from chatgpt_web.tools.policy import resolve_edit_path


class _DummyChatIO(ChatIOMixin):
    """Minimal ChatIO harness for the one-shot nudge invariant."""

    def __init__(self):
        self._last_prompts = {}
        self._states = {}
        self.calls = []

    async def _ensure_page(self, bucket):
        return None

    async def _ensure_linked_target(self, bucket):
        # 宿主契约的一部分（PagePoolMixin 提供）：无不绑定即无导航
        return False

    def _state(self, bucket):
        return self._states.setdefault(
            bucket,
            SimpleNamespace(
                pending_rotation=False,
                has_history=False,
                cap_failures=0,
                last_error=None,
            ),
        )

    async def _remember_session(self, bucket):
        return None

    def _shrink_seed_if_repeated_cap(self, bucket, prompt, is_seed):
        return prompt

    async def _send_chat_locked(self, prompt, on_delta, key):
        self.calls.append((prompt, key))
        if len(self.calls) == 1:
            return "plain answer without a tool call", []
        return "final corrected reply", []


class NormalizeParseInvariantTests(unittest.TestCase):
    def test_normalize_parse_survives_common_model_noise(self):
        samples = [
            "",
            "   ",
            "I will handle this for you.",
            "\n\n```tool_call\n{\"id\": \"call_1\", \"name\": \"f\", \"arguments\": {\"x\": 1}}\n```\n",
            "prefix\n```tool_call\n{\"name\": \"f\", \"arguments\": {}}\n```\nsuffix",
            "```tool_call\n{not valid json}\n```",
            "TOOL_CALL: {\"name\": \"f\", \"arguments\": {\"x\": 1}}",
        ]

        for text in samples:
            with self.subTest(text=text):
                requests = parse_tool_call_requests(text, {"f"})
                normalized = normalize_tool_call_requests(requests)
                self.assertLessEqual(len(normalized), len(requests))
                for request in normalized:
                    self.assertTrue(request.id.strip())
                    self.assertTrue(request.name.strip())
                    self.assertIsInstance(request.arguments, dict)
                    self.assertEqual(request.id, request.id.strip())
                    self.assertEqual(request.name, request.name.strip())


class EditPathInvariantTests(unittest.TestCase):
    def test_accepted_edit_paths_always_resolve_inside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch.object(config, "EDIT_MARKDOWN_ROOT", root):
                accepted = [
                    "README.md",
                    "doc/tasks_pi.md",
                    "tests/test_example.py",
                    "nested/deeper/file.md",
                ]
                for path in accepted:
                    with self.subTest(path=path):
                        resolved, error = resolve_edit_path(path)
                        self.assertIsNone(error)
                        self.assertIsNotNone(resolved)
                        self.assertTrue(resolved.is_relative_to(root))


class NudgeInvariantTests(unittest.TestCase):
    def _plain_history(self):
        return [ChatMessage(role="user", content="Use the available tool.")]

    def test_automatic_nudge_can_happen_only_once(self):
        io = _DummyChatIO()

        async def run():
            with patch.object(config, "MAX_UPSTREAM_RETRIES", 1):
                return await io.send_chat(
                    "do it",
                    key="session-a",
                    validate_reply=lambda reply: reply == "final corrected reply",
                )

        result = asyncio.run(run())
        self.assertEqual(result[0], "final corrected reply")
        self.assertEqual(len(io.calls), 2)
        self.assertNotIn("final corrected reply", io.calls[0][0])
        self.assertIn("previous reply did not call any tool", io.calls[1][0].lower())

    def test_nudge_is_disabled_after_tool_execution(self):
        tools = [
            {
                "type": "function",
                "function": {"name": "f", "parameters": {"type": "object"}},
            }
        ]
        history = self._plain_history() + [
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            ),
            ChatMessage(
                role="tool",
                tool_call_id="call_1",
                content='{"ok": true}',
            ),
            ChatMessage(role="assistant", content="Done."),
        ]
        self.assertIsNone(tool_nudge_predicate(history, tools, "auto"))


class ToolLedgerInvariantTests(unittest.TestCase):
    def test_same_tool_call_id_executes_once_per_session(self):
        ledger = ToolExecutionLedger()

        first_record, _, first_owner = ledger.claim("session-a", "call-1")
        second_record, _, second_owner = ledger.claim("session-a", "call-1")

        self.assertIsNone(first_record)
        self.assertTrue(first_owner)
        self.assertFalse(second_owner)
        self.assertIsNone(second_record)

        record = ledger.record(
            session_key="session-a",
            tool_call_id="call-1",
            tool_name="f",
            normalized_arguments={"x": 1},
            started_at=0.0,
            success=True,
            error_type=None,
            result={"ok": True},
        )

        cached, _, owner_after_complete = ledger.claim("session-a", "call-1")
        self.assertIs(cached, record)
        self.assertFalse(owner_after_complete)


if __name__ == "__main__":
    unittest.main()
