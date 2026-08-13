"""LangChain tool wrappers around existing SetuHaul operational services.

Business logic stays in domain/feasibility/allocator/location — tools only adapt I/O.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models import DriverException, EtaUpdate, SlotHold
from app.services import allocator, domain
from app.services.allocator import AllocationError
from app.services.feasibility import get_effective_eta, list_feasible_slots
from app.services.location import get_scheduling_eta, latest_route_eta
from app.services.timeutil import ensure_aware
from app.services import metrics as metrics_svc
import uuid


@dataclass
class AgentSession:
    """Per-request operational context shared by all tool invocations."""

    db: Session
    driver_id: str
    exception_id: str | None = None
    shipment_id: str | None = None
    options: list[dict[str, Any]] = field(default_factory=list)
    hold: dict[str, Any] | None = None
    appointment: dict[str, Any] | None = None
    needs_shipment_choice: list[dict[str, Any]] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    escalated: bool = False
    client_action: str | None = None
    eta_comparison: dict[str, Any] | None = None
    waiting_for_browser: bool = False


_SESSION: ContextVar[AgentSession | None] = ContextVar("setuhaul_agent_session", default=None)


def get_session() -> AgentSession:
    session = _SESSION.get()
    if session is None:
        raise RuntimeError("Agent session not initialized")
    return session


def set_session(session: AgentSession):
    return _SESSION.set(session)


def reset_session(token) -> None:
    _SESSION.reset(token)


def _json(data: Any) -> str:
    return json.dumps(data, default=str)


def _track(name: str) -> None:
    get_session().tools_used.append(name)


def _hold_dict(hold: SlotHold | None) -> dict[str, Any] | None:
    if not hold:
        return None
    return {
        "hold_id": hold.hold_id,
        "exception_id": hold.exception_id,
        "shipment_id": hold.shipment_id,
        "slot_id": hold.slot_id,
        "status": hold.status,
        "created_at": ensure_aware(hold.created_at),
        "expires_at": ensure_aware(hold.expires_at),
        "confirmed_at": ensure_aware(hold.confirmed_at),
    }


def _option_card(o: dict[str, Any]) -> dict[str, Any]:
    start = o["start_time"]
    end = o["end_time"]
    label = (
        f"{o.get('dock_name', o['dock_id'])} "
        f"{ensure_aware(start).strftime('%H:%M')}-{ensure_aware(end).strftime('%H:%M')}"  # type: ignore[union-attr]
    )
    return {
        "rank": o["rank"],
        "slot_id": o["slot_id"],
        "label": label,
        "lifecycle": o["lifecycle"],
        "start_time": start,
        "end_time": end,
        "dock_name": o.get("dock_name", o["dock_id"]),
        "buffer_minutes": o.get("buffer_minutes"),
    }


# ---- Tool argument schemas ----


class DriverIdArgs(BaseModel):
    driver_id: Optional[str] = Field(default=None, description="Driver ID; defaults to authenticated driver")


class ShipmentIdArgs(BaseModel):
    shipment_id: str = Field(description="Shipment ID such as SHP-1042")


class ExceptionCreateArgs(BaseModel):
    shipment_id: str
    exception_type: str = Field(default="delay")
    reported_delay_minutes: Optional[int] = None
    latest_declared_eta: Optional[str] = Field(
        default=None, description="ISO-8601 datetime if the driver declared a revised ETA"
    )
    message: Optional[str] = None


class MessageArgs(BaseModel):
    text: str
    sender_type: str = Field(default="driver")


class EtaUpdateArgs(BaseModel):
    shipment_id: str
    declared_eta: str = Field(description="ISO-8601 revised ETA")
    confidence_note: Optional[str] = None


class FeasibleSlotsArgs(BaseModel):
    shipment_id: Optional[str] = None
    after: Optional[str] = Field(default=None, description="ISO-8601 earliest slot start")
    limit: int = Field(default=5, ge=1, le=20)


class HoldArgs(BaseModel):
    slot_id: str = Field(description="Exact slot_id returned by list_feasible_slots")
    exception_id: Optional[str] = None
    idempotency_key: Optional[str] = None


class HoldIdArgs(BaseModel):
    hold_id: str
    idempotency_key: Optional[str] = None


class EmptyArgs(BaseModel):
    pass


def _tool_get_driver(driver_id: Optional[str] = None) -> str:
    s = get_session()
    _track("get_driver")
    did = driver_id or s.driver_id
    driver = domain.get_driver(s.db, did)
    if not driver:
        return _json({"error": "driver_not_found", "driver_id": did})
    return _json(
        {
            "driver_id": driver.driver_id,
            "name": driver.name,
            "carrier_id": driver.carrier_id,
            "status": driver.status,
            "home_base": driver.home_base,
        }
    )


def _tool_list_active_shipments(driver_id: Optional[str] = None) -> str:
    s = get_session()
    _track("list_active_shipments")
    did = driver_id or s.driver_id
    rows = domain.list_active_shipments(s.db, did)
    payload = [domain.shipment_to_dict(s.db, sh) for sh in rows]
    if len(payload) > 1 and not s.shipment_id:
        s.needs_shipment_choice = payload
    return _json({"count": len(payload), "shipments": payload})


def _tool_get_shipment(shipment_id: str) -> str:
    s = get_session()
    _track("get_shipment")
    sh = domain.get_shipment(s.db, shipment_id)
    if not sh:
        return _json({"error": "shipment_not_found", "shipment_id": shipment_id})
    s.shipment_id = shipment_id
    return _json(domain.shipment_to_dict(s.db, sh))


def _tool_get_appointment(shipment_id: str) -> str:
    s = get_session()
    _track("get_appointment")
    ctx = domain.get_appointment_context(s.db, shipment_id)
    s.appointment = ctx
    return _json(ctx or {"appointment": None, "note": "no_active_appointment"})


def _tool_get_effective_eta(shipment_id: str) -> str:
    s = get_session()
    _track("get_effective_eta")
    sh = domain.get_shipment(s.db, shipment_id)
    if not sh:
        return _json({"error": "shipment_not_found"})
    eta = get_effective_eta(s.db, sh)
    sched_eta, source = get_scheduling_eta(s.db, sh, s.exception_id)
    route = latest_route_eta(s.db, s.exception_id) if s.exception_id else None
    return _json(
        {
            "effective_eta": eta,
            "scheduling_eta": sched_eta,
            "scheduling_eta_source": source,
            "route_eta": route.route_eta if route else None,
            "note": "ETAs come only from operational data; do not invent times.",
        }
    )


def _tool_create_exception(
    shipment_id: str,
    exception_type: str = "delay",
    reported_delay_minutes: Optional[int] = None,
    latest_declared_eta: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    s = get_session()
    _track("create_exception")
    declared = datetime.fromisoformat(latest_declared_eta) if latest_declared_eta else None
    existing = domain.get_open_exception(s.db, s.driver_id, shipment_id)
    if existing:
        s.exception_id = existing.exception_id
        s.shipment_id = shipment_id
        if message:
            domain.add_message(s.db, existing.exception_id, sender_type="driver", text=message)
        metrics_svc.ensure_case_metric(s.db, existing.exception_id, shipment_id)
        return _json(
            {
                "exception_id": existing.exception_id,
                "status": existing.status,
                "reused_existing": True,
            }
        )
    exc = domain.create_exception(
        s.db,
        driver_id=s.driver_id,
        shipment_id=shipment_id,
        exception_type=exception_type,
        reported_delay_minutes=reported_delay_minutes,
        latest_declared_eta=declared,
        message=message,
    )
    s.exception_id = exc.exception_id
    s.shipment_id = shipment_id
    metrics_svc.ensure_case_metric(s.db, exc.exception_id, shipment_id)
    return _json(
        {
            "exception_id": exc.exception_id,
            "status": exc.status,
            "shipment_id": shipment_id,
            "reused_existing": False,
        }
    )


def _tool_add_message(text: str, sender_type: str = "driver") -> str:
    s = get_session()
    _track("add_message")
    if not s.exception_id:
        return _json({"error": "no_exception_id", "hint": "Call create_exception first"})
    msg = domain.add_message(s.db, s.exception_id, sender_type=sender_type, text=text)
    return _json({"message_id": msg.message_id, "exception_id": s.exception_id})


def _tool_eta_update(
    shipment_id: str,
    declared_eta: str,
    confidence_note: Optional[str] = None,
) -> str:
    s = get_session()
    _track("eta_update")
    from app.config import get_settings

    eta = datetime.fromisoformat(declared_eta)
    now = ensure_aware(get_settings().now())
    row = EtaUpdate(
        eta_update_id=f"ETA-{uuid.uuid4().hex[:10].upper()}",
        shipment_id=shipment_id,
        declared_eta=eta,
        source_type="driver",
        declared_at=now,  # type: ignore[arg-type]
        confidence_note=confidence_note or "agent_tool",
    )
    s.db.add(row)
    if s.exception_id:
        exc = s.db.get(DriverException, s.exception_id)
        if exc:
            exc.latest_declared_eta = eta
    s.db.commit()
    return _json({"shipment_id": shipment_id, "declared_eta": eta, "stored": True})


def _tool_list_feasible_slots(
    shipment_id: Optional[str] = None,
    after: Optional[str] = None,
    limit: int = 5,
) -> str:
    s = get_session()
    _track("list_feasible_slots")
    sid = shipment_id or s.shipment_id
    if not sid:
        return _json({"error": "shipment_id_required"})
    sh = domain.get_shipment(s.db, sid)
    if not sh:
        return _json({"error": "shipment_not_found"})
    if not s.exception_id:
        # Ensure an exception thread exists so option_views can be recorded
        created = domain.create_exception(
            s.db,
            driver_id=s.driver_id,
            shipment_id=sid,
            exception_type="delay",
            message="agent_list_feasible_slots",
        )
        s.exception_id = created.exception_id
        metrics_svc.ensure_case_metric(s.db, created.exception_id, sid)
    exc = s.db.get(DriverException, s.exception_id)
    assert exc is not None
    after_dt = datetime.fromisoformat(after) if after else None
    options = allocator.mark_options_shown(s.db, exc, sh, after=after_dt, limit=limit)
    s.options = [_option_card(o) for o in options if o.get("lifecycle") == "shown"]
    s.shipment_id = sid
    if not options:
        exc.status = "escalated"
        s.db.commit()
        s.escalated = True
        metrics_svc.mark_resolved(s.db, exc.exception_id, status="escalated", human=True)
        return _json(
            {
                "options": [],
                "count": 0,
                "escalated": True,
                "message": (
                    "No feasible same-day slot matches vehicle, ETA, and facility rules. "
                    "Escalate to operations. Do not invent a slot."
                ),
            }
        )
    s.escalated = False
    return _json(
        {
            "options": s.options,
            "count": len(s.options),
            "lifecycle_note": "shown ≠ held ≠ confirmed",
            "escalated": False,
        }
    )


def _tool_create_hold(
    slot_id: str,
    exception_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> str:
    s = get_session()
    _track("create_hold")
    eid = exception_id or s.exception_id
    if not eid:
        return _json({"error": "exception_id_required"})
    # Reject invented slots: must appear in last shown options or DB open slots for shipment
    known_ids = {o["slot_id"] for o in s.options}
    if known_ids and slot_id not in known_ids:
        return _json(
            {
                "error": "unknown_slot_id",
                "message": "slot_id was not returned by list_feasible_slots; refuse to invent holds",
                "known_slot_ids": sorted(known_ids),
            }
        )
    try:
        hold = allocator.create_hold(
            s.db,
            exception_id=eid,
            slot_id=slot_id,
            idempotency_key=idempotency_key,
        )
    except AllocationError as exc:
        return _json({"error": exc.code, "message": exc.message})
    s.hold = _hold_dict(hold)
    s.exception_id = eid
    return _json({"hold": s.hold, "lifecycle": "held"})


def _tool_confirm_hold(hold_id: str, idempotency_key: Optional[str] = None) -> str:
    s = get_session()
    _track("confirm_hold")
    try:
        hold, appt = allocator.confirm_hold(s.db, hold_id, idempotency_key=idempotency_key)
    except AllocationError as exc:
        return _json({"error": exc.code, "message": exc.message})
    s.hold = _hold_dict(hold)
    s.appointment = domain.appointment_to_dict(s.db, appt)
    if s.exception_id:
        shipment = domain.get_shipment(s.db, hold.shipment_id)
        kwargs: dict[str, Any] = {"status": "confirmed", "human": False}
        if shipment is not None:
            confirm_m = metrics_svc.confirm_wait_and_first_option(
                s.db,
                shipment=shipment,
                exception_id=s.exception_id,
                new_appointment=appt,
            )
            kwargs.update(
                {
                    "first_option_accepted": confirm_m["first_option_accepted"],
                    "eta_source_used": confirm_m["eta_source_used"],
                    "predicted_eta": confirm_m["predicted_eta"],
                    "old_wait": confirm_m["old_wait"],
                    "new_wait": confirm_m["new_wait"],
                }
            )
        metrics_svc.mark_resolved(s.db, s.exception_id, **kwargs)
    return _json({"hold": s.hold, "appointment": s.appointment, "lifecycle": "confirmed"})


def _tool_release_hold(hold_id: str) -> str:
    s = get_session()
    _track("release_hold")
    try:
        hold = allocator.release_hold(s.db, hold_id)
    except AllocationError as exc:
        return _json({"error": exc.code, "message": exc.message})
    s.hold = _hold_dict(hold)
    return _json({"hold": s.hold, "lifecycle": hold.status})


def _tool_get_route_eta_context() -> str:
    s = get_session()
    _track("get_route_eta_context")
    if not s.exception_id or not s.shipment_id:
        return _json({"error": "exception_and_shipment_required"})
    sh = domain.get_shipment(s.db, s.shipment_id)
    if not sh:
        return _json({"error": "shipment_not_found"})
    sched_eta, source = get_scheduling_eta(s.db, sh, s.exception_id)
    route = latest_route_eta(s.db, s.exception_id)
    return _json(
        {
            "scheduling_eta": sched_eta,
            "source": source,
            "route_eta": route.route_eta if route else None,
            "provider": route.provider if route else None,
            "note": "Driver-declared and route ETAs are stored separately.",
        }
    )


def _tool_request_browser_location() -> str:
    """Pause for frontend one-time geolocation (Advanced AddOns PDF)."""
    from app.services import location_consent

    s = get_session()
    _track("request_browser_location")
    if not s.exception_id:
        return _json({"error": "exception_id_required", "message": "Create an exception first."})
    exc = s.db.get(DriverException, s.exception_id)
    if not exc:
        return _json({"error": "exception_not_found"})
    result = location_consent.request_browser_location(s.db, exc)
    s.client_action = "REQUEST_BROWSER_LOCATION"
    s.waiting_for_browser = True
    s.shipment_id = exc.shipment_id
    return _json(
        {
            "client_action": "REQUEST_BROWSER_LOCATION",
            "waiting_for_browser": True,
            "message": result["reply"],
            "instruction": (
                "Stop and tell the driver to tap Share location. "
                "Do not invent coordinates. Do not call list_feasible_slots until location "
                "is shared or declined."
            ),
        }
    )


def build_tools() -> list[StructuredTool]:
    """Build LangChain StructuredTools bound to the current AgentSession context."""

    return [
        StructuredTool.from_function(
            name="get_driver",
            description="Get authenticated driver profile.",
            func=_tool_get_driver,
            args_schema=DriverIdArgs,
        ),
        StructuredTool.from_function(
            name="list_active_shipments",
            description="List active shipments for the driver. If more than one, ask the driver which shipment.",
            func=_tool_list_active_shipments,
            args_schema=DriverIdArgs,
        ),
        StructuredTool.from_function(
            name="get_shipment",
            description="Get shipment details including planned ETA, priority, destination.",
            func=_tool_get_shipment,
            args_schema=ShipmentIdArgs,
        ),
        StructuredTool.from_function(
            name="get_appointment",
            description="Get the current appointment and whether it is still feasible.",
            func=_tool_get_appointment,
            args_schema=ShipmentIdArgs,
        ),
        StructuredTool.from_function(
            name="get_effective_eta",
            description="Get operational ETA (gate-in / declared / planned). Never invent an ETA.",
            func=_tool_get_effective_eta,
            args_schema=ShipmentIdArgs,
        ),
        StructuredTool.from_function(
            name="create_exception",
            description="Open or reuse a driver exception thread for a shipment.",
            func=_tool_create_exception,
            args_schema=ExceptionCreateArgs,
        ),
        StructuredTool.from_function(
            name="add_message",
            description="Persist a chat message on the exception thread.",
            func=_tool_add_message,
            args_schema=MessageArgs,
        ),
        StructuredTool.from_function(
            name="eta_update",
            description="Store a driver-declared revised ETA in operational data.",
            func=_tool_eta_update,
            args_schema=EtaUpdateArgs,
        ),
        StructuredTool.from_function(
            name="list_feasible_slots",
            description=(
                "Return feasible appointment slots from the deterministic allocation engine. "
                "Showing options does NOT reserve them. If empty, escalate — do not invent slots."
            ),
            func=_tool_list_feasible_slots,
            args_schema=FeasibleSlotsArgs,
        ),
        StructuredTool.from_function(
            name="create_hold",
            description=(
                "Hold a slot_id previously returned by list_feasible_slots using Redis SET NX. "
                "Only then is the slot reserved."
            ),
            func=_tool_create_hold,
            args_schema=HoldArgs,
        ),
        StructuredTool.from_function(
            name="confirm_hold",
            description="Confirm an active hold into a booking. Only then is the appointment confirmed.",
            func=_tool_confirm_hold,
            args_schema=HoldIdArgs,
        ),
        StructuredTool.from_function(
            name="release_hold",
            description="Release an active hold so the slot is no longer reserved for this driver.",
            func=_tool_release_hold,
            args_schema=HoldIdArgs,
        ),
        StructuredTool.from_function(
            name="get_route_eta_context",
            description="Get route vs declared ETA context if a location snapshot exists.",
            func=_tool_get_route_eta_context,
            args_schema=EmptyArgs,
        ),
        StructuredTool.from_function(
            name="request_browser_location",
            description=(
                "Pause the workflow and ask the frontend for a one-time browser location "
                "snapshot (REQUEST_BROWSER_LOCATION). Call this when the driver agrees to "
                "share location. Do not invent coordinates."
            ),
            func=_tool_request_browser_location,
            args_schema=EmptyArgs,
        ),
    ]


TOOL_NAMES = [
    "get_driver",
    "list_active_shipments",
    "get_shipment",
    "get_appointment",
    "get_effective_eta",
    "create_exception",
    "add_message",
    "eta_update",
    "list_feasible_slots",
    "create_hold",
    "confirm_hold",
    "release_hold",
    "get_route_eta_context",
    "request_browser_location",
]
