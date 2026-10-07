"""API adapter 包（PI-904）。

把「协议转换」从 HTTP 处理里抽出来：

    api/chat_adapter.py       Chat Completions 语义 -> OpenAI SSE chunk
    api/responses_adapter.py  Responses API <-> 内部 Chat 语义（含流式命名事件）
    api/streaming_adapter.py  通用 SSE 事件编码辅助

**兼容契约**：:mod:`chatgpt_web.streaming` 与 :mod:`chatgpt_web.responses` 仍是
facade，历史 import（``from chatgpt_web.streaming import _stream_chat_completion``、
``from chatgpt_web.responses import run_chat``）不变。
"""

from .streaming_adapter import sse_event
from .chat_adapter import chunk_text, stream_chat_completion
from .responses_adapter import (
    ResponsesRequest,
    from_chat_response,
    handle_responses,
    run_chat,
    stream_responses,
    to_chat_request,
)

__all__ = [
    "sse_event",
    "chunk_text",
    "stream_chat_completion",
    "ResponsesRequest",
    "to_chat_request",
    "from_chat_response",
    "run_chat",
    "handle_responses",
    "stream_responses",
]
