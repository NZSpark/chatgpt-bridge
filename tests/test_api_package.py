"""PI-904：api 子包拆分后的模块级回归测试。

验证：

* chat_adapter / responses_adapter / streaming_adapter 可独立导入（无循环依赖）；
* ``chatgpt_web.streaming`` 与 ``chatgpt_web.responses`` facade 再导出同一对象，
  历史 import 路径不变；
* ``chatgpt_web.api`` 包 __all__ 与三个子模块的公开对象一致；
* SSE 编码行为（裸 ``data:`` chunk 与命名 ``event:`` 事件）与拆分前一致；
* server.py 只负责 HTTP：不再直接实现协议转换，而是委托 api 适配器。
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chatgpt_web.api as api  # noqa: E402
from chatgpt_web import responses as responses_facade  # noqa: E402
from chatgpt_web import streaming as streaming_facade  # noqa: E402
from chatgpt_web.api import chat_adapter, responses_adapter, streaming_adapter  # noqa: E402
from chatgpt_web.models import ChatCompletionRequest  # noqa: E402


class PackageImportTest(unittest.TestCase):
    """三个子模块必须能独立导入，且无循环依赖。"""

    def test_submodules_import(self):
        self.assertTrue(callable(chat_adapter.stream_chat_completion))
        self.assertTrue(callable(responses_adapter.handle_responses))
        self.assertTrue(callable(streaming_adapter.sse_event))

    def test_package_all_resolvable(self):
        for name in api.__all__:
            self.assertTrue(hasattr(api, name), name)

    def test_package_all_matches_submodule_sources(self):
        # 包级 re-export 必须与子模块里的同名对象是同一对象。
        self.assertIs(api.sse_event, streaming_adapter.sse_event)
        self.assertIs(api.chunk_text, chat_adapter.chunk_text)
        self.assertIs(api.stream_chat_completion, chat_adapter.stream_chat_completion)
        self.assertIs(api.ResponsesRequest, responses_adapter.ResponsesRequest)
        self.assertIs(api.to_chat_request, responses_adapter.to_chat_request)
        self.assertIs(api.from_chat_response, responses_adapter.from_chat_response)
        self.assertIs(api.run_chat, responses_adapter.run_chat)
        self.assertIs(api.handle_responses, responses_adapter.handle_responses)
        self.assertIs(api.stream_responses, responses_adapter.stream_responses)


class FacadeContractTest(unittest.TestCase):
    """历史 import 路径必须解析到子模块里的同一对象。"""

    def test_streaming_facade_reexports(self):
        self.assertIs(
            streaming_facade._stream_chat_completion,
            chat_adapter.stream_chat_completion,
        )
        self.assertIs(streaming_facade._chunk_text, chat_adapter.chunk_text)

    def test_responses_facade_reexports(self):
        for name in (
            "ResponsesRequest",
            "to_chat_request",
            "from_chat_response",
            "run_chat",
            "handle_responses",
            "stream_responses",
        ):
            self.assertIs(
                getattr(responses_facade, name),
                getattr(responses_adapter, name),
                name,
            )

    def test_responses_facade_private_helpers(self):
        # 既有测试用 patch.object 打在这些私有 helper 上，必须仍可访问。
        for name in (
            "_error_payload",
            "_map_exception",
            "_maybe_register_edit_markdown",
            "_sse",
            "_tool_to_chat",
            "_usage_dict",
        ):
            self.assertTrue(hasattr(responses_facade, name), name)


class StreamingAdapterTest(unittest.TestCase):
    """通用 SSE 事件编码（命名事件）行为不变。"""

    def test_named_event_shape(self):
        chunk = streaming_adapter.sse_event("response.output_text.delta", {"delta": "hi"})
        self.assertTrue(chunk.startswith("event: response.output_text.delta\n"))
        self.assertTrue(chunk.endswith("\n\n"))
        data_line = [l for l in chunk.splitlines() if l.startswith("data: ")][0]
        payload = json.loads(data_line[len("data: "):])
        self.assertEqual(payload["type"], "response.output_text.delta")
        self.assertEqual(payload["delta"], "hi")

    def test_payload_not_mutated(self):
        original = {"delta": "hi"}
        streaming_adapter.sse_event("response.output_text.delta", original)
        self.assertEqual(original, {"delta": "hi"})


class ChunkTextTest(unittest.TestCase):
    """chat_adapter 的纯函数辅助。"""

    def test_empty_returns_single_empty(self):
        self.assertEqual(chat_adapter.chunk_text(""), [""])

    def test_splits_by_size(self):
        self.assertEqual(chat_adapter.chunk_text("abcdef", size=2), ["ab", "cd", "ef"])


class ChatAdapterStreamTest(unittest.TestCase):
    """chat_adapter 产出的裸 data: chunk 与历史 streaming facade 一致。"""

    def test_stream_via_adapter_matches_facade(self):
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}])
        driver = _FakeDriver()

        async def collect(agen):
            return [item async for item in agen]

        direct = asyncio.run(
            collect(chat_adapter.stream_chat_completion(req, "p", _FakeDriver()))
        )
        facade = asyncio.run(
            collect(streaming_facade._stream_chat_completion(req, "p", _FakeDriver()))
        )
        self.assertEqual(_strip_ids(direct), _strip_ids(facade))
        self.assertEqual(direct[-1], "data: [DONE]\n\n")


class ServerDelegatesToApiTest(unittest.TestCase):
    """server.py 只负责 HTTP，协议转换委托给 api 适配器。"""

    def test_server_imports_api_adapters(self):
        import chatgpt_web.server as server

        self.assertIs(server.run_chat_completion, chat_adapter.run_chat_completion)
        self.assertIs(server.register_edit_markdown, chat_adapter.register_edit_markdown)
        self.assertIs(server.ChatAdapterError, chat_adapter.ChatAdapterError)

    def test_server_responses_handler_from_facade(self):
        import chatgpt_web.server as server

        self.assertIs(server.handle_responses, responses_adapter.handle_responses)


class _FakeDriver:
    def __init__(self, reply="hello there"):
        self.reply = reply

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None,
                        validate_reply=None):
        if on_delta:
            await on_delta(self.reply)
        return self.reply, []

    def sent_prompt(self, key=None):
        return None


def _strip_ids(lines):
    """chunk id / created 每次不同，比较时归零，只看结构与内容。"""
    out = []
    for line in lines:
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            out.append(line)
            continue
        body = json.loads(line[len("data: "):])
        body.pop("id", None)
        body.pop("created", None)
        out.append(json.dumps(body, sort_keys=True, ensure_ascii=False))
    return out


if __name__ == "__main__":
    unittest.main()
