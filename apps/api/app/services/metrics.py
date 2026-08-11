"""Case metrics for before/after operational comparison (Advanced PDF §2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import BaselineMetric, CaseMetric, FacilityCheckin
from app.services.timeutil import ensure_aware


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _now() -> datetime:
    return ensure_aware(get_settings().now())  # type: ignore[return-value]


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
                sum(1 for c in confirmed if c.first_option_accepted) / len(confirmed),
                2,
            )
            if confirmed
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
            "after values are computed from CaseMetric rows generated by the live workflow."
        ),
    }
