"""Case metrics for before/after operational comparison (Advanced PDF §2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import (
    Appointment,
    AppointmentSlot,
    BaselineMetric,
    CaseMetric,
    FacilityCheckin,
    OptionView,
    Shipment,
)
from app.services import ops_log
from app.services.timeutil import ensure_aware


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _now() -> datetime:
    return ensure_aware(get_settings().now())  # type: ignore[return-value]


def projected_wait_minutes(eta: datetime | None, slot_start: datetime | None) -> int | None:
    """Minutes from scheduling ETA to slot start; None if either side missing."""
    if not eta or not slot_start:
        return None
    e = ensure_aware(eta)
    s = ensure_aware(slot_start)
    if not e or not s:
        return None
    return max(0, int((s - e).total_seconds() // 60))


def first_offered_slot_id(db: Session, exception_id: str) -> str | None:
    """Earliest rank-1 option shown for this exception (first offered batch)."""
    view = (
        db.query(OptionView)
        .filter(OptionView.exception_id == exception_id, OptionView.rank == 1)
        .order_by(OptionView.shown_at.asc())
        .first()
    )
    return view.slot_id if view else None


def confirm_wait_and_first_option(
    db: Session,
    *,
    shipment: Shipment,
    exception_id: str,
    new_appointment: Appointment,
) -> dict[str, Any]:
    """Honest post-confirm metrics: real old/new waits + whether first offered slot won.

    Old wait uses the most recently superseded appointment for the shipment (set by
    allocator.confirm_hold). New wait uses the newly confirmed appointment slot.
    Both are measured against the same scheduling ETA (gate-in > route > declared).
    """
    from app.services.location import get_scheduling_eta

    sched_eta, eta_src = get_scheduling_eta(db, shipment, exception_id)
    new_slot = new_appointment.slot
    if new_slot is None:
        new_slot = db.get(AppointmentSlot, new_appointment.slot_id)
    new_wait = projected_wait_minutes(
        sched_eta, new_slot.start_time if new_slot else None
    )

    prior = (
        db.query(Appointment)
        .options(joinedload(Appointment.slot))
        .filter(
            Appointment.shipment_id == shipment.shipment_id,
            Appointment.status == "superseded",
        )
        .order_by(Appointment.cancelled_at.desc().nullslast())
        .first()
    )
    old_wait = None
    if prior is not None:
        prior_slot = prior.slot or db.get(AppointmentSlot, prior.slot_id)
        old_wait = projected_wait_minutes(
            sched_eta, prior_slot.start_time if prior_slot else None
        )

    first_slot = first_offered_slot_id(db, exception_id)
    first_accepted = (
        (first_slot == new_appointment.slot_id) if first_slot else None
    )

    return {
        "eta_source_used": eta_src,
        "predicted_eta": sched_eta,
        "old_wait": old_wait,
        "new_wait": new_wait,
        "first_option_accepted": first_accepted,
    }


def ensure_case_metric(db: Session, exception_id: str, shipment_id: str) -> CaseMetric:
    existing = db.query(CaseMetric).filter(CaseMetric.exception_id == exception_id).first()
    if existing:
        return existing
    row = CaseMetric(
        metric_id=_new_id("MET"),
        exception_id=exception_id,
        shipment_id=shipment_id,
        started_at=_now(),
        resolution_status="open",
        human_intervention=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def mark_options_generated(db: Session, exception_id: str) -> None:
    row = db.query(CaseMetric).filter(CaseMetric.exception_id == exception_id).first()
    if not row:
        return
    if not row.options_generated_at:
        row.options_generated_at = _now()
        db.commit()


def mark_resolved(
    db: Session,
    exception_id: str,
    *,
    status: str,
    human: bool = False,
    first_option_accepted: bool | None = None,
    eta_source_used: str | None = None,
    predicted_eta: datetime | None = None,
    old_wait: int | None = None,
    new_wait: int | None = None,
) -> None:
    row = db.query(CaseMetric).filter(CaseMetric.exception_id == exception_id).first()
    if not row:
        return
    row.resolved_at = _now()
    row.resolution_status = status
    row.human_intervention = human or status == "escalated"
    if first_option_accepted is not None:
        row.first_option_accepted = first_option_accepted
    if eta_source_used:
        row.eta_source_used = eta_source_used
    if predicted_eta:
        row.predicted_eta = ensure_aware(predicted_eta)
    if old_wait is not None:
        row.old_projected_wait_minutes = old_wait
    if new_wait is not None:
        row.new_projected_wait_minutes = new_wait

    checkin = (
        db.query(FacilityCheckin)
        .filter(FacilityCheckin.shipment_id == row.shipment_id)
        .order_by(FacilityCheckin.gate_in_at.desc().nullslast())
        .first()
    )
    if checkin and checkin.gate_in_at:
        row.actual_gate_in = ensure_aware(checkin.gate_in_at)
    db.commit()

    outcome = "completed" if status == "confirmed" else ("escalated" if status == "escalated" else status)
    ops_log.record_event(
        kind="domain",
        outcome=outcome if not human else ("human_help" if status == "escalated" else outcome),
        detail=f"exception={exception_id} status={status}",
    )
    if human or status == "escalated":
        ops_log.record_event(
            kind="domain",
            outcome="human_help",
            detail=f"exception={exception_id}",
        )

    if status == "escalated":
        from app.services.observability import log_event

        log_event(
            "escalation",
            exception_id=exception_id,
            shipment_id=row.shipment_id,
            status=status,
            human=bool(human or status == "escalated"),
        )


def summary(db: Session) -> dict[str, Any]:
    cases = db.query(CaseMetric).all()
    resolved = [c for c in cases if c.resolution_status in {"confirmed", "escalated"}]
    confirmed = [c for c in cases if c.resolution_status == "confirmed"]
    escalated = [c for c in cases if c.resolution_status == "escalated"]

    def avg_resolution(rows: list[CaseMetric]) -> float | None:
        vals = []
        for c in rows:
            if c.started_at and c.resolved_at:
                start = ensure_aware(c.started_at)
                end = ensure_aware(c.resolved_at)
                if start and end:
                    vals.append((end - start).total_seconds() / 60.0)
        return round(sum(vals) / len(vals), 1) if vals else None

    def eta_error(rows: list[CaseMetric]) -> float | None:
        errs = []
        for c in rows:
            if c.predicted_eta and c.actual_gate_in:
                p = ensure_aware(c.predicted_eta)
                a = ensure_aware(c.actual_gate_in)
                if p and a:
                    errs.append(abs((a - p).total_seconds()) / 60.0)
        return round(sum(errs) / len(errs), 1) if errs else None

    wait_reduced = []
    for c in confirmed:
        if c.old_projected_wait_minutes is not None and c.new_projected_wait_minutes is not None:
            wait_reduced.append(c.old_projected_wait_minutes - c.new_projected_wait_minutes)

    first_known = [c for c in confirmed if c.first_option_accepted is not None]
    solution = {
        "cases": len(cases),
        "resolved": len(resolved),
        "confirmed": len(confirmed),
        "escalated": len(escalated),
        "avg_resolution_minutes": avg_resolution(resolved),
        "human_help_rate": (
            round(sum(1 for c in resolved if c.human_intervention) / len(resolved), 2)
            if resolved
            else None
        ),
        "self_service_rate": (
            round(sum(1 for c in resolved if not c.human_intervention) / len(resolved), 2)
            if resolved
            else None
        ),
        "avg_eta_error_minutes": eta_error(cases),
        "first_option_accept_rate": (
            round(
                sum(1 for c in first_known if c.first_option_accepted) / len(first_known),
                2,
            )
            if first_known
            else None
        ),
        "avg_wait_reduced_minutes": (
            round(sum(wait_reduced) / len(wait_reduced), 1) if wait_reduced else None
        ),
    }

    baselines = db.query(BaselineMetric).all()
    before = [
        {
            "label": b.label,
            "avg_resolution_minutes": b.avg_resolution_minutes,
            "human_help_rate": b.human_help_rate,
            "avg_eta_error_minutes": b.avg_eta_error_minutes,
            "sample_size": b.sample_size,
            "notes": b.notes,
        }
        for b in baselines
    ]

    return {
        "before_manual": before,
        "after_solution": solution,
        "comparison_note": (
            "Before values are seeded classroom baselines for similar delay types; "
            "after values are computed from CaseMetric rows generated by the live workflow. "
            "Wait reduction uses superseded vs new appointment slot starts against the same "
            "scheduling ETA; first-option accept compares the confirmed slot to the earliest "
            "rank-1 shown option."
        ),
    }


def ops_summary(db: Session) -> dict[str, Any]:
    """Combine live CaseMetric outcomes with in-process request/event log."""
    snap = ops_log.snapshot()
    cases = db.query(CaseMetric).all()
    confirmed = sum(1 for c in cases if c.resolution_status == "confirmed")
    escalated = sum(1 for c in cases if c.resolution_status == "escalated")
    human = sum(1 for c in cases if c.human_intervention)
    open_cases = sum(1 for c in cases if c.resolution_status == "open")
    return {
        **snap,
        "case_metrics": {
            "open": open_cases,
            "confirmed": confirmed,
            "escalated": escalated,
            "human_intervention": human,
            "total": len(cases),
        },
    }
