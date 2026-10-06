"""SSE 流编码：首 chunk role、末 chunk finish_reason、[DONE] 收尾。"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatgpt_web.models import ChatCompletionRequest  # noqa: E402
from chatgpt_web.streaming import _chunk_text, _stream_chat_completion  # noqa: E402


def _collect(agen):
    async def run():
        return [item async for item in agen]

    return asyncio.run(run())


def _events(lines):
    out = []
    for line in lines:
        if line.startswith("data: "):
            body = line[len("data: "):].strip()
            if body == "[DONE]":
                out.append({"__done__": True})
            else:
                out.append(json.loads(body))
    return out


class FakeDriver:
    def __init__(self, reply="hello there"):
        self.reply = reply
        self.validate_calls = []

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None,
                        validate_reply=None):
        # 生产代码在工具模式下会传纠偏判定；fake 记录它，但不重试
        # （纠偏重试的行为由 tests/test_tool_injection.py 用假 page 覆盖）。
        self.validate_calls.append(validate_reply)
        if on_delta:
            await on_delta(self.reply)
        return self.reply, []


class ChunkTextTests(unittest.TestCase):
    def test_empty_returns_single_empty(self):
        self.assertEqual(_chunk_text(""), [""])

    def test_splits_by_size(self):
        self.assertEqual(_chunk_text("abcdef", size=2), ["ab", "cd", "ef"])

    def test_remainder(self):
        self.assertEqual(_chunk_text("abcde", size=2), ["ab", "cd", "e"])


class StreamTests(unittest.TestCase):
    def _request(self, **kw):
        return ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], **kw)

    def test_first_chunk_has_role(self):
        req = self._request()
        lines = _collect(_stream_chat_completion(req, "p", FakeDriver()))
        events = _events(lines)
        self.assertEqual(events[0]["choices"][0]["delta"], {"role": "assistant"})

    def test_content_delivered(self):
        req = self._request()
        lines = _collect(_stream_chat_completion(req, "p", FakeDriver("hello there")))
        events = _events(lines)
        content = "".join(
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if "choices" in e and "delta" in e["choices"][0]
        )
        self.assertIn("hello there", content)

    def test_finish_reason_stop(self):
        req = self._request()
        lines = _collect(_stream_chat_completion(req, "p", FakeDriver()))
        events = _events(lines)
        finishes = [
            e["choices"][0]["finish_reason"]
            for e in events
            if "choices" in e and e["choices"][0].get("finish_reason")
        ]
        self.assertIn("stop", finishes)

    def test_ends_with_done(self):
        req = self._request()
        lines = _collect(_stream_chat_completion(req, "p", FakeDriver()))
        self.assertEqual(lines[-1], "data: [DONE]\n\n")

    def test_chunk_object_type(self):
        req = self._request()
        lines = _collect(_stream_chat_completion(req, "p", FakeDriver()))
        events = _events(lines)
        self.assertEqual(events[0]["object"], "chat.completion.chunk")
        self.assertTrue(events[0]["id"].startswith("chatcmpl-"))

    def test_tool_call_stream(self):
        req = self._request(
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
            tool_choice="auto",
        )
        reply = '```tool_call\n{"name": "get_weather", "arguments": {"city": "SF"}}\n```'
        lines = _collect(_stream_chat_completion(req, "p", FakeDriver(reply)))
        events = _events(lines)
        tool_chunks = [
            e for e in events
            if "choices" in e and e["choices"][0]["delta"].get("tool_calls")
        ]
        self.assertTrue(tool_chunks)
        self.assertIn("tool_calls", finishes_of(events))


def finishes_of(events):
    return [
        e["choices"][0]["finish_reason"]
        for e in events
        if "choices" in e and e["choices"][0].get("finish_reason")
    ]


if __name__ == "__main__":
    unittest.main()
