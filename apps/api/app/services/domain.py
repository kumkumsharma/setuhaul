"""Domain read/write helpers used by APIs and the chat orchestrator."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import (
    Appointment,
    ChatMessage,
    Driver,
    DriverException,
    EtaUpdate,
    FacilityCheckin,
    Shipment,
)
from app.services.feasibility import current_appointment, evaluate_slot_for_shipment, get_effective_eta
from app.services.timeutil import ensure_aware


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _now() -> datetime:
    return ensure_aware(get_settings().now())  # type: ignore[return-value]


def get_driver(db: Session, driver_id: str) -> Driver | None:
    return db.get(Driver, driver_id)


def list_active_shipments(db: Session, driver_id: str) -> list[Shipment]:
    return (
        db.query(Shipment)
        .options(
            joinedload(Shipment.eta_updates),
            joinedload(Shipment.checkins),
            joinedload(Shipment.destination),
        )
        .filter(
            Shipment.driver_id == driver_id,
            Shipment.status.in_(["planned", "in_transit", "arrived", "waiting"]),
        )
        .order_by(Shipment.planned_eta.asc())
        .all()
    )


def get_shipment(db: Session, shipment_id: str) -> Shipment | None:
    return (
        db.query(Shipment)
        .options(
            joinedload(Shipment.eta_updates),
            joinedload(Shipment.checkins),
            joinedload(Shipment.vehicle),
            joinedload(Shipment.destination),
        )
        .filter(Shipment.shipment_id == shipment_id)
        .first()
    )


def shipment_to_dict(db: Session, shipment: Shipment) -> dict[str, Any]:
    checkin = (
        db.query(FacilityCheckin)
        .filter(FacilityCheckin.shipment_id == shipment.shipment_id)
        .order_by(FacilityCheckin.gate_in_at.desc().nullslast())
        .first()
    )
    latest_declared = None
    for upd in sorted(
        shipment.eta_updates,
        key=lambda u: ensure_aware(u.declared_at) or datetime.min,
        reverse=True,
    ):
        if upd.source_type in {"driver", "operations"}:
            latest_declared = ensure_aware(upd.declared_eta)
            break
    return {
        "shipment_id": shipment.shipment_id,
        "driver_id": shipment.driver_id,
        "vehicle_id": shipment.vehicle_id,
        "origin_id": shipment.origin_id,
        "destination_id": shipment.destination_id,
        "product_class": shipment.product_class,
        "priority": shipment.priority,
        "planned_eta": ensure_aware(shipment.planned_eta),
        "expected_unload_minutes": shipment.expected_unload_minutes,
        "status": shipment.status,
        "leave_by": ensure_aware(shipment.leave_by),
        "latest_declared_eta": latest_declared,
        "effective_eta": get_effective_eta(db, shipment),
        "arrival_status": checkin.arrival_status if checkin else None,
        "queue_status": checkin.queue_status if checkin else None,
    }


def appointment_to_dict(db: Session, appt: Appointment | None) -> dict[str, Any] | None:
    if not appt:
        return None
    slot = appt.slot
    shipment = db.get(Shipment, appt.shipment_id)
    feasibility = None
    reasons: list[str] = []
    if shipment and slot:
        result = evaluate_slot_for_shipment(db, shipment, slot)
        feasibility = result.feasible
        reasons = result.reasons
    return {
        "appointment_id": appt.appointment_id,
        "shipment_id": appt.shipment_id,
        "slot_id": appt.slot_id,
        "status": appt.status,
        "booked_at": ensure_aware(appt.booked_at),
        "confirmed_at": ensure_aware(appt.confirmed_at),
        "cancelled_at": ensure_aware(appt.cancelled_at),
        "slot_start": ensure_aware(slot.start_time) if slot else None,
        "slot_end": ensure_aware(slot.end_time) if slot else None,
        "dock_id": slot.dock_id if slot else None,
        "facility_id": slot.facility_id if slot else None,
        "is_feasible": feasibility,
        "infeasible_reasons": reasons,
    }


def create_exception(
    db: Session,
    *,
    driver_id: str,
    shipment_id: str,
    exception_type: str = "delay",
    reported_delay_minutes: int | None = None,
    latest_declared_eta: datetime | None = None,
    message: str | None = None,
) -> DriverException:
    now = _now()
    exception = DriverException(
        exception_id=_new_id("EXC"),
        driver_id=driver_id,
        shipment_id=shipment_id,
        exception_type=exception_type,
        reported_delay_minutes=reported_delay_minutes,
        latest_declared_eta=latest_declared_eta,
        reported_at=now,
        status="open",
        conversation_state="{}",
    )
    db.add(exception)

    if latest_declared_eta:
        db.add(
            EtaUpdate(
                eta_update_id=_new_id("ETA"),
                shipment_id=shipment_id,
                declared_eta=latest_declared_eta,
                source_type="driver",
                declared_at=now,
                confidence_note="declared_via_exception",
            )
        )

    if message:
        db.add(
            ChatMessage(
                message_id=_new_id("MSG"),
                thread_id=exception.exception_id,
                exception_id=exception.exception_id,
                sender_type="driver",
                message_text=message,
                created_at=now,
            )
        )

    db.commit()
    db.refresh(exception)
    from app.services.observability import log_event

    log_event(
        "exception_opened",
        exception_id=exception.exception_id,
        driver_id=driver_id,
        shipment_id=shipment_id,
        status=exception.status,
    )
    return exception


def add_message(
    db: Session,
    exception_id: str,
    *,
    sender_type: str,
    text: str,
    meta: dict[str, Any] | None = None,
) -> ChatMessage:
    msg = ChatMessage(
        message_id=_new_id("MSG"),
        thread_id=exception_id,
        exception_id=exception_id,
        sender_type=sender_type,
        message_text=text,
        created_at=_now(),
        meta_json=json.dumps(meta) if meta else None,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def update_exception_state(db: Session, exception: DriverException, patch: dict[str, Any]) -> None:
    state = json.loads(exception.conversation_state or "{}")
    state.update(patch)
    exception.conversation_state = json.dumps(state)
    db.commit()


def get_open_exception(db: Session, driver_id: str, shipment_id: str | None = None) -> DriverException | None:
    q = db.query(DriverException).filter(
        DriverException.driver_id == driver_id,
        DriverException.status.in_(
            ["open", "awaiting_choice", "held", "pending", "escalated"]
        ),
    )
    if shipment_id:
        q = q.filter(DriverException.shipment_id == shipment_id)
    return q.order_by(DriverException.reported_at.desc()).first()


def get_appointment_context(db: Session, shipment_id: str) -> dict[str, Any] | None:
    appt = current_appointment(db, shipment_id)
    return appointment_to_dict(db, appt)
