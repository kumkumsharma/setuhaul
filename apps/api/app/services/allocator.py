"""Allocation engine: Redis short-TTL holds, idempotency, confirm/cancel.

Capacity truth is enforced here with Redis SET NX + DB transactions.
The LLM / chat layer must only call these APIs — never invent availability.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Appointment, AppointmentSlot, DriverException, OptionView, SlotHold
from app.services import redis_client as rc
from app.services.feasibility import evaluate_slot_for_shipment, list_feasible_slots
from app.models import Shipment
from app.services.timeutil import ensure_aware


class AllocationError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _now() -> datetime:
    return ensure_aware(get_settings().now())  # type: ignore[return-value]


def get_idempotent_result(key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    raw = rc.get_redis().get(rc.idem_key(key))
    return rc.loads(raw)


def store_idempotent_result(key: str | None, payload: dict[str, Any]) -> None:
    if not key:
        return
    settings = get_settings()
    rc.get_redis().setex(
        rc.idem_key(key),
        settings.idempotency_ttl_seconds,
        rc.dumps(payload),
    )


def mark_options_shown(
    db: Session,
    exception: DriverException,
    shipment: Shipment,
    *,
    after: datetime | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Compute feasible options from the engine and persist explicit 'shown' views."""
    now = _now()
    # Stale any previous shown options for this exception
    prior = (
        db.query(OptionView)
        .filter(OptionView.exception_id == exception.exception_id, OptionView.status == "shown")
        .all()
    )
    for view in prior:
        view.status = "stale"

    # Phase 2: optional route ETA for ranking/buffers only (declared ETA rows unchanged)
    from app.services.location import get_scheduling_eta

    sched_eta, eta_source = get_scheduling_eta(db, shipment, exception.exception_id, now=now)
    feasible = list_feasible_slots(
        db, shipment, after=after or sched_eta, limit=limit * 2, now=now, release_eta=sched_eta
    )
    redis = rc.get_redis()
    options: list[dict[str, Any]] = []

    for rank, (slot, result) in enumerate(feasible[:limit], start=1):
        # If another exception currently holds this slot, surface as unavailable to claim
        holder = redis.get(rc.hold_slot_key(slot.slot_id))
        lifecycle = "shown"
        reasons = list(result.reasons)
        if holder and holder != exception.exception_id:
            lifecycle = "stale"
            reasons.append("held_by_another_request")
        reasons.append(f"eta_source:{eta_source}")

        view = OptionView(
            view_id=_new_id("VIEW"),
            exception_id=exception.exception_id,
            slot_id=slot.slot_id,
            shown_at=now,
            rank=rank,
            status="shown" if lifecycle == "shown" else "stale",
            reason_json=rc.dumps(
                {
                    "reasons": reasons,
                    "buffer_minutes": result.buffer_minutes,
                    "eta_source": eta_source,
                }
            ),
        )
        db.add(view)
        options.append(
            {
                "slot_id": slot.slot_id,
                "facility_id": slot.facility_id,
                "dock_id": slot.dock_id,
                "dock_name": slot.dock.dock_name if slot.dock else slot.dock_id,
                "start_time": ensure_aware(slot.start_time),
                "end_time": ensure_aware(slot.end_time),
                "rank": rank,
                "lifecycle": lifecycle,
                "reasons": reasons,
                "buffer_minutes": result.buffer_minutes,
                "eta_source": eta_source,
            }
        )

    exception.status = "awaiting_choice" if options else "escalated"
    db.commit()
    try:
        from app.services import metrics as metrics_svc

        metrics_svc.mark_options_generated(db, exception.exception_id)
    except Exception:  # noqa: BLE001
        pass
    from app.services.observability import log_event

    shown = sum(1 for o in options if o.get("lifecycle") == "shown")
    log_event(
        "options_shown",
        exception_id=exception.exception_id,
        driver_id=exception.driver_id,
        shipment_id=shipment.shipment_id,
        options_count=shown,
        status=exception.status,
    )
    return options


def create_hold(
    db: Session,
    *,
    exception_id: str,
    slot_id: str,
    idempotency_key: str | None = None,
) -> SlotHold:
    cached = get_idempotent_result(idempotency_key)
    if cached and cached.get("hold_id"):
        existing = db.get(SlotHold, cached["hold_id"])
        if existing:
            return existing

    exception = db.get(DriverException, exception_id)
    if not exception:
        raise AllocationError("exception_not_found", "Exception not found")
    shipment = db.get(Shipment, exception.shipment_id)
    slot = (
        db.query(AppointmentSlot)
        .options(joinedload(AppointmentSlot.dock))
        .filter(AppointmentSlot.slot_id == slot_id)
        .first()
    )
    if not shipment or not slot:
        raise AllocationError("not_found", "Shipment or slot not found")

    now = _now()
    from app.services.location import get_scheduling_eta

    sched_eta, _src = get_scheduling_eta(db, shipment, exception_id, now=now)
    feasibility = evaluate_slot_for_shipment(
        db, shipment, slot, now=now, release_eta=sched_eta
    )
    if not feasibility.feasible:
        raise AllocationError(
            "infeasible",
            f"Slot is not feasible: {', '.join(feasibility.reasons)}",
        )

    settings = get_settings()
    redis = rc.get_redis()
    hold_id = _new_id("HOLD")
    expires_at = now + timedelta(seconds=settings.hold_ttl_seconds)

    # Atomic exclusive claim
    claimed = redis.set(
        rc.hold_slot_key(slot_id),
        exception_id,
        nx=True,
        ex=settings.hold_ttl_seconds,
    )
    if not claimed:
        current = redis.get(rc.hold_slot_key(slot_id))
        if current == exception_id:
            # Refresh existing hold for same exception
            prev_hold_id = redis.get(rc.exception_hold_key(exception_id))
            if prev_hold_id:
                prev = db.get(SlotHold, prev_hold_id)
                if prev and prev.status == "held" and prev.slot_id == slot_id:
                    redis.expire(rc.hold_slot_key(slot_id), settings.hold_ttl_seconds)
                    prev.expires_at = expires_at
                    db.commit()
                    store_idempotent_result(
                        idempotency_key,
                        {"hold_id": prev.hold_id, "status": prev.status},
                    )
                    return prev
        raise AllocationError("slot_held", "Slot is currently held by another request")

    # Release any prior hold by this exception on a different slot
    prior_hold_id = redis.get(rc.exception_hold_key(exception_id))
    if prior_hold_id:
        prior = db.get(SlotHold, prior_hold_id)
        if prior and prior.status == "held" and prior.slot_id != slot_id:
            redis.delete(rc.hold_slot_key(prior.slot_id))
            prior.status = "released"
            prior.released_at = now

    meta = {
        "hold_id": hold_id,
        "exception_id": exception_id,
        "shipment_id": shipment.shipment_id,
        "slot_id": slot_id,
        "expires_at": expires_at.isoformat(),
    }
    redis.setex(rc.hold_meta_key(hold_id), settings.hold_ttl_seconds, rc.dumps(meta))
    redis.setex(rc.exception_hold_key(exception_id), settings.hold_ttl_seconds, hold_id)

    hold = SlotHold(
        hold_id=hold_id,
        exception_id=exception_id,
        shipment_id=shipment.shipment_id,
        slot_id=slot_id,
        status="held",
        created_at=now,
        expires_at=expires_at,
        idempotency_key=idempotency_key,
    )
    db.add(hold)
    exception.status = "held"

    # Mark matching shown option as held lifecycle via reason note; others remain shown/stale
    views = (
        db.query(OptionView)
        .filter(OptionView.exception_id == exception_id, OptionView.status == "shown")
        .all()
    )
    for view in views:
        if view.slot_id != slot_id:
            view.status = "stale"

    db.commit()
    db.refresh(hold)
    store_idempotent_result(idempotency_key, {"hold_id": hold.hold_id, "status": hold.status})
    from app.services.observability import log_event

    log_event(
        "hold_created",
        exception_id=exception_id,
        driver_id=exception.driver_id if exception else None,
        shipment_id=shipment.shipment_id,
        hold_id=hold.hold_id,
        slot_id=slot_id,
        status=hold.status,
    )
    return hold


def confirm_hold(
    db: Session,
    hold_id: str,
    *,
    idempotency_key: str | None = None,
) -> tuple[SlotHold, Appointment]:
    cached = get_idempotent_result(idempotency_key)
    if cached and cached.get("appointment_id"):
        hold = db.get(SlotHold, cached.get("hold_id", hold_id))
        appt = db.get(Appointment, cached["appointment_id"])
        if hold and appt:
            return hold, appt

    hold = db.get(SlotHold, hold_id)
    if not hold:
        raise AllocationError("hold_not_found", "Hold not found")
    if hold.status == "confirmed":
        appt = (
            db.query(Appointment)
            .filter(
                Appointment.shipment_id == hold.shipment_id,
                Appointment.slot_id == hold.slot_id,
                Appointment.status == "confirmed",
            )
            .order_by(Appointment.confirmed_at.desc())
            .first()
        )
        if appt:
            return hold, appt
        raise AllocationError("missing_appointment", "Confirmed hold missing appointment")
    if hold.status != "held":
        raise AllocationError("invalid_hold_state", f"Hold status is {hold.status}")

    now = _now()
    if ensure_aware(hold.expires_at) <= now:
        hold.status = "expired"
        db.commit()
        raise AllocationError("hold_expired", "Hold has expired")

    redis = rc.get_redis()
    owner = redis.get(rc.hold_slot_key(hold.slot_id))
    if owner != hold.exception_id:
        hold.status = "expired"
        db.commit()
        raise AllocationError("hold_lost", "Hold is no longer exclusive in Redis")

    shipment = db.get(Shipment, hold.shipment_id)
    slot = db.get(AppointmentSlot, hold.slot_id)
    if not shipment or not slot:
        raise AllocationError("not_found", "Shipment or slot missing")

    from app.services.location import get_scheduling_eta

    sched_eta, _src = get_scheduling_eta(db, shipment, hold.exception_id, now=now)
    feasibility = evaluate_slot_for_shipment(
        db, shipment, slot, now=now, release_eta=sched_eta
    )
    if not feasibility.feasible:
        raise AllocationError(
            "became_infeasible",
            f"Slot became infeasible before confirm: {', '.join(feasibility.reasons)}",
        )

    # Supersede existing active appointment for this shipment
    existing = (
        db.query(Appointment)
        .filter(
            Appointment.shipment_id == shipment.shipment_id,
            Appointment.status.in_(["confirmed", "pending"]),
        )
        .all()
    )
    for appt in existing:
        appt.status = "superseded"
        appt.cancelled_at = now

    appointment = Appointment(
        appointment_id=_new_id("APT"),
        shipment_id=shipment.shipment_id,
        slot_id=slot.slot_id,
        status="confirmed",
        booked_at=now,
        confirmed_at=now,
    )
    db.add(appointment)

    hold.status = "confirmed"
    hold.confirmed_at = now

    exception = db.get(DriverException, hold.exception_id)
    if exception:
        exception.status = "confirmed"

    # Persist booking: Redis hold can be cleared — DB appointment is now source of truth
    redis.delete(rc.hold_slot_key(hold.slot_id))
    redis.delete(rc.hold_meta_key(hold.hold_id))
    redis.delete(rc.exception_hold_key(hold.exception_id))

    db.commit()
    db.refresh(hold)
    db.refresh(appointment)

    store_idempotent_result(
        idempotency_key,
        {
            "hold_id": hold.hold_id,
            "appointment_id": appointment.appointment_id,
            "status": "confirmed",
        },
    )
    from app.services.observability import log_event

    log_event(
        "hold_confirmed",
        exception_id=hold.exception_id,
        driver_id=exception.driver_id if exception else None,
        shipment_id=hold.shipment_id,
        hold_id=hold.hold_id,
        slot_id=hold.slot_id,
        appointment_id=appointment.appointment_id,
        status="confirmed",
    )
    return hold, appointment


def release_hold(db: Session, hold_id: str) -> SlotHold:
    hold = db.get(SlotHold, hold_id)
    if not hold:
        raise AllocationError("hold_not_found", "Hold not found")
    if hold.status not in {"held", "pending"}:
        return hold

    now = _now()
    redis = rc.get_redis()
    owner = redis.get(rc.hold_slot_key(hold.slot_id))
    if owner == hold.exception_id:
        redis.delete(rc.hold_slot_key(hold.slot_id))
    redis.delete(rc.hold_meta_key(hold.hold_id))
    redis.delete(rc.exception_hold_key(hold.exception_id))

    hold.status = "released"
    hold.released_at = now
    exception = db.get(DriverException, hold.exception_id)
    if exception and exception.status == "held":
        exception.status = "awaiting_choice"
    db.commit()
    db.refresh(hold)
    return hold


def cancel_appointment(db: Session, appointment_id: str) -> Appointment:
    appt = db.get(Appointment, appointment_id)
    if not appt:
        raise AllocationError("appointment_not_found", "Appointment not found")
    if appt.status == "cancelled":
        return appt

    now = _now()
    appt.status = "cancelled"
    appt.cancelled_at = now
    db.commit()
    db.refresh(appt)
    return appt


def is_slot_available_for_claim(db: Session, slot_id: str, exception_id: str) -> bool:
    """Availability for claiming: open slot, capacity free, not held by another."""
    slot = db.get(AppointmentSlot, slot_id)
    if not slot or slot.slot_status != "open":
        return False
    used = (
        db.query(Appointment)
        .filter(Appointment.slot_id == slot_id, Appointment.status.in_(["confirmed", "pending"]))
        .count()
    )
    if used >= slot.capacity_units:
        return False
    owner = rc.get_redis().get(rc.hold_slot_key(slot_id))
    if owner and owner != exception_id:
        return False
    return True
