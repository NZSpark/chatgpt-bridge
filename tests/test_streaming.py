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


class NudgePredicateWiringTests(unittest.TestCase):
    """工具模式下传给 driver 的纠偏判定必须随「任务是否已开始执行」变化。

    用户实测：ChatGPT 已经结束任务（纯文本收尾）后，旧行为仍会追发一条纠偏
    prompt，把结论重新推成一条新指令，任务永远收不了尾。修好后的语义：

    * 一次工具都还没调用过 → 仍传判定（保留 T1.1 的首轮纠偏）；
    * 历史里已有工具结果 / assistant 工具调用 → 传 ``None``（桥不追发任何
      prompt，无指令的纯文本回复即最终答案，由客户端判定任务结束）。
    """

    TOOLS = [{"type": "function", "function": {"name": "bash"}}]

    def _validate_args(self, messages, **kw):
        req = ChatCompletionRequest(messages=messages, **kw)
        driver = FakeDriver()
        _collect(_stream_chat_completion(req, "p", driver))
        return driver.validate_calls

    def test_first_turn_keeps_nudge(self):
        calls = self._validate_args(
            [{"role": "user", "content": "看看仓库状态"}],
            tools=self.TOOLS,
            tool_choice="auto",
        )
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0])
        self.assertFalse(calls[0]("仓库是干净的，任务完成。"))

    def test_after_tool_result_nudge_is_disabled(self):
        calls = self._validate_args(
            [
                {"role": "user", "content": "看看仓库状态"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "(no output)"},
                {"role": "user", "content": "那再确认一下分支"},
            ],
            tools=self.TOOLS,
            tool_choice="auto",
        )
        self.assertEqual(calls, [None])

    def test_without_tools_no_nudge(self):
        calls = self._validate_args([{"role": "user", "content": "你好"}])
        self.assertEqual(calls, [None])

    def test_tool_choice_none_no_nudge(self):
        calls = self._validate_args(
            [{"role": "user", "content": "你好"}],
            tools=self.TOOLS,
            tool_choice="none",
        )
        self.assertEqual(calls, [None])


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
