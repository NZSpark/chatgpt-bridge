"""Thin protocol adapters over the shared BridgeEvent model.

Browser/driver code produces :mod:`chatgpt_web.events`; API modules use these
helpers to project the same event sequence into Chat Completions or Responses
shapes without reparsing browser text.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .events import (
    AssistantTextDelta,
    AssistantTextFinal,
    BridgeEvent,
    ToolCall,
    event_final_text,
    event_tool_calls,
)


def completed_text(events: List[BridgeEvent], fallback: str = "") -> str:
    """Return the assistant final text represented by a completed event list."""
    return event_final_text(events) or fallback


def completed_tool_calls(events: List[BridgeEvent]) -> List[Dict[str, Any]]:
    """Return normalized tool calls while preserving the internal call id."""
    return [
        {
            "id": event.tool_call_id,
            "name": event.name,
            "arguments": event.arguments,
        }
        for event in event_tool_calls(events)
    ]


def chat_sse_choice_for_event(
    event: BridgeEvent,
    *,
    tool_index: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Project one BridgeEvent into an OpenAI Chat SSE choice payload.

    This helper deliberately knows only the Chat wire shape; event production
    remains independent of Chat/Responses protocol details.
    """
    if isinstance(event, AssistantTextDelta):
        return {"index": 0, "delta": {"content": event.text}, "finish_reason": None}
    if isinstance(event, AssistantTextFinal):
        return {"index": 0, "delta": {"content": event.text}, "finish_reason": None}
    if isinstance(event, ToolCall) and tool_index is not None:
        return {
            "index": 0,
            "delta": {
                "tool_calls": [
                    {
                        "index": tool_index,
                        "id": event.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": event.name,
                            "arguments": "",
                        },
                    }
                ]
            },
            "finish_reason": None,
        }
    return None


def responses_text_delta(event: AssistantTextDelta, *, item_id: str) -> Dict[str, Any]:
    """Project a text delta into a Responses output_text.delta payload."""
    return {
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "delta": event.text,
    }


def responses_function_call(
    event: ToolCall,
    *,
    item_id: str,
) -> Dict[str, Any]:
    """Create the stable Responses function_call item from one ToolCall."""
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": event.tool_call_id,
        "name": event.name,
        "arguments": "",
        "status": "in_progress",
    }


def responses_function_call_arguments(event: ToolCall) -> str:
    """Serialize a ToolCall's arguments for Responses argument events."""
    import json

    return json.dumps(event.arguments, ensure_ascii=False)


__all__ = [
    "chat_sse_choice_for_event",
    "completed_text",
    "completed_tool_calls",
    "responses_function_call",
    "responses_function_call_arguments",
    "responses_text_delta",
]
