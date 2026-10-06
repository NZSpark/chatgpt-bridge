"""Internal bridge event model shared by Chat and Responses adapters."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class EventKind(str, Enum):
    GENERATION_STARTED = "generation_started"
    ASSISTANT_TEXT_DELTA = "assistant_text_delta"
    ASSISTANT_TEXT_FINAL = "assistant_text_final"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    GENERATION_FINISHED = "generation_finished"
    GENERATION_FAILED = "generation_failed"


@dataclass(frozen=True)
class GenerationStarted:
    kind: EventKind = EventKind.GENERATION_STARTED


@dataclass(frozen=True)
class AssistantTextDelta:
    text: str
    kind: EventKind = EventKind.ASSISTANT_TEXT_DELTA


@dataclass(frozen=True)
class AssistantTextFinal:
    text: str
    kind: EventKind = EventKind.ASSISTANT_TEXT_FINAL


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    name: str
    arguments: Dict[str, Any]
    kind: EventKind = EventKind.TOOL_CALL


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    output: str
    success: bool = True
    error_type: Optional[str] = None
    kind: EventKind = EventKind.TOOL_RESULT


@dataclass(frozen=True)
class GenerationFinished:
    kind: EventKind = EventKind.GENERATION_FINISHED


@dataclass(frozen=True)
class GenerationFailed:
    message: str
    error_type: str = "server_error"
    kind: EventKind = EventKind.GENERATION_FAILED


BridgeEvent = Union[
    GenerationStarted,
    AssistantTextDelta,
    AssistantTextFinal,
    ToolCall,
    ToolResult,
    GenerationFinished,
    GenerationFailed,
]


def completion_events(
    reply: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> List[BridgeEvent]:
    """Normalize one completed browser response into internal bridge events."""
    events: List[BridgeEvent] = [GenerationStarted()]
    calls = tool_calls or []
    if calls:
        for index, call in enumerate(calls):
            events.append(
                ToolCall(
                    tool_call_id=str(call.get("id") or f"call_{index}"),
                    name=str(call["name"]),
                    arguments=dict(call.get("arguments") or {}),
                )
            )
    else:
        events.append(AssistantTextFinal(reply))
    events.append(GenerationFinished())
    return events


def event_tool_calls(events: List[BridgeEvent]) -> List[ToolCall]:
    return [event for event in events if isinstance(event, ToolCall)]


def event_final_text(events: List[BridgeEvent]) -> Optional[str]:
    for event in events:
        if isinstance(event, AssistantTextFinal):
            return event.text
    return None


__all__ = [
    "AssistantTextDelta",
    "AssistantTextFinal",
    "BridgeEvent",
    "EventKind",
    "GenerationFailed",
    "GenerationFinished",
    "GenerationStarted",
    "ToolCall",
    "ToolResult",
    "completion_events",
    "event_final_text",
    "event_tool_calls",
]
