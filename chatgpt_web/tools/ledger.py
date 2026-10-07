"""工具执行台账：会话隔离的去重、记录与结果哈希（PI-901-6）。

``ToolExecutionLedger`` 以 ``(session_key, tool_call_id)`` 为键，保证：

* 同一会话内同一个 ``tool_call_id`` 只会真正执行一次；
* 并发到达的重复调用会等待首个执行完成并复用其结果；
* 不同会话之间互不影响（session 隔离）。
"""

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True, slots=True)
class ToolExecutionRecord:
    """Audit record and cached result for one session-scoped tool execution."""

    session_key: str
    tool_call_id: str
    tool_name: str
    normalized_arguments: Dict[str, Any]
    duration_ms: float
    success: bool
    error_type: Optional[str]
    result_hash: Optional[str]
    result: Optional[Dict[str, Any]]


class ToolExecutionLedger:
    """In-memory execution ledger keyed by ``(session_key, tool_call_id)``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[tuple[str, str], ToolExecutionRecord] = {}
        self._inflight: Dict[tuple[str, str], threading.Event] = {}

    @staticmethod
    def _key(session_key: Optional[str], tool_call_id: str) -> tuple[str, str]:
        return (session_key or "default", tool_call_id)

    @staticmethod
    def _hash_result(result: Optional[Dict[str, Any]]) -> Optional[str]:
        if result is None:
            return None
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def get(self, session_key: Optional[str], tool_call_id: str) -> Optional[ToolExecutionRecord]:
        key = self._key(session_key, tool_call_id)
        with self._lock:
            return self._records.get(key)

    def claim(self, session_key: Optional[str], tool_call_id: str) -> tuple[Optional[ToolExecutionRecord], threading.Event, bool]:
        """Claim an execution slot, returning (record, event, owner)."""
        key = self._key(session_key, tool_call_id)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                event = threading.Event()
                event.set()
                return existing, event, False
            pending = self._inflight.get(key)
            if pending is not None:
                return None, pending, False
            event = threading.Event()
            self._inflight[key] = event
            return None, event, True

    def complete(
        self,
        *,
        session_key: Optional[str],
        tool_call_id: str,
        record: ToolExecutionRecord,
    ) -> ToolExecutionRecord:
        key = self._key(session_key, tool_call_id)
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                self._records[key] = record
                existing = record
            event = self._inflight.pop(key, None)
            if event is not None:
                event.set()
            return existing

    def record(
        self,
        *,
        session_key: Optional[str],
        tool_call_id: str,
        tool_name: str,
        normalized_arguments: Dict[str, Any],
        started_at: float,
        success: bool,
        error_type: Optional[str],
        result: Optional[Dict[str, Any]],
    ) -> ToolExecutionRecord:
        key = self._key(session_key, tool_call_id)
        record = ToolExecutionRecord(
            session_key=key[0],
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            normalized_arguments=json.loads(json.dumps(normalized_arguments, ensure_ascii=False, sort_keys=True)),
            duration_ms=round((time.monotonic() - started_at) * 1000, 3),
            success=success,
            error_type=error_type,
            result_hash=self._hash_result(result),
            result=json.loads(json.dumps(result, ensure_ascii=False)) if result is not None else None,
        )
        return self.complete(
            session_key=session_key,
            tool_call_id=tool_call_id,
            record=record,
        )

    def snapshot(self) -> List[ToolExecutionRecord]:
        with self._lock:
            return list(self._records.values())


TOOL_EXECUTION_LEDGER = ToolExecutionLedger()
