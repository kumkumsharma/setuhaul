"""Facility-level scheduling engine (rule-based, explicit scores).

Does not parse free text. Receives structured operational data and returns a
proposed sequence / ranked assignments. Capacity confirmation still goes through
the Phase 1 allocator.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import (
    Appointment,
    AppointmentSlot,
    Dock,
    FacilityCheckin,
    SchedulingRun,
    Shipment,
    Vehicle,
)
from app.services.feasibility import dock_compatible, get_effective_eta
from app.services.timeutil import ensure_aware


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _now() -> datetime:
    return ensure_aware(get_settings().now())  # type: ignore[return-value]


def _as_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    if isinstance(value, str):
        return ensure_aware(datetime.fromisoformat(value))
    return None


def _score_job(job: dict[str, Any], now: datetime) -> float:
    """Lower score = schedule sooner.

    Explicit weights (not hidden in a prompt):
      - already unloading: fixed, excluded from reassignment
      - waiting now: strong priority
      - appointment lateness risk
      - shipment priority (1=highest → lower score)
      - release time (ETA)
    """
    if job["fixed"]:
        return -1_000_000.0

    score = 0.0
    release = _as_dt(job["release_time"])
    assert release and now
    wait_if_ready = max(0, int((now - release).total_seconds() // 60)) if release <= now else 0
    score -= wait_if_ready * 2.0  # prefer trucks already waiting

    due = _as_dt(job.get("appointment_start"))
    if due:
        lateness = max(0, int((release - due).total_seconds() // 60))
        score += lateness * 3.0

    score += (job["priority"] - 1) * 15.0
    # Prefer earlier release among equals
    score += max(0, int((release - now).total_seconds() // 60)) * 0.5
    return score


def build_facility_snapshot(db: Session, facility_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = ensure_aware(now or _now())
    assert now

    docks = (
        db.query(Dock)
        .filter(Dock.facility_id == facility_id, Dock.active_flag.is_(True))
        .all()
    )
    dock_state: dict[str, Any] = {}
    for dock in docks:
        dock_state[dock.dock_id] = {
            "dock_id": dock.dock_id,
            "dock_name": dock.dock_name,
            "free_at": now.isoformat(),
            "supported_vehicle_type": dock.supported_vehicle_type,
            "supported_product_class": dock.supported_product_class,
            "max_length_ft": dock.max_length_ft,
        }

    # Active / relevant shipments toward this facility
    shipments = (
        db.query(Shipment)
        .options(joinedload(Shipment.eta_updates), joinedload(Shipment.vehicle))
        .filter(
            Shipment.destination_id == facility_id,
            Shipment.status.in_(["planned", "in_transit", "arrived", "waiting"]),
        )
        .all()
    )

    jobs: list[dict[str, Any]] = []
    for shp in shipments:
        checkin = (
            db.query(FacilityCheckin)
            .filter(FacilityCheckin.shipment_id == shp.shipment_id)
            .order_by(FacilityCheckin.gate_in_at.desc().nullslast())
            .first()
        )
        appt = (
            db.query(Appointment)
            .options(joinedload(Appointment.slot))
            .filter(
                Appointment.shipment_id == shp.shipment_id,
                Appointment.status.in_(["confirmed", "pending"]),
            )
            .order_by(Appointment.booked_at.desc())
            .first()
        )
        vehicle = db.get(Vehicle, shp.vehicle_id)
        fixed = bool(checkin and checkin.arrival_status == "unloading")
        release = get_effective_eta(db, shp, now=now)
        compatible = []
        for dock in docks:
            ok, _ = dock_compatible(dock, vehicle, shp.product_class) if vehicle else (False, [])
            if ok:
                compatible.append(dock.dock_id)

        jobs.append(
            {
                "shipment_id": shp.shipment_id,
                "priority": shp.priority,
                "unload_minutes": shp.expected_unload_minutes,
                "release_time": release.isoformat(),
                "appointment_start": (
                    ensure_aware(appt.slot.start_time).isoformat() if appt and appt.slot else None
                ),
                "appointment_id": appt.appointment_id if appt else None,
                "arrival_status": checkin.arrival_status if checkin else "en_route",
                "queue_status": checkin.queue_status if checkin else "none",
                "fixed": fixed,
                "expected_finish_at": (
                    ensure_aware(checkin.expected_finish_at).isoformat()
                    if checkin and checkin.expected_finish_at
                    else None
                ),
                "compatible_docks": compatible,
            }
        )

    return {
        "facility_id": facility_id,
        "now": now.isoformat(),
        "docks": list(dock_state.values()),
        "jobs": jobs,
    }


def propose_schedule(db: Session, facility_id: str, *, now: datetime | None = None) -> SchedulingRun:
    now = ensure_aware(now or _now())
    assert now
    snapshot = build_facility_snapshot(db, facility_id, now=now)

    # Initialize dock free times; fixed unloading occupies a compatible dock until finish
    dock_free: dict[str, datetime] = {
        d["dock_id"]: now for d in snapshot["docks"]
    }
    assignments: list[dict[str, Any]] = []
    explanations: list[str] = []

    fixed_jobs = [j for j in snapshot["jobs"] if j["fixed"]]
    for job in fixed_jobs:
        finish = _as_dt(job.get("expected_finish_at")) or (
            now + timedelta(minutes=job["unload_minutes"])
        )
        # Occupy first compatible dock
        dock_id = job["compatible_docks"][0] if job["compatible_docks"] else None
        if dock_id:
            dock_free[dock_id] = finish
            assignments.append(
                {
                    "shipment_id": job["shipment_id"],
                    "dock_id": dock_id,
                    "start": now.isoformat(),
                    "end": finish.isoformat(),
                    "fixed": True,
                    "score": None,
                    "reason": "already_unloading_immovable",
                }
            )
            explanations.append(
                f"{job['shipment_id']} fixed on {dock_id} until {finish.strftime('%H:%M')} (unloading)."
            )

    movable = [j for j in snapshot["jobs"] if not j["fixed"] and j["compatible_docks"]]
    scored = []
    for job in movable:
        scored.append((_score_job(job, now), job))
    scored.sort(key=lambda x: x[0])

    for score, job in scored:
        release = _as_dt(job["release_time"])
        assert release
        best_dock = None
        best_start = None
        for dock_id in job["compatible_docks"]:
            free_at = dock_free.get(dock_id, now)
            start = max(free_at, release, now)
            if best_start is None or start < best_start:
                best_start = start
                best_dock = dock_id
        if not best_dock or not best_start:
            explanations.append(f"{job['shipment_id']} has no compatible dock — skipped.")
            continue
        end = best_start + timedelta(minutes=job["unload_minutes"])
        dock_free[best_dock] = end
        assignments.append(
            {
                "shipment_id": job["shipment_id"],
                "dock_id": best_dock,
                "start": best_start.isoformat(),
                "end": end.isoformat(),
                "fixed": False,
                "score": score,
                "reason": "score_heuristic",
            }
        )
        explanations.append(
            f"{job['shipment_id']} → {best_dock} at {best_start.strftime('%H:%M')} "
            f"(score {score:.1f}; priority={job['priority']}, status={job['arrival_status']})."
        )

    objective = "min_weighted_wait_lateness_priority"
    run = SchedulingRun(
        run_id=_new_id("SCH"),
        facility_id=facility_id,
        created_at=now,
        objective=objective,
        input_snapshot_json=json.dumps(snapshot, default=str),
        proposal_json=json.dumps({"assignments": assignments}, default=str),
        explanation="\n".join(explanations) or "No jobs to schedule.",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def run_to_dict(run: SchedulingRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "facility_id": run.facility_id,
        "created_at": ensure_aware(run.created_at),
        "objective": run.objective,
        "proposal": json.loads(run.proposal_json),
        "explanation": run.explanation,
        "input_snapshot": json.loads(run.input_snapshot_json),
    }
