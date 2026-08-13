"""Deterministic feasibility engine.

Capacity / feasibility truth lives here — never in the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import (
    Appointment,
    AppointmentSlot,
    Dock,
    Facility,
    FacilityCheckin,
    FacilityRule,
    Shipment,
    Vehicle,
)
from app.services.timeutil import IST, ensure_aware


@dataclass
class FeasibilityResult:
    feasible: bool
    reasons: list[str] = field(default_factory=list)
    buffer_minutes: int | None = None


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def in_operating_hours(facility: Facility, slot_start: datetime, slot_end: datetime) -> bool:
    local_start = ensure_aware(slot_start).time()  # type: ignore[union-attr]
    local_end = ensure_aware(slot_end).time()  # type: ignore[union-attr]
    open_t = parse_hhmm(facility.open_time)
    close_t = parse_hhmm(facility.close_time)
    return local_start >= open_t and local_end <= close_t


def rule_active_at(rule: FacilityRule, when: datetime) -> bool:
    if not rule.effective_from and not rule.effective_to:
        return True
    local = ensure_aware(when).time()  # type: ignore[union-attr]
    start = parse_hhmm(rule.effective_from) if rule.effective_from else time(0, 0)
    end = parse_hhmm(rule.effective_to) if rule.effective_to else time(23, 59)
    return start <= local <= end


def get_effective_eta(db: Session, shipment: Shipment, now: datetime | None = None) -> datetime:
    """Precedence: gate-in (arrived) > latest driver/ops declared ETA > planned ETA."""
    now = ensure_aware(now or get_settings().now())
    checkin = (
        db.query(FacilityCheckin)
        .filter(FacilityCheckin.shipment_id == shipment.shipment_id)
        .order_by(FacilityCheckin.gate_in_at.desc().nullslast())
        .first()
    )
    if checkin and checkin.gate_in_at and checkin.arrival_status in {
        "arrived",
        "waiting_gate",
        "waiting_yard",
        "docked",
        "unloading",
    }:
        return ensure_aware(checkin.gate_in_at)  # type: ignore[return-value]

    latest = None
    for upd in sorted(
        shipment.eta_updates,
        key=lambda u: ensure_aware(u.declared_at) or datetime.min.replace(tzinfo=IST),
        reverse=True,
    ):
        if upd.source_type in {"driver", "operations"}:
            latest = upd
            break
    if latest:
        return ensure_aware(latest.declared_eta)  # type: ignore[return-value]
    return ensure_aware(shipment.planned_eta)  # type: ignore[return-value]


def dock_compatible(dock: Dock, vehicle: Vehicle, product_class: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not dock.active_flag:
        reasons.append("dock_inactive")
    supported_types = {t.strip().lower() for t in dock.supported_vehicle_type.split("|")}
    if vehicle.vehicle_type.lower() not in supported_types and "any" not in supported_types:
        reasons.append("vehicle_type_incompatible")
    supported_products = {p.strip().lower() for p in dock.supported_product_class.split("|")}
    if product_class.lower() not in supported_products and "any" not in supported_products:
        reasons.append("product_class_incompatible")
    if vehicle.length_ft > dock.max_length_ft:
        reasons.append("vehicle_too_long")
    if vehicle.refrigeration_required and "reefer" not in supported_types and "refrigerated" not in supported_types:
        # dry docks cannot take reefer-required loads unless explicitly supported
        if "reefer" not in dock.supported_vehicle_type.lower():
            reasons.append("refrigeration_required")
    return (len(reasons) == 0, reasons)


def evaluate_slot_for_shipment(
    db: Session,
    shipment: Shipment,
    slot: AppointmentSlot,
    *,
    now: datetime | None = None,
    ignore_appointment_id: str | None = None,
    release_eta: datetime | None = None,
) -> FeasibilityResult:
    now = ensure_aware(now or get_settings().now())
    reasons: list[str] = []

    facility = db.get(Facility, slot.facility_id)
    dock = db.get(Dock, slot.dock_id)
    vehicle = db.get(Vehicle, shipment.vehicle_id)
    if not facility or not dock or not vehicle:
        return FeasibilityResult(False, ["missing_reference_data"])

    slot_start = ensure_aware(slot.start_time)
    slot_end = ensure_aware(slot.end_time)
    assert slot_start and slot_end and now

    if slot.facility_id != shipment.destination_id:
        reasons.append("wrong_facility")

    if slot.slot_status != "open":
        reasons.append(f"slot_status_{slot.slot_status}")

    if not in_operating_hours(facility, slot_start, slot_end):
        reasons.append("outside_operating_hours")

    ok, dock_reasons = dock_compatible(dock, vehicle, shipment.product_class)
    if not ok:
        reasons.extend(dock_reasons)

    # Facility rules
    rules = db.query(FacilityRule).filter(FacilityRule.facility_id == facility.facility_id).all()
    for rule in rules:
        if not rule_active_at(rule, slot_start):
            continue
        if rule.rule_type == "max_vehicle_length_ft":
            if vehicle.length_ft > int(rule.rule_value):
                reasons.append("facility_rule_max_length")
        elif rule.rule_type == "allowed_product_class":
            allowed = {x.strip().lower() for x in rule.rule_value.split("|")}
            if shipment.product_class.lower() not in allowed:
                reasons.append("facility_rule_product_class")
        elif rule.rule_type == "block_carrier":
            # carrier blocked via vehicle carrier
            if vehicle.carrier_id == rule.rule_value:
                reasons.append("facility_rule_carrier_blocked")

    eta = ensure_aware(release_eta) if release_eta else get_effective_eta(db, shipment, now=now)
    if eta > slot_start:
        reasons.append("cannot_reach_before_slot_start")

    # Unload duration must fit the slot window
    slot_minutes = int((slot_end - slot_start).total_seconds() // 60)
    if shipment.expected_unload_minutes > slot_minutes:
        reasons.append("unload_exceeds_slot_duration")

    leave_by = ensure_aware(shipment.leave_by)
    if leave_by and slot_end > leave_by:
        reasons.append("cannot_finish_before_leave_by")

    # Capacity: confirmed/pending appointments consume capacity_units
    active_statuses = {"confirmed", "pending"}
    q = (
        db.query(Appointment)
        .filter(
            Appointment.slot_id == slot.slot_id,
            Appointment.status.in_(active_statuses),
        )
    )
    if ignore_appointment_id:
        q = q.filter(Appointment.appointment_id != ignore_appointment_id)
    used = q.count()
    if used >= slot.capacity_units:
        reasons.append("slot_capacity_exhausted")

    buffer = int((slot_start - eta).total_seconds() // 60)
    feasible = len(reasons) == 0
    return FeasibilityResult(feasible=feasible, reasons=reasons, buffer_minutes=buffer if feasible or buffer >= 0 else buffer)


def list_feasible_slots(
    db: Session,
    shipment: Shipment,
    *,
    after: datetime | None = None,
    limit: int = 10,
    now: datetime | None = None,
    release_eta: datetime | None = None,
) -> list[tuple[AppointmentSlot, FeasibilityResult]]:
    now = ensure_aware(now or get_settings().now())
    after = ensure_aware(after or now)
    assert now and after

    # SQLite stores naive IST wall times; Postgres stores timestamptz (UTC).
    # Compare like-for-like so the SQL filter does not mis-interpret naive as UTC.
    bind = db.get_bind()
    dialect = bind.dialect.name if bind is not None else "sqlite"
    if dialect == "sqlite":
        after_cmp: datetime = after.replace(tzinfo=None)
    else:
        after_cmp = after

    slots = (
        db.query(AppointmentSlot)
        .options(joinedload(AppointmentSlot.dock))
        .filter(
            AppointmentSlot.facility_id == shipment.destination_id,
            AppointmentSlot.start_time >= after_cmp,
            AppointmentSlot.slot_status == "open",
        )
        .order_by(AppointmentSlot.start_time.asc())
        .all()
    )

    results: list[tuple[AppointmentSlot, FeasibilityResult]] = []
    for slot in slots:
        result = evaluate_slot_for_shipment(
            db, shipment, slot, now=now, release_eta=release_eta
        )
        if result.feasible:
            results.append((slot, result))
    # Prefer safer buffers first when ranking
    results.sort(key=lambda x: (-(x[1].buffer_minutes or 0), x[0].start_time))
    return results[:limit]


def current_appointment(db: Session, shipment_id: str) -> Appointment | None:
    return (
        db.query(Appointment)
        .options(joinedload(Appointment.slot))
        .filter(
            Appointment.shipment_id == shipment_id,
            Appointment.status.in_(["confirmed", "pending"]),
        )
        .order_by(Appointment.booked_at.desc())
        .first()
    )
