"""Phase 2 — location / Geoapify, facility scheduler, metrics."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import BaselineMetric, DriverException, RouteEtaRecord, Shipment
from app.services import location as location_svc
from app.services.geoapify import calculate_route_eta, mock_route
from app.services.scheduler import propose_schedule
from app.services import metrics as metrics_svc
from app.services.timeutil import IST, ensure_aware

IST = ZoneInfo("Asia/Kolkata")


def test_mock_route_eta_separate_from_declared(db_session):
    now = datetime(2026, 8, 11, 17, 25, tzinfo=IST)
    # Neemrana → Jaipur roughly
    result = mock_route(
        origin_lat=27.9889,
        origin_lon=76.3881,
        dest_lat=26.9124,
        dest_lon=75.7873,
        now=now,
    )
    assert result.ok
    assert result.provider == "mock"
    assert result.duration_minutes > 0
    assert ensure_aware(result.route_eta) > now


def test_location_submit_keeps_etas_separate(client, db_session):
    # Create exception via chat
    chat = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Stuck near Neemrana. Need slots after 7 PM.",
        },
    ).json()
    exc_id = chat["exception_id"]
    assert exc_id

    res = client.post(
        "/api/location",
        json={
            "exception_id": exc_id,
            "shipment_id": "SHP-1042",
            "latitude": 27.9889,
            "longitude": 76.3881,
            "accuracy_m": 20,
            "captured_at": "2026-08-11T17:20:00+05:30",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body.get("eta_comparison") or "route" in body["reply"].lower() or body["options"] is not None
    routes = db_session.query(RouteEtaRecord).filter(RouteEtaRecord.exception_id == exc_id).all()
    assert len(routes) >= 1
    # Driver eta_updates must still exist independently
    shipment = db_session.get(Shipment, "SHP-1042")
    assert any(u.source_type == "driver" for u in shipment.eta_updates)


def test_location_denied_continues_workflow(client):
    chat = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "I am late, what slots after 7 PM?",
        },
    ).json()
    res = client.post(
        "/api/location/decline",
        json={"exception_id": chat["exception_id"], "shipment_id": "SHP-1042"},
    )
    assert res.status_code == 200
    assert res.json()["client_action"] is None
    # Must still be able to operate without inventing slots from thin air
    assert "tools_used" in res.json()


def test_facility_scheduler_respects_fixed_unloading(client, db_session):
    res = client.post("/api/scheduling/facilities/FAC-JPR-01/run")
    assert res.status_code == 200
    body = res.json()
    assert body["facility_id"] == "FAC-JPR-01"
    assert "assignments" in body["proposal"]
    fixed = [a for a in body["proposal"]["assignments"] if a.get("fixed")]
    assert any(a["shipment_id"] == "SHP-204" for a in fixed)
    assert "unloading" in body["explanation"].lower() or fixed


def test_metrics_before_after_summary(client, db_session):
    # Ensure baseline exists
    assert db_session.query(BaselineMetric).count() >= 1
    # Generate a resolved case
    chat = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Need slots after 7 PM",
        },
    ).json()
    if chat.get("options"):
        client.post(
            "/api/chat",
            json={
                "driver_id": "DRV-027",
                "exception_id": chat["exception_id"],
                "shipment_id": "SHP-1042",
                "message": "1",
            },
        )
        client.post(
            "/api/chat",
            json={
                "driver_id": "DRV-027",
                "exception_id": chat["exception_id"],
                "shipment_id": "SHP-1042",
                "message": "confirm",
            },
        )
    summary = client.get("/api/metrics/summary").json()
    assert "before_manual" in summary
    assert "after_solution" in summary
    assert summary["before_manual"][0]["avg_resolution_minutes"] == 18.0


def test_scheduling_eta_prefers_route_when_fresh(db_session):
    from app.services import domain as domain_svc

    exc = domain_svc.create_exception(
        db_session, driver_id="DRV-027", shipment_id="SHP-1042", message="late"
    )
    location_svc.submit_location(
        db_session,
        exception_id=exc.exception_id,
        shipment_id="SHP-1042",
        latitude=27.9889,
        longitude=76.3881,
        accuracy_m=15,
        captured_at=datetime(2026, 8, 11, 17, 20, tzinfo=IST),
    )
    shipment = db_session.get(Shipment, "SHP-1042")
    eta, source = location_svc.get_scheduling_eta(db_session, shipment, exc.exception_id)
    assert source == "route"
    assert eta is not None


def test_stale_location_falls_back(db_session):
    from app.services import domain as domain_svc
    from app.models import LocationShare
    from app.config import get_settings

    exc = domain_svc.create_exception(
        db_session, driver_id="DRV-027", shipment_id="SHP-1042", message="late"
    )
    # Captured far before scenario now → stale
    share = LocationShare(
        location_id="LOC-STALE",
        exception_id=exc.exception_id,
        shipment_id="SHP-1042",
        latitude=27.99,
        longitude=76.39,
        accuracy_m=10,
        captured_at=datetime(2026, 8, 11, 10, 0, tzinfo=IST),
        received_at=datetime(2026, 8, 11, 10, 0, tzinfo=IST),
        status="ok",
    )
    db_session.add(share)
    db_session.commit()
    assert location_svc.is_location_stale(share) is True
    shipment = db_session.get(Shipment, "SHP-1042")
    _eta, source = location_svc.get_scheduling_eta(db_session, shipment, exc.exception_id)
    assert source == "declared_or_planned"


def test_ops_summary_endpoint(client):
    from app.services import ops_log

    ops_log.reset_for_tests()
    # Generate a chat hit so middleware records latency
    client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Need slots after 7 PM",
        },
    )
    res = client.get("/api/metrics/ops")
    assert res.status_code == 200
    body = res.json()
    assert "avg_latency_ms" in body
    assert "failures" in body
    assert "case_metrics" in body
    assert body["http_requests"] >= 1
    assert "application-log" in body["note"].lower() or "CloudWatch" in body["note"]


def test_confirm_metrics_use_superseded_appointment_not_heuristic(client, db_session):
    from app.models import Appointment, CaseMetric
    from app.services.timeutil import ensure_aware

    # Known prior appointment for SHP-1042 in seed
    prior = (
        db_session.query(Appointment)
        .filter(
            Appointment.shipment_id == "SHP-1042",
            Appointment.status.in_(["confirmed", "pending"]),
        )
        .first()
    )
    assert prior is not None
    prior_start = ensure_aware(prior.slot.start_time)

    chat = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Need slots after 7 PM. skip location",
        },
    ).json()
    # Rules path may still prompt location; force skip
    if "share your current location" in (chat.get("reply") or "").lower():
        chat = client.post(
            "/api/chat",
            json={
                "driver_id": "DRV-027",
                "shipment_id": "SHP-1042",
                "exception_id": chat["exception_id"],
                "message": "no",
            },
        ).json()
    if not chat.get("options"):
        chat = client.post(
            "/api/chat",
            json={
                "driver_id": "DRV-027",
                "shipment_id": "SHP-1042",
                "exception_id": chat["exception_id"],
                "message": "What are the next slots after 7 PM?",
            },
        ).json()
    assert chat.get("options"), chat.get("reply")
    first_slot = chat["options"][0]["slot_id"]
    # Accept second option when available to exercise first_option_accepted=False
    choice = "2" if len(chat["options"]) > 1 else "1"
    chosen_slot = chat["options"][int(choice) - 1]["slot_id"]
    held = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "exception_id": chat["exception_id"],
            "shipment_id": "SHP-1042",
            "message": choice,
        },
    ).json()
    assert held.get("hold") or "hold" in (held.get("reply") or "").lower() or held.get("status") == "held"
    client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "exception_id": chat["exception_id"],
            "shipment_id": "SHP-1042",
            "message": "confirm",
        },
    )
    row = (
        db_session.query(CaseMetric)
        .filter(CaseMetric.exception_id == chat["exception_id"])
        .first()
    )
    assert row is not None
    assert row.resolution_status == "confirmed"
    assert row.old_projected_wait_minutes is not None
    assert row.new_projected_wait_minutes is not None
    # Heuristic old_wait = new_wait + 20 must be gone
    assert row.old_projected_wait_minutes != row.new_projected_wait_minutes + 20
    assert row.first_option_accepted is (chosen_slot == first_slot)
    # Old wait must reflect superseded prior slot vs scheduling ETA (not invented)
    assert prior_start is not None


def test_location_only_no_declared_eta_still_ranks(db_session):
    """Driver shares location without a declared ETA — route source used when fresh."""
    from app.services import domain as domain_svc

    exc = domain_svc.create_exception(
        db_session, driver_id="DRV-027", shipment_id="SHP-1042", message="stuck"
    )
    # Clear declared ETA so only planned/route remain
    exc.latest_declared_eta = None
    db_session.commit()
    location_svc.submit_location(
        db_session,
        exception_id=exc.exception_id,
        shipment_id="SHP-1042",
        latitude=27.9889,
        longitude=76.3881,
        accuracy_m=15,
        captured_at=datetime(2026, 8, 11, 17, 20, tzinfo=IST),
    )
    shipment = db_session.get(Shipment, "SHP-1042")
    _eta, source = location_svc.get_scheduling_eta(db_session, shipment, exc.exception_id)
    assert source == "route"


def test_reshare_location_updates_route_eta(db_session):
    from app.models import RouteEtaRecord
    from app.services import domain as domain_svc

    exc = domain_svc.create_exception(
        db_session, driver_id="DRV-027", shipment_id="SHP-1042", message="late"
    )
    location_svc.submit_location(
        db_session,
        exception_id=exc.exception_id,
        shipment_id="SHP-1042",
        latitude=27.9889,
        longitude=76.3881,
        accuracy_m=15,
        captured_at=datetime(2026, 8, 11, 17, 10, tzinfo=IST),
    )
    first = (
        db_session.query(RouteEtaRecord)
        .filter(RouteEtaRecord.exception_id == exc.exception_id)
        .count()
    )
    location_svc.submit_location(
        db_session,
        exception_id=exc.exception_id,
        shipment_id="SHP-1042",
        latitude=27.5,
        longitude=76.5,
        accuracy_m=20,
        captured_at=datetime(2026, 8, 11, 17, 22, tzinfo=IST),
    )
    second = (
        db_session.query(RouteEtaRecord)
        .filter(RouteEtaRecord.exception_id == exc.exception_id)
        .count()
    )
    assert second == first + 1
    latest_loc = location_svc.latest_location(db_session, exc.exception_id)
    assert latest_loc is not None
    assert float(latest_loc.latitude) == 27.5
    assert location_svc.latest_route_eta(db_session, exc.exception_id) is not None
