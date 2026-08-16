"""Application observability: structured JSON logs to stdout (ECS → CloudWatch).

LangSmith remains separate (agent/LLM traces). ops_log remains a local demo UI buffer.
This module does not call AWS PutLogEvents — container stdout is the intended path.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

logger = logging.getLogger("setuhaul")

_request_id: ContextVar[str | None] = ContextVar("setuhaul_request_id", default=None)
_LOGGING_CONFIGURED = False

# Fields allowed on domain / request logs (deny-by-default for accidental secrets).
_SAFE_KEYS = frozenset(
    {
        "event",
        "request_id",
        "method",
        "path",
        "status_code",
        "latency_ms",
        "outcome",
        "exception_id",
        "driver_id",
        "shipment_id",
        "hold_id",
        "slot_id",
        "appointment_id",
        "facility_id",
        "run_id",
        "options_count",
        "status",
        "reason",
        "stale",
        "route_ok",
        "human",
        "component",
        "tools_used",
        "escalated",
        "options",
        "client_action",
        "error",
    }
)


class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log line for CloudWatch Logs Insights."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
        }
        event = getattr(record, "setuhaul_event", None)
        fields = getattr(record, "setuhaul_fields", None)
        if event is not None:
            payload["event"] = event
            if isinstance(fields, dict):
                payload.update(fields)
        else:
            payload["event"] = "log"
            payload["message"] = record.getMessage()
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(*, force: bool = False) -> None:
    """Idempotent setup: JSON on stdout for setuhaul; quiet uvicorn access spam."""
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED and not force:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    handler.setLevel(logging.DEBUG)

    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # Prefer our single request-completion line over uvicorn's access log format.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    _LOGGING_CONFIGURED = True


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(request_id: str):
    """Bind request_id for the current context; returns a reset token."""
    return _request_id.set(request_id)


def reset_request_id(token) -> None:
    _request_id.reset(token)


def new_request_id() -> str:
    return str(uuid.uuid4())


def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key not in _SAFE_KEYS:
            continue
        out[key] = value
    return out


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Structured application/domain event → stdout JSON."""
    data = _sanitize(fields)
    rid = get_request_id()
    if rid:
        data.setdefault("request_id", rid)
    logger.log(
        level,
        event,
        extra={"setuhaul_event": event, "setuhaul_fields": data},
    )


def log_request_complete(
    *,
    request_id: str,
    method: str,
    path: str,
    status_code: int,
    latency_ms: float,
    outcome: str,
) -> None:
    log_event(
        "http_request",
        request_id=request_id,
        method=method,
        path=path,
        status_code=status_code,
        latency_ms=round(latency_ms, 1),
        outcome=outcome,
    )


def configure_langsmith_env() -> None:
    """Enable LangChain/LangSmith auto-tracing when key + LANGSMITH_TRACING=true.

    Sets the env vars LangChain reads before Gemini/tool invokes so LLM and
    tool runs appear as a real trace tree (not ad-hoc create_run stubs).
    """
    settings = get_settings()
    if settings.langsmith_api_key and settings.langsmith_api_key.strip() and settings.langsmith_tracing:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        project = settings.langsmith_project or "setuhaul-fde"
        os.environ["LANGCHAIN_PROJECT"] = project
        os.environ["LANGSMITH_PROJECT"] = project
        logger.info("langsmith_tracing_enabled project=%s", project)
    else:
        # Explicitly disable so a prior enable in-process cannot leak into tests/fallback.
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"
        logger.debug(
            "langsmith_tracing_disabled key_present=%s flag=%s",
            bool(settings.langsmith_api_key and settings.langsmith_api_key.strip()),
            settings.langsmith_tracing,
        )


def langsmith_tracing_enabled() -> bool:
    settings = get_settings()
    return bool(
        settings.langsmith_api_key
        and settings.langsmith_api_key.strip()
        and settings.langsmith_tracing
    )


def trace_event(name: str, payload: dict[str, Any] | None = None) -> None:
    """Local structured log helper. Real agent traces come from LangChain auto-tracing."""
    log_event(name, **(payload or {}))
