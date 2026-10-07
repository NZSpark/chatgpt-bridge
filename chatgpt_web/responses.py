"""OpenAI Responses API（Codex CLI 专用）兼容层 —— 兼容 facade（PI-904）。

实现已迁移到 :mod:`chatgpt_web.api.responses_adapter`；本模块保留历史 import
路径（``from chatgpt_web.responses import run_chat`` 等）。
"""

from .api.responses_adapter import (  # noqa: F401
    ResponsesRequest,
    _error_payload,
    _map_exception,
    _maybe_register_edit_markdown,
    _sse,
    _tool_to_chat,
    _usage_dict,
    from_chat_response,
    handle_responses,
    run_chat,
    stream_responses,
    to_chat_request,
)

__all__ = [
    "ResponsesRequest",
    "to_chat_request",
    "from_chat_response",
    "run_chat",
    "handle_responses",
    "stream_responses",
]
