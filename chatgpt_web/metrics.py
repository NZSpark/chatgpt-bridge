"""Small in-process metrics registry used by the bridge runtime.

The first observability phase intentionally avoids a Prometheus dependency. Metrics
are process-local, bounded, JSON-serializable snapshots suitable for diagnostics and
unit tests; later exporters can consume the same registry without changing call sites.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Dict, Optional

COUNTERS = (
    "request_total",
    "request_success_total",
    "request_error_total",
    "request_timeout_total",
    "request_retry_total",
    "request_context_limit_total",
    "session_recovery_total",
    "session_rotation_total",
    "session_link_navigation_total",
    "browser_selector_miss_total",
    "reply_extraction_failure_total",
    "tool_call_total",
    "tool_parse_failure_total",
    "tool_execution_failure_total",
    "tool_duplicate_total",
)

LATENCIES = (
    "request_latency",
    "browser_generation_latency",
    "reply_extraction_latency",
    "tool_execution_latency",
    "session_recovery_latency",
)


class MetricsRegistry:
    """Thread-safe in-memory counters and duration accumulators."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = defaultdict(int)
        self._latency_totals: Dict[str, float] = defaultdict(float)
        self._latency_counts: Dict[str, int] = defaultdict(int)
        self._started_at = time.monotonic()

    def inc(self, name: str, value: int = 1) -> int:
        if name not in COUNTERS:
            raise ValueError(f"unknown metric counter: {name}")
        with self._lock:
            self._counters[name] += value
            return self._counters[name]

    def observe(self, name: str, duration_s: float) -> None:
        if name not in LATENCIES:
            raise ValueError(f"unknown latency metric: {name}")
        duration_s = max(0.0, float(duration_s))
        with self._lock:
            self._latency_totals[name] += duration_s
            self._latency_counts[name] += 1

    def timer(self, name: str) -> "MetricTimer":
        if name not in LATENCIES:
            raise ValueError(f"unknown latency metric: {name}")
        return MetricTimer(self, name)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counters = {name: int(self._counters[name]) for name in COUNTERS}
            latency = {}
            for name in LATENCIES:
                count = int(self._latency_counts[name])
                total = float(self._latency_totals[name])
                latency[name] = {
                    "count": count,
                    "total_seconds": total,
                    "avg_seconds": total / count if count else 0.0,
                }
            return {
                "counters": counters,
                "latency": latency,
                "uptime_seconds": max(0.0, time.monotonic() - self._started_at),
            }

    def reset(self) -> None:
        """Clear runtime samples; intended for tests and controlled diagnostics."""
        with self._lock:
            self._counters.clear()
            self._latency_totals.clear()
            self._latency_counts.clear()
            self._started_at = time.monotonic()


class MetricTimer:
    """Context manager that records elapsed seconds on exit."""

    def __init__(self, registry: MetricsRegistry, name: str) -> None:
        self.registry = registry
        self.name = name
        self.started_at: Optional[float] = None

    def __enter__(self) -> "MetricTimer":
        self.started_at = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.started_at is not None:
            self.registry.observe(self.name, time.monotonic() - self.started_at)


metrics = MetricsRegistry()
