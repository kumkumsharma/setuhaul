from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import DriverException, Shipment
from app.services import location as location_svc
from app.services import metrics as metrics_svc
from app.services.chat import handle_chat
from app.services.observability import log_event
from app.services import ops_log

router = APIRouter(prefix="/api", tags=["location"])


class LocationSubmit(BaseModel):
    exception_id: str
    shipment_id: Optional[str] = None
    latitude: float
    longitude: float
    accuracy_m: Optional[float] = None
    captured_at: datetime
    denied: bool = False
    error: Optional[str] = None


class LocationDecline(BaseModel):
    exception_id: str
    shipment_id: Optional[str] = None
    reason: str = "declined"


@router.post("/location")
def submit_browser_location(body: LocationSubmit, db: Session = Depends(get_db)):
    exc = db.get(DriverException, body.exception_id)
    if not exc:
        raise HTTPException(404, "Exception not found")
    shipment_id = body.shipment_id or exc.shipment_id

    if body.denied or body.error:
        location_svc.record_denied_or_error(
            db,
            exception_id=body.exception_id,
            shipment_id=shipment_id,
            status="denied" if body.denied else "error",
        )
        ops_log.record_event(
            kind="domain",
            outcome="location_failure",
            detail=f"exception={body.exception_id} denied={body.denied}",
        )
        log_event(
            "location_failure" if body.error else "location_declined",
            exception_id=body.exception_id,
            driver_id=exc.driver_id,
            shipment_id=shipment_id,
            reason="error" if body.error else "denied",
        )
        # Resume chat with declared-ETA workflow
        result = handle_chat(
            db,
            driver_id=exc.driver_id,
            message="Location unavailable — continue with my declared ETA.",
            exception_id=exc.exception_id,
            shipment_id=shipment_id,
        )
        result["client_action"] = None
        result["tools_used"] = list(result.get("tools_used") or []) + ["location_fallback"]
        return result

    payload = location_svc.submit_location(
        db,
        exception_id=body.exception_id,
        shipment_id=shipment_id,
        latitude=body.latitude,
        longitude=body.longitude,
        accuracy_m=body.accuracy_m,
        captured_at=body.captured_at,
    )
    log_event(
        "location_submitted",
        exception_id=body.exception_id,
        driver_id=exc.driver_id,
        shipment_id=shipment_id,
        stale=payload["stale"],
        route_ok=bool(payload["comparison"].get("ok")),
    )

    # Resume conversation: re-ask for options with route-aware ranking
    follow = handle_chat(
        db,
        driver_id=exc.driver_id,
        message="Location shared. Show me safer slot options using the route ETA.",
        exception_id=exc.exception_id,
        shipment_id=shipment_id,
    )
    follow["eta_comparison"] = payload["comparison"]
    follow["client_action"] = None
    follow["tools_used"] = list(follow.get("tools_used") or []) + ["submit_location", "route_eta"]
    if payload["stale"]:
        ops_log.record_event(
            kind="domain",
            outcome="location_failure",
            detail=f"exception={body.exception_id} stale=true",
        )
        log_event(
            "location_failure",
            exception_id=body.exception_id,
            driver_id=exc.driver_id,
            shipment_id=shipment_id,
            reason="stale",
            stale=True,
        )
        follow["reply"] = (
            "That location snapshot looks stale. Continuing with your declared ETA. "
            + follow.get("reply", "")
        )
    elif payload["comparison"].get("ok"):
        route_eta = payload["comparison"].get("route_eta")
        declared = payload["comparison"].get("driver_declared_or_planned_eta")
        delta = payload["comparison"].get("delta_minutes")
        follow["reply"] = (
            f"Based on the location you shared, route ETA is around "
            f"{route_eta}. Driver/declared ETA remains {declared} "
            f"(delta {delta} min). Driver and route ETAs are stored separately.\n"
            + follow.get("reply", "")
        )
    else:
        ops_log.record_event(
            kind="domain",
            outcome="location_failure",
            detail=f"exception={body.exception_id} routing_failed",
        )
        log_event(
            "location_failure",
            exception_id=body.exception_id,
            driver_id=exc.driver_id,
            shipment_id=shipment_id,
            reason="routing_failed",
            route_ok=False,
        )
        follow["reply"] = (
            "Location received but routing failed — continuing with declared ETA. "
            + follow.get("reply", "")
        )
    return follow


@router.post("/location/decline")
def decline_location(body: LocationDecline, db: Session = Depends(get_db)):
    exc = db.get(DriverException, body.exception_id)
    if not exc:
        raise HTTPException(404, "Exception not found")
    shipment_id = body.shipment_id or exc.shipment_id
    location_svc.record_denied_or_error(
        db,
        exception_id=body.exception_id,
        shipment_id=shipment_id,
        status="denied",
    )
    log_event(
        "location_declined",
        exception_id=body.exception_id,
        driver_id=exc.driver_id,
        shipment_id=shipment_id,
        reason=body.reason,
    )
    result = handle_chat(
        db,
        driver_id=exc.driver_id,
        message="I do not want to share location. Continue with declared ETA.",
        exception_id=exc.exception_id,
        shipment_id=shipment_id,
    )
    result["client_action"] = None
    return result
