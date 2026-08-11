"""Unit tests for feasibility and domain behaviour from the PDF."""

from datetime import datetime

from app.models import AppointmentSlot, Shipment
from app.services import domain
from app.services.feasibility import (
    evaluate_slot_for_shipment,
    get_effective_eta,
    list_feasible_slots,
)
from app.services import allocator
from app.services.timeutil import IST, ensure_aware


def test_ravi_original_appointment_infeasible_after_eta(db_session):
    shipment = db_session.get(Shipment, "SHP-1042")
    slot = db_session.get(AppointmentSlot, "SLOT-1938")
    result = evaluate_slot_for_shipment(db_session, shipment, slot)
    assert result.feasible is False
    assert "cannot_reach_before_slot_start" in result.reasons


def test_effective_eta_precedence_declared_over_planned(db_session):
    shipment = domain.get_shipment(db_session, "SHP-1042")
    eta = get_effective_eta(db_session, shipment)
    assert ensure_aware(eta) == datetime(2026, 8, 11, 19, 10, tzinfo=IST)


def test_effective_eta_gate_in_wins(db_session):
    shipment = domain.get_shipment(db_session, "SHP-201")
    eta = get_effective_eta(db_session, shipment)
    assert ensure_aware(eta) == datetime(2026, 8, 11, 17, 5, tzinfo=IST)


def test_repair_delay_not_auto_equal_eta_shift_in_chat(client):
    """PDF: 90-minute repair delay does not equal a 90-minute ETA shift."""
    res = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-027",
            "shipment_id": "SHP-1042",
            "message": "Tyre damaged near Neemrana. Repair may take 90 minutes.",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert "not always the same" in body["reply"].lower() or "provisional" in body["reply"].lower()


def test_feasible_options_for_ravi_after_declared_eta(db_session):
    shipment = domain.get_shipment(db_session, "SHP-1042")
    options = list_feasible_slots(db_session, shipment, limit=10)
    assert options, "Expected later feasible slots for Ravi"
    cutoff = datetime(2026, 8, 11, 19, 10, tzinfo=IST)
    for slot, result in options:
        assert ensure_aware(slot.start_time) >= cutoff
        assert result.feasible is True


def test_vehicle_compatibility_rejects_reefer_slot(db_session):
    shipment = domain.get_shipment(db_session, "SHP-1042")
    slot = db_session.get(AppointmentSlot, "SLOT-REEFER")
    result = evaluate_slot_for_shipment(db_session, shipment, slot)
    assert result.feasible is False


def test_no_feasible_slot_escalates(client):
    res = client.post(
        "/api/chat",
        json={
            "driver_id": "DRV-NOP",
            "shipment_id": "SHP-NOP",
            "message": "I will be late. What slots after 6 PM?",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["escalated"] is True
    assert body["options"] == []
    assert "will not invent" in body["reply"].lower() or "escalat" in body["reply"].lower()


def test_multi_shipment_disambiguation(client):
    res = client.post(
        "/api/chat",
        json={"driver_id": "DRV-MULTI", "message": "I am stuck in traffic and will be late."},
    )
    assert res.status_code == 200
    body = res.json()
    assert len(body["needs_shipment_choice"]) == 2
    assert "more than one" in body["reply"].lower()


def test_shown_is_not_hold(db_session):
    from app.models import DriverException
    from app.services import domain as domain_svc

    exc = domain_svc.create_exception(
        db_session,
        driver_id="DRV-027",
        shipment_id="SHP-1042",
        exception_type="tyre_issue",
        message="late",
    )
    shipment = domain.get_shipment(db_session, "SHP-1042")
    options = allocator.mark_options_shown(db_session, exc, shipment, limit=3)
    assert options
    assert all(o["lifecycle"] in {"shown", "stale"} for o in options)
    # No Redis hold should exist merely because options were shown
    from app.services import redis_client as rc

    for o in options:
        assert rc.get_redis().get(rc.hold_slot_key(o["slot_id"])) is None


def test_confirm_cancel_lifecycle(db_session):
    from app.services import domain as domain_svc

    exc = domain_svc.create_exception(
        db_session, driver_id="DRV-027", shipment_id="SHP-1042", message="need slot after 7"
    )
    shipment = domain.get_shipment(db_session, "SHP-1042")
    options = allocator.mark_options_shown(db_session, exc, shipment, limit=5)
    slot_id = options[0]["slot_id"]

    hold = allocator.create_hold(db_session, exception_id=exc.exception_id, slot_id=slot_id)
    assert hold.status == "held"

    hold, appt = allocator.confirm_hold(db_session, hold.hold_id)
    assert hold.status == "confirmed"
    assert appt.status == "confirmed"

    cancelled = allocator.cancel_appointment(db_session, appt.appointment_id)
    assert cancelled.status == "cancelled"


def test_idempotent_hold(db_session):
    from app.services import domain as domain_svc

    exc = domain_svc.create_exception(
        db_session, driver_id="DRV-027", shipment_id="SHP-1042", message="late"
    )
    shipment = domain.get_shipment(db_session, "SHP-1042")
    options = allocator.mark_options_shown(db_session, exc, shipment, limit=5)
    slot_id = options[0]["slot_id"]
    key = "idem-hold-1"
    h1 = allocator.create_hold(
        db_session, exception_id=exc.exception_id, slot_id=slot_id, idempotency_key=key
    )
    h2 = allocator.create_hold(
        db_session, exception_id=exc.exception_id, slot_id=slot_id, idempotency_key=key
    )
    assert h1.hold_id == h2.hold_id


def test_capacity_reduction_makes_slot_stale(db_session):
    from app.services import domain as domain_svc

    exc = domain_svc.create_exception(
        db_session, driver_id="DRV-027", shipment_id="SHP-1042", message="late"
    )
    shipment = domain.get_shipment(db_session, "SHP-1042")
    options = allocator.mark_options_shown(db_session, exc, shipment, limit=10)
    target = next(o for o in options if o["slot_id"] == "SLOT-EVE-4")
    assert target["lifecycle"] == "shown"

    slot = db_session.get(AppointmentSlot, "SLOT-EVE-4")
    slot.slot_status = "blocked"
    db_session.commit()

    refreshed = allocator.mark_options_shown(db_session, exc, shipment, limit=10)
    assert all(o["slot_id"] != "SLOT-EVE-4" for o in refreshed)


def test_domain_api_shipment_and_appointment(client):
    s = client.get("/api/shipments/SHP-1042")
    assert s.status_code == 200
    assert s.json()["driver_id"] == "DRV-027"
    a = client.get("/api/shipments/SHP-1042/appointment")
    assert a.status_code == 200
    assert a.json()["appointment_id"] == "APT-552"
    assert a.json()["is_feasible"] is False
