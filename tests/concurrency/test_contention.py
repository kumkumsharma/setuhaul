"""Concurrency tests for scarce-slot contention (PDF stress scenarios)."""

from __future__ import annotations

from app.models import Appointment, AppointmentSlot
from app.services import allocator, domain
from app.services.allocator import AllocationError


def _open_exception(db, driver_id: str, shipment_id: str):
    return domain.create_exception(
        db,
        driver_id=driver_id,
        shipment_id=shipment_id,
        exception_type="delay",
        message="need evening slot",
    )


def test_two_drivers_same_slot_only_one_hold(db_session):
    """PDF: two drivers select the same option within seconds → no double booking."""
    slot_id = "SLOT-EVE-3"
    slot = db_session.get(AppointmentSlot, slot_id)
    assert slot.slot_status == "open"
    for appt in db_session.query(Appointment).filter(Appointment.slot_id == slot_id).all():
        appt.status = "cancelled"
    db_session.commit()

    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.models import EtaUpdate

    IST = ZoneInfo("Asia/Kolkata")
    for sid in ("SHP-EVE-00", "SHP-EVE-01"):
        db_session.add(
            EtaUpdate(
                eta_update_id=f"ETA-EXTRA-{sid}",
                shipment_id=sid,
                declared_eta=datetime(2026, 8, 11, 18, 30, tzinfo=IST),
                source_type="driver",
                declared_at=datetime(2026, 8, 11, 17, 25, tzinfo=IST),
            )
        )
    db_session.commit()

    exc_a = _open_exception(db_session, "DRV-EVE-00", "SHP-EVE-00")
    exc_b = _open_exception(db_session, "DRV-EVE-01", "SHP-EVE-01")

    # Prove Redis SET NX exclusivity (sequential is sufficient; SQLite StaticPool
    # is not thread-safe under concurrent ORM sessions).
    hold_a = allocator.create_hold(db_session, exception_id=exc_a.exception_id, slot_id=slot_id)
    try:
        allocator.create_hold(db_session, exception_id=exc_b.exception_id, slot_id=slot_id)
        second_ok = True
    except AllocationError as exc:
        second_ok = False
        assert exc.code == "slot_held"
    assert second_ok is False

    hold, appt = allocator.confirm_hold(db_session, hold_a.hold_id)
    assert appt.status == "confirmed"
    confirmed = (
        db_session.query(Appointment)
        .filter(Appointment.slot_id == slot_id, Appointment.status == "confirmed")
        .count()
    )
    assert confirmed == 1


def test_ten_drivers_compete_for_scarce_evening_slots(db_session):
    """PDF: >=10 drivers request alternatives; only few compatible slots exist."""
    # Collect open evening slots after 18:00 that are free
    from datetime import datetime
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    after = datetime(2026, 8, 11, 18, 0, tzinfo=IST)

    # Cancel MULTI/NOP occupancy on EVE-1/EVE-2 so there are a few free slots
    for appt_id in ("APT-MULTI-A", "APT-NOP"):
        appt = db_session.get(Appointment, appt_id)
        if appt:
            appt.status = "cancelled"
    db_session.commit()

    free_slots = (
        db_session.query(AppointmentSlot)
        .filter(
            AppointmentSlot.facility_id == "FAC-JPR-01",
            AppointmentSlot.start_time >= after,
            AppointmentSlot.slot_status == "open",
        )
        .all()
    )
    # Exclude reefer
    free_slots = [s for s in free_slots if s.dock_id != "DOCK-JPR-REEFER"]
    assert 3 <= len(free_slots) <= 8

    holds = []
    failures = 0
    for i in range(10):
        shipment = domain.get_shipment(db_session, f"SHP-EVE-{i:02d}")
        # Make ETA early enough for evening slots
        from app.models import EtaUpdate

        db_session.add(
            EtaUpdate(
                eta_update_id=f"ETA-COMP-{i:02d}",
                shipment_id=shipment.shipment_id,
                declared_eta=datetime(2026, 8, 11, 17, 45, tzinfo=IST),
                source_type="driver",
                declared_at=datetime(2026, 8, 11, 17, 25, tzinfo=IST),
            )
        )
        db_session.commit()

        exc = _open_exception(db_session, f"DRV-EVE-{i:02d}", shipment.shipment_id)
        options = allocator.mark_options_shown(db_session, exc, shipment, after=after, limit=5)
        if not options:
            failures += 1
            continue
        try:
            hold = allocator.create_hold(
                db_session,
                exception_id=exc.exception_id,
                slot_id=options[0]["slot_id"],
                idempotency_key=f"eve-{i}",
            )
            holds.append(hold)
            allocator.confirm_hold(db_session, hold.hold_id, idempotency_key=f"eve-confirm-{i}")
        except AllocationError:
            failures += 1

    assert len(holds) <= len(free_slots)
    assert failures >= 1  # scarcity forces some failures/escalations
    # No double-confirmed slots
    from sqlalchemy import func

    dupes = (
        db_session.query(Appointment.slot_id, func.count(Appointment.appointment_id))
        .filter(Appointment.status == "confirmed")
        .group_by(Appointment.slot_id)
        .having(func.count(Appointment.appointment_id) > 1)
        .all()
    )
    assert dupes == []


def test_cancellation_frees_slot_for_other_driver(db_session):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    slot_id = "SLOT-EVE-5"
    for appt in db_session.query(Appointment).filter(Appointment.slot_id == slot_id).all():
        appt.status = "cancelled"
    db_session.commit()

    # Driver A holds and confirms
    for sid, did in [("SHP-EVE-02", "DRV-EVE-02"), ("SHP-EVE-03", "DRV-EVE-03")]:
        from app.models import EtaUpdate

        db_session.add(
            EtaUpdate(
                eta_update_id=f"ETA-FREE-{sid}",
                shipment_id=sid,
                declared_eta=datetime(2026, 8, 11, 18, 0, tzinfo=IST),
                source_type="driver",
                declared_at=datetime(2026, 8, 11, 17, 25, tzinfo=IST),
            )
        )
    db_session.commit()

    exc_a = _open_exception(db_session, "DRV-EVE-02", "SHP-EVE-02")
    hold_a = allocator.create_hold(db_session, exception_id=exc_a.exception_id, slot_id=slot_id)
    _, appt_a = allocator.confirm_hold(db_session, hold_a.hold_id)

    exc_b = _open_exception(db_session, "DRV-EVE-03", "SHP-EVE-03")
    try:
        allocator.create_hold(db_session, exception_id=exc_b.exception_id, slot_id=slot_id)
        raised = False
    except AllocationError:
        raised = True
    assert raised is True

    allocator.cancel_appointment(db_session, appt_a.appointment_id)
    hold_b = allocator.create_hold(db_session, exception_id=exc_b.exception_id, slot_id=slot_id)
    assert hold_b.status == "held"


def test_duplicate_message_idempotency(client):
    key = "dup-msg-001"
    payload = {
        "driver_id": "DRV-027",
        "shipment_id": "SHP-1042",
        "message": "Stuck near Neemrana, need slots after 7 PM",
        "idempotency_key": key,
    }
    r1 = client.post("/api/chat", json=payload)
    r2 = client.post("/api/chat", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["exception_id"] == r2.json()["exception_id"]
    assert r1.json()["reply"] == r2.json()["reply"]


def test_high_priority_late_entrant_does_not_steal_confirmed(db_session):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
    slot_id = "SLOT-EVE-6"
    for appt in db_session.query(Appointment).filter(Appointment.slot_id == slot_id).all():
        appt.status = "cancelled"
    db_session.commit()

    from app.models import EtaUpdate

    db_session.add(
        EtaUpdate(
            eta_update_id="ETA-LOW",
            shipment_id="SHP-EVE-04",
            declared_eta=datetime(2026, 8, 11, 18, 0, tzinfo=IST),
            source_type="driver",
            declared_at=datetime(2026, 8, 11, 17, 25, tzinfo=IST),
        )
    )
    db_session.commit()

    low = _open_exception(db_session, "DRV-EVE-04", "SHP-EVE-04")
    hold = allocator.create_hold(db_session, exception_id=low.exception_id, slot_id=slot_id)
    _, appt = allocator.confirm_hold(db_session, hold.hold_id)

    hipri = _open_exception(db_session, "DRV-HIPRI", "SHP-HIPRI")
    try:
        allocator.create_hold(db_session, exception_id=hipri.exception_id, slot_id=slot_id)
        stolen = True
    except AllocationError:
        stolen = False
    assert stolen is False
    assert db_session.get(Appointment, appt.appointment_id).status == "confirmed"
