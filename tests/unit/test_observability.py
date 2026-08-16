"""Phase B: structured JSON logging + request correlation."""

from __future__ import annotations

import json
import logging

import pytest

from app.services.observability import (
    JsonLogFormatter,
    configure_logging,
    get_request_id,
    log_event,
    reset_request_id,
    set_request_id,
)


@pytest.fixture()
def captured_logs():
    configure_logging(force=True)
    rows: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rows.append(json.loads(self.format(record)))

    handler = Capture()
    handler.setFormatter(JsonLogFormatter())
    handler.setLevel(logging.DEBUG)
    log = logging.getLogger("setuhaul")
    log.addHandler(handler)
    try:
        yield rows
    finally:
        log.removeHandler(handler)


def _events(rows: list[dict], name: str) -> list[dict]:
    return [r for r in rows if r.get("event") == name]


def test_request_id_propagated_and_returned(client, captured_logs):
    rid = "test-req-propagate-001"
    res = client.get("/api/drivers/DRV-027/shipments", headers={"X-Request-Id": rid})
    assert res.status_code == 200
    assert res.headers.get("X-Request-Id") == rid
    http = _events(captured_logs, "http_request")
    assert len(http) == 1
    assert http[0]["request_id"] == rid
    assert http[0]["method"] == "GET"
    assert http[0]["path"] == "/api/drivers/DRV-027/shipments"
    assert http[0]["status_code"] == 200
    assert http[0]["outcome"] == "ok"
    assert "latency_ms" in http[0]


def test_request_id_generated_when_missing(client, captured_logs):
    res = client.get("/api/drivers/DRV-027/shipments")
    assert res.status_code == 200
    rid = res.headers.get("X-Request-Id")
    assert rid
    http = _events(captured_logs, "http_request")
    assert len(http) == 1
    assert http[0]["request_id"] == rid


def test_http_request_log_on_error(client, captured_logs):
    res = client.get("/api/drivers/DRV-DOES-NOT-EXIST/shipments")
    # endpoint may 200 with empty list or 404 depending on impl — force unknown path
    res = client.get("/api/definitely-missing-route-xyz")
    assert res.status_code == 404
    http = _events(captured_logs, "http_request")
    assert http
    last = http[-1]
    assert last["status_code"] == 404
    assert last["outcome"] == "failure"
    assert last["path"] == "/api/definitely-missing-route-xyz"
    assert res.headers.get("X-Request-Id")


def test_health_success_omits_http_request_log(client, captured_logs):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.headers.get("X-Request-Id")
    assert _events(captured_logs, "http_request") == []


def test_domain_lifecycle_exception_and_options(client, captured_logs):
    res = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Need slots after 7 PM",
        },
        headers={"X-Request-Id": "lifecycle-chat-1"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body.get("exception_id")

    opened = _events(captured_logs, "exception_opened")
    assert opened
    assert opened[-1]["exception_id"] == body["exception_id"]
    assert opened[-1]["driver_id"] == "DRV-027"
    assert opened[-1]["shipment_id"] == "SHP-1042"
    assert opened[-1]["request_id"] == "lifecycle-chat-1"

    shown = _events(captured_logs, "options_shown")
    assert shown
    assert shown[-1]["exception_id"] == body["exception_id"]
    assert shown[-1]["request_id"] == "lifecycle-chat-1"
    assert "options_count" in shown[-1]

    http = _events(captured_logs, "http_request")
    assert len(http) == 1
    assert http[0]["path"] == "/api/chat"


def test_hold_created_and_confirmed_logs(client, captured_logs):
    chat = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Need slots after 7 PM",
        },
    ).json()
    assert chat.get("options"), "expected feasible options for hold path"
    slot_id = chat["options"][0]["slot_id"]
    hold_res = client.post(
        "/api/allocation/holds",
        json={"exception_id": chat["exception_id"], "slot_id": slot_id},
        headers={"X-Request-Id": "hold-flow-1"},
    )
    assert hold_res.status_code == 200
    hold = hold_res.json()
    created = _events(captured_logs, "hold_created")
    assert created
    assert created[-1]["hold_id"] == hold["hold_id"]
    assert created[-1]["request_id"] == "hold-flow-1"

    confirm = client.post(
        f"/api/allocation/holds/{hold['hold_id']}/confirm",
        headers={"X-Request-Id": "hold-flow-2"},
    )
    assert confirm.status_code == 200
    confirmed = _events(captured_logs, "hold_confirmed")
    assert confirmed
    assert confirmed[-1]["hold_id"] == hold["hold_id"]
    assert confirmed[-1]["request_id"] == "hold-flow-2"
    assert confirmed[-1].get("appointment_id")


def test_location_declined_lifecycle(client, captured_logs):
    chat = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Running late, stuck in traffic",
        },
    ).json()
    eid = chat["exception_id"]
    res = client.post(
        "/api/location/decline",
        json={"exception_id": eid, "shipment_id": "SHP-1042", "reason": "declined"},
        headers={"X-Request-Id": "loc-decline-1"},
    )
    assert res.status_code == 200
    declined = _events(captured_logs, "location_declined")
    assert declined
    assert declined[-1]["exception_id"] == eid
    assert declined[-1]["request_id"] == "loc-decline-1"


def test_scheduling_run_lifecycle(client, captured_logs):
    res = client.post(
        "/api/scheduling/facilities/FAC-JPR-01/run",
        headers={"X-Request-Id": "sched-1"},
    )
    assert res.status_code == 200
    runs = _events(captured_logs, "scheduling_run")
    assert runs
    assert runs[-1]["facility_id"] == "FAC-JPR-01"
    assert runs[-1]["run_id"]
    assert runs[-1]["request_id"] == "sched-1"


def test_log_event_sanitizes_secrets_and_binds_request_id(captured_logs):
    token = set_request_id("ctx-rid-9")
    try:
        log_event(
            "exception_opened",
            exception_id="EXC-1",
            api_key="should-not-appear",
            authorization="Bearer secret",
            prompt="full prompt text",
        )
    finally:
        reset_request_id(token)
    rows = _events(captured_logs, "exception_opened")
    assert rows
    payload = rows[-1]
    assert payload["request_id"] == "ctx-rid-9"
    assert payload["exception_id"] == "EXC-1"
    assert "api_key" not in payload
    assert "authorization" not in payload
    assert "prompt" not in payload
    assert get_request_id() is None


def test_ops_log_still_separate_from_cloudwatch_note(client):
    res = client.get("/api/metrics/ops")
    assert res.status_code == 200
    note = res.json().get("note", "")
    assert "demo" in note.lower() or "in-process" in note.lower()
    assert "CloudWatch" in note
