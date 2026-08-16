"""In-process ops/log summary for the local demo metrics UI.

Bounded ring buffer of recent API + domain events. Separate from production
CloudWatch logging: structured JSON on stdout is handled by
`app.services.observability` (ECS log driver → CloudWatch Logs).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_LOCK = threading.Lock()
_MAX = 500
_events: deque[dict[str, Any]] = deque(maxlen=_MAX)


def record_event(
    *,
    kind: str,
    path: str | None = None,
    status_code: int | None = None,
    latency_ms: float | None = None,
    outcome: str | None = None,
    detail: str | None = None,
) -> None:
    with _LOCK:
        _events.append(
            {
                "ts": time.time(),
                "kind": kind,
                "path": path,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
                "outcome": outcome,
                "detail": (detail or "")[:240] or None,
            }
        )


def reset_for_tests() -> None:
    with _LOCK:
        _events.clear()


def snapshot(limit: int = 40) -> dict[str, Any]:
    with _LOCK:
        rows = list(_events)

    http = [e for e in rows if e["kind"] == "http"]
    latencies = [e["latency_ms"] for e in http if e.get("latency_ms") is not None]
    failures = [
        e
        for e in http
        if (e.get("status_code") or 0) >= 400
    ]
    domain = [e for e in rows if e["kind"] == "domain"]
    completed = sum(1 for e in domain if e.get("outcome") == "completed")
    escalated = sum(1 for e in domain if e.get("outcome") == "escalated")
    human_help = sum(1 for e in domain if e.get("outcome") == "human_help")
    location_fail = sum(1 for e in domain if e.get("outcome") == "location_failure")

    return {
        "window_events": len(rows),
        "http_requests": len(http),
        "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "p95_latency_ms": (
            round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1)
            if latencies
            else None
        ),
        "failures": len(failures),
        "failure_rate": round(len(failures) / len(http), 3) if http else None,
        "completed_cases": completed,
        "escalated_cases": escalated,
        "human_help_events": human_help,
        "location_failures": location_fail,
        "recent": list(reversed(rows[-limit:])),
        "note": (
            "In-process demo ops buffer (not CloudWatch). "
            "Production app logs are structured JSON on stdout → ECS CloudWatch Logs; "
            "use alongside LangSmith for agent/LLM traces."
        ),
    }
