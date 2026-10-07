"""把一次上游对话编码成 OpenAI 兼容的 SSE 流 —— 兼容 facade（PI-904）。

实现已迁移到 :mod:`chatgpt_web.api.chat_adapter`；本模块保留历史 import
路径（``from chatgpt_web.streaming import _chunk_text, _stream_chat_completion``）。
"""

from .api.chat_adapter import (  # noqa: F401
    chunk_text as _chunk_text,
    stream_chat_completion as _stream_chat_completion,
)

__all__ = ["_chunk_text", "_stream_chat_completion"]
