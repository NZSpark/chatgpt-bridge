"""Explicit request/task lifecycle state machine.

The state machine is intentionally small and independent from Playwright.  It
provides one canonical vocabulary for request progress, retry/recovery, and
terminal outcomes while callers remain responsible for protocol formatting.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional


class TaskStateName(str, Enum):
    RECEIVED = "RECEIVED"
    PROMPT_BUILT = "PROMPT_BUILT"
    MODEL_GENERATING = "MODEL_GENERATING"
    TOOL_CALL_DETECTED = "TOOL_CALL_DETECTED"
    TOOL_EXECUTING = "TOOL_EXECUTING"
    TOOL_RESULT_RETURNED = "TOOL_RESULT_RETURNED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CONTEXT_LIMIT = "CONTEXT_LIMIT"
    UPSTREAM_BUSY = "UPSTREAM_BUSY"
    SESSION_RECOVERY = "SESSION_RECOVERY"


_TERMINAL: FrozenSet[TaskStateName] = frozenset(
    {
        TaskStateName.COMPLETED,
        TaskStateName.FAILED,
        TaskStateName.TIMEOUT,
        TaskStateName.CONTEXT_LIMIT,
        TaskStateName.UPSTREAM_BUSY,
    }
)

_ALLOWED: dict[TaskStateName, FrozenSet[TaskStateName]] = {
    TaskStateName.RECEIVED: frozenset({TaskStateName.PROMPT_BUILT, TaskStateName.FAILED}),
    TaskStateName.PROMPT_BUILT: frozenset(
        {TaskStateName.MODEL_GENERATING, TaskStateName.FAILED}
    ),
    TaskStateName.MODEL_GENERATING: frozenset(
        {
            TaskStateName.TOOL_CALL_DETECTED,
            TaskStateName.COMPLETED,
            TaskStateName.FAILED,
            TaskStateName.TIMEOUT,
            TaskStateName.CONTEXT_LIMIT,
            TaskStateName.UPSTREAM_BUSY,
            TaskStateName.SESSION_RECOVERY,
        }
    ),
    TaskStateName.TOOL_CALL_DETECTED: frozenset(
        {TaskStateName.TOOL_EXECUTING, TaskStateName.FAILED}
    ),
    TaskStateName.TOOL_EXECUTING: frozenset(
        {TaskStateName.TOOL_RESULT_RETURNED, TaskStateName.FAILED}
    ),
    TaskStateName.TOOL_RESULT_RETURNED: frozenset(
        {TaskStateName.MODEL_GENERATING, TaskStateName.FAILED}
    ),
    TaskStateName.SESSION_RECOVERY: frozenset(
        {
            TaskStateName.PROMPT_BUILT,
            TaskStateName.MODEL_GENERATING,
            TaskStateName.CONTEXT_LIMIT,
            TaskStateName.TIMEOUT,
            TaskStateName.FAILED,
        }
    ),
    TaskStateName.COMPLETED: frozenset(),
    TaskStateName.FAILED: frozenset(),
    TaskStateName.TIMEOUT: frozenset(),
    TaskStateName.CONTEXT_LIMIT: frozenset(),
    TaskStateName.UPSTREAM_BUSY: frozenset(),
}


@dataclass
class TaskState:
    """Mutable lifecycle state for one logical bridge request."""

    state: TaskStateName = TaskStateName.RECEIVED
    transition_count: int = 0
    last_error: Optional[str] = None

    def transition(self, target: TaskStateName, *, error: Optional[str] = None) -> None:
        """Move to ``target`` if the transition is explicitly allowed."""
        if target == self.state:
            raise ValueError(f"duplicate task-state transition: {self.state.value}")
        if self.state in _TERMINAL:
            raise ValueError(
                f"terminal task state {self.state.value} cannot transition to {target.value}"
            )
        allowed = _ALLOWED[self.state]
        if target not in allowed:
            raise ValueError(
                f"invalid task-state transition: {self.state.value} -> {target.value}"
            )
        self.state = target
        self.transition_count += 1
        if error is not None:
            self.last_error = error

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def complete(self) -> None:
        self.transition(TaskStateName.COMPLETED)

    def fail(self, error: Optional[str] = None) -> None:
        self.transition(TaskStateName.FAILED, error=error)

    def timeout(self, error: Optional[str] = None) -> None:
        self.transition(TaskStateName.TIMEOUT, error=error)

    def context_limit(self, error: Optional[str] = None) -> None:
        self.transition(TaskStateName.CONTEXT_LIMIT, error=error)

    def upstream_busy(self, error: Optional[str] = None) -> None:
        self.transition(TaskStateName.UPSTREAM_BUSY, error=error)


__all__ = ["TaskState", "TaskStateName"]
