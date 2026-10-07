"""通用 SSE 事件编码辅助（PI-904）。

目前只有 Responses 的「命名事件」（``event:`` + ``data:``）需要它；Chat
Completions 的裸 ``data:`` chunk 在 :mod:`chatgpt_web.api.chat_adapter` 里。
"""

import json
from typing import Any, Dict


def sse_event(event_type: str, payload: Dict[str, Any]) -> str:
    """命名 SSE 事件：event 名 + data（data 载荷自带 type，Codex 反序列化要求）。"""
    data = dict(payload)
    data["type"] = event_type
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
