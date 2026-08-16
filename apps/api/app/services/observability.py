"""Application observability: structured JSON logs to stdout (ECS → CloudWatch).

LangSmith remains separate (agent/LLM traces). ops_log remains a local demo UI buffer.
This module does not call AWS PutLogEvents — container stdout is the intended path.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
        "path_group",
        "status_code",
        "status_class",
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
        "environment",
        "provider",
    }
)

# Low-cardinality CloudWatch metric dimensions only (never per-request/entity IDs).
_METRIC_DIM_KEYS = frozenset(
    {"environment", "provider", "method", "path_group", "outcome", "status_class"}
)
_FORBIDDEN_METRIC_DIM_KEYS = frozenset(
    {
        "request_id",
        "driver_id",
        "shipment_id",
        "exception_id",
        "hold_id",
        "slot_id",
        "appointment_id",
        "facility_id",
        "run_id",
        "path",  # use path_group only
    }
)

CW_METRICS_NAMESPACE = "SetuHaul/API"

_ID_SEGMENT_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?:EXC|HOLD|SHP|DRV|FAC|SLOT|APT|MET|RUN|VIEW|LOC|ETA|MSG|DOCK|RULE|VEH|CONTACT|BASE)"
    r"-[A-Za-z0-9]+"
    r"|[A-Z]{2,}[-_][A-Za-z0-9]+"
    r")$",
    re.IGNORECASE,
)


class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log line for CloudWatch Logs Insights (+ optional EMF)."""

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
        emf = getattr(record, "setuhaul_emf", None)
        if isinstance(emf, dict):
            # EMF properties (_aws, metric values, dimension keys) sit alongside log fields.
            payload.update(emf)
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


def environment_label() -> str:
    try:
        return (get_settings().app_env or "local").strip() or "local"
    except Exception:  # noqa: BLE001
        return "local"


def status_class_for(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "2xx"
    if 400 <= status_code < 500:
        return "4xx"
    if 500 <= status_code < 600:
        return "5xx"
    return "other"


def normalize_path_group(path: str) -> str:
    """Replace dynamic ID path segments with '*' for low-cardinality metrics."""
    if not path:
        return "/"
    raw = path.split("?")[0]
    parts = raw.split("/")
    out: list[str] = []
    for part in parts:
        if part == "":
            out.append(part)
            continue
        if _ID_SEGMENT_RE.match(part):
            out.append("*")
        else:
            out.append(part)
    grouped = "/".join(out)
    if not grouped.startswith("/"):
        grouped = "/" + grouped
    # Collapse accidental duplicate slashes except root
    while "//" in grouped:
        grouped = grouped.replace("//", "/")
    return grouped if grouped else "/"


def _metric_dimensions(dimensions: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in dimensions.items():
        if value is None or value == "":
            continue
        if key in _FORBIDDEN_METRIC_DIM_KEYS:
            continue
        if key not in _METRIC_DIM_KEYS:
            continue
        out[key] = str(value)
    return out


def emit_emf(
    *,
    event: str,
    metrics: list[tuple[str, float, str]],
    dimensions: dict[str, Any],
    log_fields: dict[str, Any] | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit one readable structured log line that also carries CloudWatch EMF.

    Fail-open: never raises to callers. Does not call PutMetricData.
    High-cardinality IDs may appear in log_fields only — never as metric dimensions.
    """
    try:
        dims = _metric_dimensions(dimensions)
        metric_defs = [{"Name": name, "Unit": unit} for name, _value, unit in metrics]
        # Stable dimension set order for CloudWatch.
        dim_names = sorted(dims.keys())
        emf_block: dict[str, Any] = {
            **dims,
            **{name: value for name, value, _unit in metrics},
            "_aws": {
                "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": CW_METRICS_NAMESPACE,
                        "Dimensions": [dim_names] if dim_names else [[]],
                        "Metrics": metric_defs,
                    }
                ],
            },
        }
        data = _sanitize(log_fields or {})
        rid = get_request_id()
        if rid:
            data.setdefault("request_id", rid)
        logger.log(
            level,
            event,
            extra={
                "setuhaul_event": event,
                "setuhaul_fields": data,
                "setuhaul_emf": emf_block,
            },
        )
    except Exception:  # noqa: BLE001
        try:
            # Fall back to plain structured log without metrics.
            log_event(event, **(log_fields or {}))
        except Exception:  # noqa: BLE001
            pass


def log_request_complete(
    *,
    request_id: str,
    method: str,
    path: str,
    status_code: int,
    latency_ms: float,
    outcome: str,
) -> None:
    """Phase B http_request log + RequestCount/RequestErrors/RequestLatency EMF."""
    path_group = normalize_path_group(path)
    latency = round(latency_ms, 1)
    status_class = status_class_for(status_code)
    metrics: list[tuple[str, float, str]] = [
        ("RequestCount", 1.0, "Count"),
        ("RequestLatency", latency, "Milliseconds"),
    ]
    if outcome == "failure" or status_code >= 400:
        metrics.append(("RequestErrors", 1.0, "Count"))
    emit_emf(
        event="http_request",
        metrics=metrics,
        dimensions={
            "environment": environment_label(),
            "method": (method or "GET").upper(),
            "path_group": path_group,
            "outcome": outcome,
            "status_class": status_class,
        },
        log_fields={
            "request_id": request_id,
            "method": (method or "GET").upper(),
            "path": path,
            "path_group": path_group,
            "status_code": status_code,
            "status_class": status_class,
            "latency_ms": latency,
            "outcome": outcome,
            "environment": environment_label(),
        },
    )


def emit_agent_invocation_metrics(*, latency_ms: float, provider: str | None = None) -> None:
    prov = provider or resolve_llm_provider_model()[0]
    dims: dict[str, Any] = {"environment": environment_label()}
    if prov and prov != "none":
        dims["provider"] = prov
    emit_emf(
        event="agent_metrics",
        metrics=[
            ("AgentInvocations", 1.0, "Count"),
            ("AgentLatency", round(latency_ms, 1), "Milliseconds"),
        ],
        dimensions=dims,
        log_fields={
            "environment": environment_label(),
            "provider": prov if prov != "none" else None,
            "latency_ms": round(latency_ms, 1),
        },
    )


def emit_agent_error_metric(*, provider: str | None = None) -> None:
    prov = provider or resolve_llm_provider_model()[0]
    dims: dict[str, Any] = {"environment": environment_label()}
    if prov and prov != "none":
        dims["provider"] = prov
    emit_emf(
        event="agent_metrics",
        metrics=[("AgentErrors", 1.0, "Count")],
        dimensions=dims,
        log_fields={
            "environment": environment_label(),
            "provider": prov if prov != "none" else None,
            "outcome": "failure",
        },
    )


def emit_business_count_metric(metric_name: str) -> None:
    """HoldsCreated / HoldsConfirmed / Escalations — environment dimension only."""
    emit_emf(
        event="business_metrics",
        metrics=[(metric_name, 1.0, "Count")],
        dimensions={"environment": environment_label()},
        log_fields={"environment": environment_label()},
    )


_LANGSMITH_CONFIGURED = False


def resolve_llm_provider_model() -> tuple[str, str]:
    """Mirror _build_model selection without constructing a client (openrouter > gemini)."""
    settings = get_settings()
    if settings.openrouter_api_key and settings.openrouter_api_key.strip():
        return "openrouter", settings.openrouter_model
    if settings.gemini_api_key and settings.gemini_api_key.strip():
        return "gemini", settings.gemini_model
    return "none", ""


def build_langsmith_run_metadata(
    *,
    driver_id: str | None = None,
    exception_id: str | None = None,
    shipment_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Filterable root metadata for setuhaul_driver_agent (no secrets / prompts / PII blobs)."""
    settings = get_settings()
    meta: dict[str, Any] = {}
    rid = get_request_id()
    if rid:
        meta["request_id"] = rid
    if driver_id:
        meta["driver_id"] = driver_id
    if exception_id:
        meta["exception_id"] = exception_id
    if shipment_id:
        meta["shipment_id"] = shipment_id
    if provider:
        meta["provider"] = provider
    if model:
        meta["model"] = model
    env = (settings.app_env or "local").strip() or "local"
    meta["environment"] = env
    sha = (settings.git_sha or "").strip()
    if sha:
        meta["git_sha"] = sha
    return meta


def build_langsmith_run_tags(
    *,
    provider: str | None = None,
    environment: str | None = None,
) -> list[str]:
    """Stable, low-cardinality tags for LangSmith filtering."""
    settings = get_settings()
    env = (environment or settings.app_env or "local").strip() or "local"
    tags = ["setuhaul", "agent", env]
    if provider and provider != "none":
        tags.append(provider)
    # De-dupe while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def configure_langsmith_env(*, force: bool = False) -> None:
    """Enable LangChain/LangSmith auto-tracing when key + LANGSMITH_TRACING=true.

    Sets the env vars LangChain reads before model/tool invokes so LLM and
    tool runs appear as a real trace tree (not ad-hoc create_run stubs).
    Fail-open: never raises to the caller.
    """
    global _LANGSMITH_CONFIGURED
    try:
        settings = get_settings()
        if settings.langsmith_api_key and settings.langsmith_api_key.strip() and settings.langsmith_tracing:
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
            os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
            project = settings.langsmith_project or "setuhaul-fde"
            os.environ["LANGCHAIN_PROJECT"] = project
            os.environ["LANGSMITH_PROJECT"] = project
            if force or not _LANGSMITH_CONFIGURED:
                # Never log the API key — project name only.
                logger.info("langsmith_tracing_enabled project=%s", project)
            _LANGSMITH_CONFIGURED = True
        else:
            # Explicitly disable so a prior enable in-process cannot leak into tests/fallback.
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            os.environ["LANGSMITH_TRACING"] = "false"
            if force or not _LANGSMITH_CONFIGURED:
                logger.debug(
                    "langsmith_tracing_disabled key_present=%s flag=%s",
                    bool(settings.langsmith_api_key and settings.langsmith_api_key.strip()),
                    settings.langsmith_tracing,
                )
            _LANGSMITH_CONFIGURED = True
    except Exception:  # noqa: BLE001
        # Tracing must never break application startup or chat.
        try:
            os.environ["LANGCHAIN_TRACING_V2"] = "false"
            os.environ["LANGSMITH_TRACING"] = "false"
        except Exception:  # noqa: BLE001
            pass


def langsmith_tracing_enabled() -> bool:
    try:
        settings = get_settings()
        return bool(
            settings.langsmith_api_key
            and settings.langsmith_api_key.strip()
            and settings.langsmith_tracing
        )
    except Exception:  # noqa: BLE001
        return False


def trace_event(name: str, payload: dict[str, Any] | None = None) -> None:
    """Local structured log helper. Real agent traces come from LangChain auto-tracing."""
    log_event(name, **(payload or {}))
