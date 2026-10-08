"""PI-024 API contract tests.

Freeze the protocol boundaries that are independent of a real Playwright session:
Chat Completions usage/errors and Responses usage/errors.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from chatgpt_web.api import chat_adapter, responses_adapter
from chatgpt_web.api.chat_adapter import ChatAdapterError
from chatgpt_web.api.responses_adapter import ResponsesRequest
from chatgpt_web.driver import (
    ChatGPTBusyError,
    ChatGPTContextLimitError,
    ChatGPTTimeoutError,
)
from chatgpt_web.models import ChatCompletionRequest
from chatgpt_web.prompting import estimate_tokens
from chatgpt_web.server import app


class ApiPayloadContractTests(unittest.TestCase):
    def test_responses_usage_contract(self):
        self.assertEqual(
            responses_adapter._usage_dict(12, 7),
            {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
        )

    def test_responses_text_response_contains_usage(self):
        response = responses_adapter.from_chat_response(
            "hello there", "gpt-4o", prompt_tokens=12
        )
        self.assertEqual(response["object"], "response")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["model"], "gpt-4o")
        self.assertEqual(response["usage"]["input_tokens"], 12)
        self.assertEqual(
            response["usage"]["output_tokens"], estimate_tokens("hello there")
        )
        self.assertEqual(
            response["usage"]["total_tokens"], 12 + estimate_tokens("hello there")
        )

    def test_responses_tool_response_contains_usage(self):
        response = responses_adapter.from_chat_response(
            "",
            "gpt-4o",
            prompt_tokens=5,
            tool_calls=[{"name": "bash", "arguments": {"command": "pwd"}}],
        )
        self.assertEqual(response["usage"], {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5})
        self.assertEqual(response["output"][0]["type"], "function_call")

    def test_responses_error_payload_contract(self):
        self.assertEqual(
            responses_adapter._error_payload("boom", "upstream_error"),
            {"error": {"message": "boom", "type": "upstream_error"}},
        )

    def test_responses_exception_mapping_contract(self):
        cases = [
            (ChatGPTContextLimitError("context"), (400, "context_length_exceeded")),
            (ChatGPTBusyError("busy"), (503, "upstream_busy")),
            (ChatGPTTimeoutError("timeout"), (504, "timeout")),
            (RuntimeError("upstream"), (502, "upstream_error")),
            (ValueError("bad"), (500, "server_error")),
        ]
        for exc, expected in cases:
            with self.subTest(type=type(exc).__name__):
                self.assertEqual(responses_adapter._map_exception(exc), expected)

    def test_chat_error_payload_contract(self):
        client = TestClient(app)
        response = client.post(
            "/v1/chat/completions", json={"model": "gpt-4o", "messages": []}
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["code"], 400)
        self.assertEqual(body["error"]["message"], "messages 不能为空")


class ChatCompletionUsageContractTests(unittest.TestCase):
    def test_non_streaming_usage_shape(self):
        sent_prompt = "hello from prompt"
        reply = "hello from assistant"
        driver = MagicMock()
        driver.send_chat = AsyncMock(return_value=(reply, []))
        driver.sent_prompt.return_value = sent_prompt
        request = ChatCompletionRequest(
            model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
        )

        response = asyncio.run(
            chat_adapter.run_chat_completion(
                request,
                driver,
                session_key="contract-test",
                auto_local_edit_markdown=False,
                task_block=None,
                prompt=sent_prompt,
                seeded_prompt=sent_prompt,
            )
        )

        payload = response.model_dump()
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(
            payload["usage"],
            {
                "prompt_tokens": estimate_tokens(sent_prompt),
                "completion_tokens": estimate_tokens(reply),
                "total_tokens": estimate_tokens(sent_prompt) + estimate_tokens(reply),
            },
        )

    def test_tool_call_response_keeps_usage_contract(self):
        tool_reply = 'TOOL_CALL: {"name":"bash","arguments":{"command":"pwd"}}'
        sent_prompt = "run a command"
        driver = MagicMock()
        driver.send_chat = AsyncMock(return_value=(tool_reply, []))
        driver.sent_prompt.return_value = sent_prompt
        request = ChatCompletionRequest(
            model="gpt-4o",
            messages=[{"role": "user", "content": "run a command"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "bash", "parameters": {"type": "object"}},
                }
            ],
        )

        response = asyncio.run(
            chat_adapter.run_chat_completion(
                request,
                driver,
                session_key="contract-tool",
                auto_local_edit_markdown=False,
                task_block=None,
                prompt=sent_prompt,
                seeded_prompt=sent_prompt,
            )
        )

        payload = response.model_dump()
        self.assertEqual(payload["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(payload["usage"]["prompt_tokens"], estimate_tokens(sent_prompt))
        self.assertEqual(payload["usage"]["completion_tokens"], estimate_tokens(tool_reply))
        self.assertEqual(
            payload["usage"]["total_tokens"],
            estimate_tokens(sent_prompt) + estimate_tokens(tool_reply),
        )

    def test_driver_errors_map_to_contract_errors(self):
        cases = [
            (ChatGPTContextLimitError("context"), 400, "context_length_exceeded"),
            (ChatGPTBusyError("busy"), 503, "upstream_busy"),
            (ChatGPTTimeoutError("timeout"), 504, "timeout"),
            (RuntimeError("upstream"), 502, "upstream_error"),
            (ValueError("unexpected"), 500, "server_error"),
        ]
        for exc, status_code, err_type in cases:
            with self.subTest(type=type(exc).__name__):
                driver = MagicMock()
                driver.send_chat = AsyncMock(side_effect=exc)
                request = ChatCompletionRequest(
                    model="gpt-4o", messages=[{"role": "user", "content": "hello"}]
                )
                with self.assertRaises(ChatAdapterError) as caught:
                    asyncio.run(
                        chat_adapter.run_chat_completion(
                            request,
                            driver,
                            session_key="contract-error",
                            auto_local_edit_markdown=False,
                            task_block=None,
                            prompt="hello",
                            seeded_prompt="hello",
                        )
                    )
                self.assertEqual(caught.exception.status_code, status_code)
                self.assertEqual(caught.exception.err_type, err_type)


class ResponsesRequestContractTests(unittest.TestCase):
    def test_minimal_request_defaults(self):
        request = ResponsesRequest(input="hello")
        self.assertEqual(request.model, "chatgpt-chat")
        self.assertFalse(request.stream)
        self.assertEqual(request.input, "hello")
        self.assertIsNone(request.tools)


if __name__ == "__main__":
    unittest.main()
