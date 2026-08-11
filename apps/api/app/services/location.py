"""One-time browser location + route ETA (kept separate from driver-declared ETA)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Facility, LocationShare, RouteEtaRecord, Shipment
from app.services.feasibility import get_effective_eta
from app.services.geoapify import calculate_route_eta
from app.services.timeutil import ensure_aware


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _now() -> datetime:
    return ensure_aware(get_settings().now())  # type: ignore[return-value]


def latest_location(db: Session, exception_id: str) -> LocationShare | None:
    return (
        db.query(LocationShare)
        .filter(LocationShare.exception_id == exception_id, LocationShare.status == "ok")
        .order_by(LocationShare.captured_at.desc())
        .first()
    )


def latest_route_eta(db: Session, exception_id: str) -> RouteEtaRecord | None:
    return (
        db.query(RouteEtaRecord)
        .filter(RouteEtaRecord.exception_id == exception_id)
        .order_by(RouteEtaRecord.calculated_at.desc())
        .first()
    )


def is_location_stale(share: LocationShare, now: datetime | None = None) -> bool:
    now = ensure_aware(now or _now())
    captured = ensure_aware(share.captured_at)
    assert now and captured
    age = now - captured
    return age > timedelta(minutes=get_settings().location_stale_minutes)


def get_scheduling_eta(
    db: Session,
    shipment: Shipment,
    exception_id: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[datetime, str]:
    """ETA used for ranking/buffers. Does not overwrite driver-declared ETA.

    Precedence for *scheduling* use:
      gate-in > fresh route ETA (if present) > driver/ops declared > planned
    Driver-declared rows remain untouched in eta_updates.
    """
    now = ensure_aware(now or _now())
    # Gate-in still absolute truth via get_effective_eta when arrived
    from app.models import FacilityCheckin

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
        return ensure_aware(checkin.gate_in_at), "gate_in"  # type: ignore[return-value]

    if exception_id:
        route = latest_route_eta(db, exception_id)
        loc = latest_location(db, exception_id)
        if route and route.route_eta and loc and not is_location_stale(loc, now):
            return ensure_aware(route.route_eta), "route"  # type: ignore[return-value]

    return get_effective_eta(db, shipment, now=now), "declared_or_planned"


def record_denied_or_error(
    db: Session,
    *,
    exception_id: str,
    shipment_id: str,
    status: str,
) -> LocationShare:
    share = LocationShare(
        location_id=_new_id("LOC"),
        exception_id=exception_id,
        shipment_id=shipment_id,
        latitude=0.0,
        longitude=0.0,
        accuracy_m=None,
        captured_at=_now(),
        received_at=_now(),
        status=status,
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return share


def submit_location(
    db: Session,
    *,
    exception_id: str,
    shipment_id: str,
    latitude: float,
    longitude: float,
    accuracy_m: float | None,
    captured_at: datetime,
) -> dict[str, Any]:
    now = _now()
    share = LocationShare(
        location_id=_new_id("LOC"),
        exception_id=exception_id,
        shipment_id=shipment_id,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        captured_at=ensure_aware(captured_at) or now,
        received_at=now,
        status="ok",
    )
    if is_location_stale(share, now):
        share.status = "stale"
    db.add(share)
    db.flush()

    shipment = db.get(Shipment, shipment_id)
    facility = db.get(Facility, shipment.destination_id) if shipment else None
    route_record = None
    comparison: dict[str, Any] = {}

    if share.status == "ok" and facility and facility.latitude is not None and facility.longitude is not None:
        result = calculate_route_eta(
            origin_lat=latitude,
            origin_lon=longitude,
            dest_lat=facility.latitude,
            dest_lon=facility.longitude,
            now=now,
        )
        route_record = RouteEtaRecord(
            route_eta_id=_new_id("RETA"),
            exception_id=exception_id,
            shipment_id=shipment_id,
            location_id=share.location_id,
            provider=result.provider,
            distance_km=result.distance_km if result.ok else None,
            duration_minutes=result.duration_minutes if result.ok else None,
            route_eta=result.route_eta if result.ok else None,
            calculated_at=now,
            used_for_scheduling=bool(result.ok),
        )
        db.add(route_record)

        declared = get_effective_eta(db, shipment, now=now) if shipment else None
        comparison = {
            "driver_declared_or_planned_eta": declared,
            "route_eta": result.route_eta if result.ok else None,
            "provider": result.provider,
            "distance_km": result.distance_km if result.ok else None,
            "duration_minutes": result.duration_minutes if result.ok else None,
            "ok": result.ok,
            "error": result.error,
            "delta_minutes": (
                int((ensure_aware(result.route_eta) - ensure_aware(declared)).total_seconds() // 60)  # type: ignore[operator]
                if result.ok and declared
                else None
            ),
        }

    db.commit()
    if route_record:
        db.refresh(route_record)

    return {
        "location": share,
        "route_eta": route_record,
        "comparison": comparison,
        "stale": share.status == "stale",
    }
