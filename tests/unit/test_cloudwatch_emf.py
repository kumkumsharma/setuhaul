"""Phase D1: CloudWatch EMF helpers, path grouping, agent latency."""

from __future__ import annotations

import json
import logging
import time
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from app.config import get_settings
from app.services.agent_llm import handle_chat_llm, run_tool_loop
from app.services.observability import (
    CW_METRICS_NAMESPACE,
    JsonLogFormatter,
    configure_logging,
    emit_emf,
    environment_label,
    log_request_complete,
    normalize_path_group,
    reset_request_id,
    set_request_id,
    status_class_for,
)


def _parse_logs(rows: list[dict], event: str) -> list[dict]:
    return [r for r in rows if r.get("event") == event]


def test_normalize_path_group_holds_confirm():
    assert (
        normalize_path_group("/api/allocation/holds/HOLD-ABC123/confirm")
        == "/api/allocation/holds/*/confirm"
    )


def test_normalize_path_group_driver_and_exception():
    assert normalize_path_group("/api/drivers/DRV-027/shipments") == "/api/drivers/*/shipments"
    assert (
        normalize_path_group("/api/allocation/exceptions/EXC-3221F8C326/options")
        == "/api/allocation/exceptions/*/options"
    )
    assert normalize_path_group("/api/chat") == "/api/chat"


def test_status_class_for():
    assert status_class_for(200) == "2xx"
    assert status_class_for(404) == "4xx"
    assert status_class_for(500) == "5xx"


def test_http_emf_payload_structure_and_dimensions(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    get_settings.cache_clear()
    configure_logging(force=True)
    rows: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rows.append(json.loads(self.format(record)))

    handler = Capture()
    handler.setFormatter(JsonLogFormatter())
    log = logging.getLogger("setuhaul")
    log.addHandler(handler)
    try:
        token = set_request_id("req-high-card-should-stay-in-logs")
        try:
            log_request_complete(
                request_id="req-high-card-should-stay-in-logs",
                method="post",
                path="/api/allocation/holds/HOLD-XYZ99/confirm",
                status_code=200,
                latency_ms=42.26,
                outcome="ok",
            )
        finally:
            reset_request_id(token)
    finally:
        log.removeHandler(handler)
        get_settings.cache_clear()

    http = _parse_logs(rows, "http_request")
    assert len(http) == 1
    line = http[0]
    assert line["request_id"] == "req-high-card-should-stay-in-logs"
    assert line["path"] == "/api/allocation/holds/HOLD-XYZ99/confirm"
    assert line["path_group"] == "/api/allocation/holds/*/confirm"
    assert line["RequestCount"] == 1.0
    assert line["RequestLatency"] == 42.3
    assert "RequestErrors" not in line
    aws = line["_aws"]
    assert aws["CloudWatchMetrics"][0]["Namespace"] == CW_METRICS_NAMESPACE
    metric_names = {m["Name"] for m in aws["CloudWatchMetrics"][0]["Metrics"]}
    assert metric_names == {"RequestCount", "RequestLatency"}
    dim_names = set(aws["CloudWatchMetrics"][0]["Dimensions"][0])
    assert dim_names == {"environment", "method", "path_group", "outcome", "status_class"}
    assert "request_id" not in dim_names
    assert "hold_id" not in dim_names
    assert line["environment"] == "staging"
    assert line["method"] == "POST"
    assert line["path_group"] == "/api/allocation/holds/*/confirm"
    assert line["outcome"] == "ok"
    assert line["status_class"] == "2xx"


def test_http_emf_request_errors_on_failure(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    get_settings.cache_clear()
    configure_logging(force=True)
    rows: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rows.append(json.loads(self.format(record)))

    handler = Capture()
    handler.setFormatter(JsonLogFormatter())
    log = logging.getLogger("setuhaul")
    log.addHandler(handler)
    try:
        log_request_complete(
            request_id="err-1",
            method="GET",
            path="/api/missing",
            status_code=404,
            latency_ms=5,
            outcome="failure",
        )
    finally:
        log.removeHandler(handler)
        get_settings.cache_clear()

    line = _parse_logs(rows, "http_request")[0]
    assert line["RequestErrors"] == 1.0
    assert line["status_class"] == "4xx"
    names = {m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert "RequestErrors" in names


def test_emit_emf_strips_forbidden_dimensions():
    configure_logging(force=True)
    rows: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rows.append(json.loads(self.format(record)))

    handler = Capture()
    handler.setFormatter(JsonLogFormatter())
    log = logging.getLogger("setuhaul")
    log.addHandler(handler)
    try:
        emit_emf(
            event="business_metrics",
            metrics=[("HoldsCreated", 1.0, "Count")],
            dimensions={
                "environment": "local",
                "request_id": "should-not-be-dim",
                "exception_id": "EXC-1",
                "driver_id": "DRV-027",
            },
            log_fields={"request_id": "should-not-be-dim"},
        )
    finally:
        log.removeHandler(handler)

    line = rows[-1]
    dim_names = set(line["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0])
    assert dim_names == {"environment"}
    assert line.get("request_id") == "should-not-be-dim"  # still in logs
    assert "request_id" not in dim_names


def test_agent_complete_includes_latency_ms(db_session, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    get_settings.cache_clear()
    configure_logging(force=True)
    rows: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rows.append(json.loads(self.format(record)))

    handler = Capture()
    handler.setFormatter(JsonLogFormatter())
    log = logging.getLogger("setuhaul")
    log.addHandler(handler)

    class Scripted:
        def bind_tools(self, tools):  # noqa: ARG002
            return self

        def invoke(self, messages):  # noqa: ARG002
            time.sleep(0.01)
            return AIMessage(content="Hello from agent")

    try:
        token = set_request_id("agent-lat-1")
        try:
            handle_chat_llm(
                db_session,
                driver_id="DRV-027",
                shipment_id="SHP-1042",
                message="hello",
                model_factory=lambda: Scripted(),
            )
        finally:
            reset_request_id(token)
    finally:
        log.removeHandler(handler)
        get_settings.cache_clear()

    completes = _parse_logs(rows, "agent_llm_complete")
    assert completes
    assert "latency_ms" in completes[-1]
    assert completes[-1]["latency_ms"] >= 10

    agent_metrics = _parse_logs(rows, "agent_metrics")
    assert agent_metrics
    m = agent_metrics[-1]
    assert m["AgentInvocations"] == 1.0
    assert m["AgentLatency"] >= 10
    dim_names = set(m["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0])
    assert "environment" in dim_names
    assert "request_id" not in dim_names
    assert "exception_id" not in dim_names


def test_environment_label_default():
    get_settings.cache_clear()
    assert isinstance(environment_label(), str)
    assert environment_label()
