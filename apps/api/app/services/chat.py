"""Deterministic chat orchestrator.

Phase 1: rule-based NLU that ONLY surfaces capacity via domain/allocator tools.
No availability is invented in replies.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DriverException, SlotHold
from app.services import allocator, domain
from app.services.allocator import AllocationError
from app.services.feasibility import get_effective_eta
from app.services.timeutil import IST, ensure_aware
from app.services import metrics as metrics_svc
from app.services.observability import trace_event
from app.services.location import latest_location, latest_route_eta


def _base_result(**kwargs: Any) -> dict[str, Any]:
    out = {
        "client_action": None,
        "eta_comparison": None,
        "waiting_for_browser": False,
        "needs_shipment_choice": [],
        "hold": None,
        "appointment": None,
        "options": [],
        "escalated": False,
        "tools_used": [],
    }
    out.update(kwargs)
    return out


def _wants_location_share(text: str) -> bool:
    lower = text.lower().strip()
    return lower in {"yes", "y", "share", "share location", "ok", "sure"} or "share my location" in lower


def _declines_location(text: str) -> bool:
    lower = text.lower()
    return any(
        k in lower
        for k in [
            "no location",
            "don't share",
            "do not share",
            "dont share",
            "without location",
            "skip location",
            "declared eta",
            "continue with declared",
            "no thanks",
            "i do not want to share",
        ]
    ) or lower.strip() in {"no thanks"}


def _asks_location_prompt(text: str) -> bool:
    lower = text.lower()
    return "location" in lower and any(k in lower for k in ["share", "send", "use", "gps"])


def _fmt(dt: datetime | None) -> str:
    if not dt:
        return "unknown"
    return ensure_aware(dt).strftime("%H:%M")  # type: ignore[union-attr]


def _parse_delay_minutes(text: str) -> int | None:
    patterns = [
        r"(\d+)\s*(?:min|mins|minutes)",
        r"(\d+)\s*(?:hr|hrs|hour|hours)",
        r"around\s+(\d+)\s*(?:min|minutes|hr|hours)?",
        r"(\d+)\s*minutes?\s*late",
    ]
    lower = text.lower()
    for pat in patterns:
        m = re.search(pat, lower)
        if m:
            val = int(m.group(1))
            if "hr" in pat or "hour" in lower[m.start() : m.end() + 6]:
                return val * 60
            return val
    if "two hours" in lower or "2 hours" in lower:
        return 120
    if "one hour" in lower or "an hour" in lower:
        return 60
    return None


def _parse_after_time(text: str, now: datetime) -> datetime | None:
    lower = text.lower()
    m = re.search(r"after\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", lower)
    if not m:
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", lower)
        if not m:
            return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    # Assume same day scenario date
    base = now.astimezone(IST)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate


def _parse_choice_rank(text: str) -> int | None:
    lower = text.lower().strip()
    mapping = {
        "1": 1,
        "first": 1,
        "option 1": 1,
        "take the first": 1,
        "2": 2,
        "second": 2,
        "option 2": 2,
        "take the second": 2,
        "the second one": 2,
        "3": 3,
        "third": 3,
        "option 3": 3,
    }
    for key, rank in mapping.items():
        if key in lower:
            return rank
    m = re.search(r"(?:option|slot)\s*#?\s*(\d+)", lower)
    if m:
        return int(m.group(1))
    return None


def _wants_options(text: str) -> bool:
    lower = text.lower()
    return any(
        k in lower
        for k in [
            "slot",
            "option",
            "after",
            "reschedule",
            "alternative",
            "what are",
            "available",
            "next",
            "book",
        ]
    )


def _wants_status(text: str) -> bool:
    lower = text.lower()
    if lower.strip() in {"confirm", "confirm it", "confirm booking", "confirm hold"}:
        return False
    return any(k in lower for k in ["status", "has it been", "booked", "is it held", "my hold"])


def _wants_confirm(text: str) -> bool:
    lower = text.lower().strip()
    return lower in {"confirm", "confirm it", "confirm booking", "confirm hold"} or (
        "confirm" in lower and "has" not in lower and "status" not in lower
    )


def _wants_cancel(text: str) -> bool:
    lower = text.lower()
    return any(k in lower for k in ["cancel", "do not book", "don't book", "release"])


def _is_report(text: str) -> bool:
    lower = text.lower()
    return any(
        k in lower
        for k in ["late", "delay", "stuck", "tyre", "tire", "breakdown", "traffic", "repair"]
    )


def handle_chat(
    db: Session,
    *,
    driver_id: str,
    message: str,
    exception_id: str | None = None,
    shipment_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    tools_used: list[str] = []
    now = ensure_aware(get_settings().now())
    assert now is not None

    if idempotency_key:
        cached = allocator.get_idempotent_result(f"chat:{idempotency_key}")
        if cached:
            return cached

    driver = domain.get_driver(db, driver_id)
    if not driver:
        return {
            "exception_id": exception_id or "",
            "shipment_id": shipment_id or "",
            "reply": "I could not find that driver ID. Please check your login.",
            "status": "escalated",
            "options": [],
            "escalated": True,
            "tools_used": ["get_driver"],
            "needs_shipment_choice": [],
            "hold": None,
            "appointment": None,
        }

    shipments = domain.list_active_shipments(db, driver_id)
    tools_used.append("list_active_shipments")

    exception: DriverException | None = None
    if exception_id:
        exception = db.get(DriverException, exception_id)

    # Shipment disambiguation
    if not shipment_id and exception:
        shipment_id = exception.shipment_id
    if not shipment_id:
        if len(shipments) == 0:
            return {
                "exception_id": "",
                "shipment_id": "",
                "reply": "I cannot find an active shipment for you. Escalating to operations.",
                "status": "escalated",
                "options": [],
                "escalated": True,
                "tools_used": tools_used,
                "needs_shipment_choice": [],
                "hold": None,
                "appointment": None,
            }
        if len(shipments) > 1 and not exception:
            choices = [domain.shipment_to_dict(db, s) for s in shipments]
            return {
                "exception_id": "",
                "shipment_id": "",
                "reply": (
                    "You have more than one active shipment. "
                    "Please reply with the shipment ID to continue "
                    f"({', '.join(s.shipment_id for s in shipments)})."
                ),
                "status": "open",
                "options": [],
                "escalated": False,
                "tools_used": tools_used,
                "needs_shipment_choice": choices,
                "hold": None,
                "appointment": None,
            }
        shipment_id = shipments[0].shipment_id

    # Allow selecting shipment by ID in message
    for s in shipments:
        if s.shipment_id.lower() in message.lower():
            shipment_id = s.shipment_id
            break

    shipment = domain.get_shipment(db, shipment_id)  # type: ignore[arg-type]
    tools_used.append("get_shipment")
    if not shipment:
        return {
            "exception_id": exception_id or "",
            "shipment_id": shipment_id or "",
            "reply": "That shipment was not found. Escalating to operations.",
            "status": "escalated",
            "options": [],
            "escalated": True,
            "tools_used": tools_used,
            "needs_shipment_choice": [],
            "hold": None,
            "appointment": None,
        }

    if not exception:
        exception = domain.get_open_exception(db, driver_id, shipment.shipment_id)

    delay_minutes = _parse_delay_minutes(message)
    declared_eta = None
    if delay_minutes is not None:
        # Repair delay is NOT automatically the ETA shift — we ask / use explicit after-time if present
        after = _parse_after_time(message, now)
        if after:
            declared_eta = after
        else:
            # Use planned_eta + delay only as a provisional declared ETA, noted as uncertain
            declared_eta = ensure_aware(shipment.planned_eta) + timedelta(minutes=delay_minutes)

    after_pref = _parse_after_time(message, now)

    needs_exception = (
        _is_report(message)
        or delay_minutes is not None
        or _wants_options(message)
        or _parse_choice_rank(message) is not None
        or _wants_status(message)
        or _wants_cancel(message)
        or "confirm" in message.lower()
    )
    if not exception and needs_exception:
        exception = domain.create_exception(
            db,
            driver_id=driver_id,
            shipment_id=shipment.shipment_id,
            exception_type="tyre_issue" if "tyre" in message.lower() or "tire" in message.lower() else "delay",
            reported_delay_minutes=delay_minutes,
            latest_declared_eta=declared_eta,
            message=message,
        )
        tools_used.append("create_exception")
    elif exception:
        domain.add_message(db, exception.exception_id, sender_type="driver", text=message)
        tools_used.append("add_message")
        if declared_eta:
            exception.latest_declared_eta = declared_eta
            from app.models import EtaUpdate
            import uuid

            db.add(
                EtaUpdate(
                    eta_update_id=f"ETA-{uuid.uuid4().hex[:10].upper()}",
                    shipment_id=shipment.shipment_id,
                    declared_eta=declared_eta,
                    source_type="driver",
                    declared_at=now,
                    confidence_note="chat_update",
                )
            )
            db.commit()
            tools_used.append("eta_update")

    if exception is None:
        reply = (
            "Tell me about your delay or ask for slots (for example: "
            "'I will be late, what slots after 7 PM?')."
        )
        return _base_result(
            exception_id="",
            shipment_id=shipment.shipment_id,
            reply=reply,
            status="open",
            tools_used=tools_used,
            appointment=domain.get_appointment_context(db, shipment.shipment_id),
        )

    metrics_svc.ensure_case_metric(db, exception.exception_id, shipment.shipment_id)
    state = __import__("json").loads(exception.conversation_state or "{}")

    # --- Phase 2 location consent / interrupt ---
    loc = latest_location(db, exception.exception_id)
    skip_location = (
        "continue with" in message.lower()
        or "location shared" in message.lower()
        or "location unavailable" in message.lower()
        or (
            state.get("location_prompted")
            and (_declines_location(message) or message.lower().strip() in {"no", "n"})
        )
    )
    if skip_location:
        state["location_prompted"] = True
        state["location_skipped"] = True
        domain.update_exception_state(db, exception, state)

    if _wants_location_share(message) and state.get("location_prompted") and not loc:
        state["waiting_for_browser"] = True
        domain.update_exception_state(db, exception, state)
        reply = (
            "Please tap Share location so the browser can capture a one-time snapshot. "
            "I will pause until the frontend returns coordinates (or a denial)."
        )
        domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
        trace_event("request_browser_location", {"exception_id": exception.exception_id})
        return _base_result(
            exception_id=exception.exception_id,
            shipment_id=shipment.shipment_id,
            reply=reply,
            status=exception.status,
            tools_used=tools_used + ["REQUEST_BROWSER_LOCATION"],
            client_action="REQUEST_BROWSER_LOCATION",
            waiting_for_browser=True,
            appointment=domain.get_appointment_context(db, shipment.shipment_id),
        )

    appt_ctx = domain.get_appointment_context(db, shipment.shipment_id)
    tools_used.append("get_appointment")

    # Status check
    if _wants_status(message) and not _wants_options(message) and _parse_choice_rank(message) is None:
        hold = (
            db.query(SlotHold)
            .filter(
                SlotHold.exception_id == exception.exception_id,
                SlotHold.status.in_(["held", "confirmed"]),
            )
            .order_by(SlotHold.created_at.desc())
            .first()
        )
        if exception.status == "confirmed" and appt_ctx:
            reply = (
                f"Your new appointment is confirmed on slot {appt_ctx['slot_id']} "
                f"({_fmt(appt_ctx.get('slot_start'))}-{_fmt(appt_ctx.get('slot_end'))})."
            )
        elif hold and hold.status == "held":
            reply = (
                f"Slot {hold.slot_id} is currently HELD for you until {_fmt(hold.expires_at)}. "
                "Reply 'confirm' to commit the booking, or 'cancel' to release it."
            )
        elif exception.status == "escalated":
            reply = "This case is escalated to operations. No automated slot was confirmed."
        else:
            reply = (
                f"Current status: {exception.status}. "
                "Showing options is not a reservation. "
                "A hold is required before confirmation."
            )
        domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
        result = {
            "exception_id": exception.exception_id,
            "shipment_id": shipment.shipment_id,
            "reply": reply,
            "status": exception.status,
            "options": [],
            "escalated": exception.status == "escalated",
            "tools_used": tools_used,
            "needs_shipment_choice": [],
            "hold": _hold_dict(hold) if hold else None,
            "appointment": appt_ctx,
        }
        if idempotency_key:
            allocator.store_idempotent_result(f"chat:{idempotency_key}", result)
        return result

    # Confirm hold
    if _wants_confirm(message) and _parse_choice_rank(message) is None:
        hold = (
            db.query(SlotHold)
            .filter(SlotHold.exception_id == exception.exception_id, SlotHold.status == "held")
            .order_by(SlotHold.created_at.desc())
            .first()
        )
        if not hold:
            reply = "There is no active hold to confirm. Ask for options and choose a slot first."
            domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
            return {
                "exception_id": exception.exception_id,
                "shipment_id": shipment.shipment_id,
                "reply": reply,
                "status": exception.status,
                "options": [],
                "escalated": False,
                "tools_used": tools_used,
                "needs_shipment_choice": [],
                "hold": None,
                "appointment": appt_ctx,
            }
        try:
            hold, appointment = allocator.confirm_hold(
                db, hold.hold_id, idempotency_key=idempotency_key
            )
            tools_used.append("confirm_hold")
            appt_ctx = domain.appointment_to_dict(db, appointment)
            confirm_m = metrics_svc.confirm_wait_and_first_option(
                db,
                shipment=shipment,
                exception_id=exception.exception_id,
                new_appointment=appointment,
            )
            metrics_svc.mark_resolved(
                db,
                exception.exception_id,
                status="confirmed",
                human=False,
                first_option_accepted=confirm_m["first_option_accepted"],
                eta_source_used=confirm_m["eta_source_used"],
                predicted_eta=confirm_m["predicted_eta"],
                old_wait=confirm_m["old_wait"],
                new_wait=confirm_m["new_wait"],
            )
            reply = (
                f"Confirmed. Appointment {appointment.appointment_id} is booked on "
                f"{appointment.slot_id} ({_fmt(appt_ctx.get('slot_start') if appt_ctx else None)}-"
                f"{_fmt(appt_ctx.get('slot_end') if appt_ctx else None)}). "
                "Lifecycle: confirmed."
            )
            domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
            result = _base_result(
                exception_id=exception.exception_id,
                shipment_id=shipment.shipment_id,
                reply=reply,
                status="confirmed",
                tools_used=tools_used,
                hold=_hold_dict(hold),
                appointment=appt_ctx,
            )
            if idempotency_key:
                allocator.store_idempotent_result(f"chat:{idempotency_key}", result)
            return result
        except AllocationError as exc:
            reply = f"Could not confirm: {exc.message}. Please request fresh options."
            domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
            return {
                "exception_id": exception.exception_id,
                "shipment_id": shipment.shipment_id,
                "reply": reply,
                "status": exception.status,
                "options": [],
                "escalated": False,
                "tools_used": tools_used,
                "needs_shipment_choice": [],
                "hold": None,
                "appointment": appt_ctx,
            }

    # Cancel / release
    if _wants_cancel(message):
        hold = (
            db.query(SlotHold)
            .filter(SlotHold.exception_id == exception.exception_id, SlotHold.status == "held")
            .order_by(SlotHold.created_at.desc())
            .first()
        )
        if hold:
            allocator.release_hold(db, hold.hold_id)
            tools_used.append("release_hold")
            reply = f"Released hold on {hold.slot_id}. It is no longer reserved for you."
        else:
            reply = "No active hold to cancel. Confirmed appointments must be cancelled via operations for now."
        domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
        return {
            "exception_id": exception.exception_id,
            "shipment_id": shipment.shipment_id,
            "reply": reply,
            "status": exception.status,
            "options": [],
            "escalated": False,
            "tools_used": tools_used,
            "needs_shipment_choice": [],
            "hold": None,
            "appointment": appt_ctx,
        }

    # Choose option by rank
    rank = _parse_choice_rank(message)
    if rank is not None:
        # Ensure we have current shown options
        options = allocator.mark_options_shown(
            db, exception, shipment, after=after_pref, limit=5
        )
        tools_used.append("list_feasible_slots")
        match = next((o for o in options if o["rank"] == rank and o["lifecycle"] == "shown"), None)
        if not match:
            reply = (
                "That option is no longer available (stale or held by another driver). "
                "Here are the current feasible slots from the allocation engine."
            )
            domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
            if not options:
                exception.status = "escalated"
                db.commit()
                reply = (
                    "There is no feasible same-day slot for your truck and constraints. "
                    "Escalating to operations — I will not invent a slot."
                )
                domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
                return {
                    "exception_id": exception.exception_id,
                    "shipment_id": shipment.shipment_id,
                    "reply": reply,
                    "status": "escalated",
                    "options": [],
                    "escalated": True,
                    "tools_used": tools_used,
                    "needs_shipment_choice": [],
                    "hold": None,
                    "appointment": appt_ctx,
                }
            return {
                "exception_id": exception.exception_id,
                "shipment_id": shipment.shipment_id,
                "reply": reply,
                "status": exception.status,
                "options": [_option_card(o) for o in options],
                "escalated": False,
                "tools_used": tools_used,
                "needs_shipment_choice": [],
                "hold": None,
                "appointment": appt_ctx,
            }
        try:
            hold = allocator.create_hold(
                db,
                exception_id=exception.exception_id,
                slot_id=match["slot_id"],
                idempotency_key=idempotency_key,
            )
            tools_used.append("create_hold")
            reply = (
                f"Held option {rank}: {match['dock_name']} "
                f"{_fmt(match['start_time'])}-{_fmt(match['end_time'])} "
                f"(slot {match['slot_id']}). Lifecycle: held until {_fmt(hold.expires_at)}. "
                "Reply 'confirm' to commit, or choose another option."
            )
            domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
            result = {
                "exception_id": exception.exception_id,
                "shipment_id": shipment.shipment_id,
                "reply": reply,
                "status": "held",
                "options": [_option_card({**match, "lifecycle": "held"})],
                "escalated": False,
                "tools_used": tools_used,
                "needs_shipment_choice": [],
                "hold": _hold_dict(hold),
                "appointment": appt_ctx,
            }
            if idempotency_key:
                allocator.store_idempotent_result(f"chat:{idempotency_key}", result)
            return result
        except AllocationError as exc:
            # Refresh options after contention
            options = allocator.mark_options_shown(db, exception, shipment, after=after_pref, limit=5)
            tools_used.append("list_feasible_slots")
            reply = (
                f"Could not hold that slot: {exc.message}. "
                "Availability changed. Fresh options from the allocation engine:"
            )
            domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
            if not options:
                exception.status = "escalated"
                db.commit()
                reply = (
                    "No feasible slots remain. Escalating to operations without inventing availability."
                )
                domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
                return {
                    "exception_id": exception.exception_id,
                    "shipment_id": shipment.shipment_id,
                    "reply": reply,
                    "status": "escalated",
                    "options": [],
                    "escalated": True,
                    "tools_used": tools_used,
                    "needs_shipment_choice": [],
                    "hold": None,
                    "appointment": appt_ctx,
                }
            return {
                "exception_id": exception.exception_id,
                "shipment_id": shipment.shipment_id,
                "reply": reply,
                "status": exception.status,
                "options": [_option_card(o) for o in options],
                "escalated": False,
                "tools_used": tools_used,
                "needs_shipment_choice": [],
                "hold": None,
                "appointment": appt_ctx,
            }

    # Default: report / options flow
    effective_eta = get_effective_eta(db, shipment)
    tools_used.append("get_effective_eta")

    original_ok = bool(appt_ctx and appt_ctx.get("is_feasible"))
    lines: list[str] = []
    lines.append(
        f"Shipment {shipment.shipment_id} to {shipment.destination_id}. "
        f"Planned ETA {_fmt(shipment.planned_eta)}; effective ETA {_fmt(effective_eta)}."
    )
    if delay_minutes is not None and declared_eta and after_pref is None:
        lines.append(
            f"Noted ~{delay_minutes} minutes impact as a provisional ETA shift. "
            "Repair time is not always the same as arrival impact — "
            "send an explicit arrival time if you can (e.g. 'after 7 PM')."
        )
    if appt_ctx:
        if original_ok:
            lines.append(
                f"Your current appointment {appt_ctx['appointment_id']} "
                f"({_fmt(appt_ctx.get('slot_start'))}) still looks feasible."
            )
        else:
            lines.append(
                f"Your current appointment {appt_ctx['appointment_id']} "
                f"({_fmt(appt_ctx.get('slot_start'))}) is no longer feasible "
                f"({', '.join(appt_ctx.get('infeasible_reasons') or [])})."
            )

    should_show = _wants_options(message) or not original_ok or _is_report(message)
    options: list[dict[str, Any]] = []
    client_action = None
    waiting_for_browser = False

    # Offer one-time location before first options (PDF add-on); skip if already handled
    offer_location = (
        should_show
        and not state.get("location_prompted")
        and not state.get("location_skipped")
        and not loc
        and not skip_location
        and "location shared" not in message.lower()
    )
    if offer_location and (_is_report(message) or _asks_location_prompt(message) or _wants_options(message)):
        state["location_prompted"] = True
        domain.update_exception_state(db, exception, state)
        lines.append(
            "Would you like to share your current location once? "
            "It can improve the ETA buffer for slot suggestions. "
            "Reply 'yes' to share, or 'no' / ask for slots to continue with your declared ETA."
        )
        # If they already asked for slots in the same message, still show options below
        if not _wants_options(message) and not after_pref:
            reply = "\n".join(lines)
            domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
            return _base_result(
                exception_id=exception.exception_id,
                shipment_id=shipment.shipment_id,
                reply=reply,
                status=exception.status,
                tools_used=tools_used,
                appointment=appt_ctx,
            )

    if should_show:
        from app.services.location import get_scheduling_eta

        sched_eta, eta_src = get_scheduling_eta(db, shipment, exception.exception_id)
        options = allocator.mark_options_shown(
            db, exception, shipment, after=after_pref or sched_eta, limit=5
        )
        tools_used.append("list_feasible_slots")
        if eta_src == "route":
            route = latest_route_eta(db, exception.exception_id)
            if route and route.route_eta:
                lines.append(
                    f"Ranking uses route ETA {_fmt(route.route_eta)} "
                    f"(driver-declared ETA kept separate)."
                )
        if not options:
            exception.status = "escalated"
            db.commit()
            metrics_svc.mark_resolved(
                db, exception.exception_id, status="escalated", human=True
            )
            lines.append(
                "No feasible same-day slot matches your truck, ETA, and facility rules. "
                "Escalating to operations — I will not invent a slot."
            )
            reply = "\n".join(lines)
            domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
            return _base_result(
                exception_id=exception.exception_id,
                shipment_id=shipment.shipment_id,
                reply=reply,
                status="escalated",
                escalated=True,
                tools_used=tools_used,
                appointment=appt_ctx,
            )
        lines.append(
            "Feasible options from the allocation engine (shown ≠ held ≠ confirmed):"
        )
        for o in options:
            lines.append(
                f"{o['rank']}. {o['dock_name']} {_fmt(o['start_time'])}-{_fmt(o['end_time'])} "
                f"(buffer {o['buffer_minutes']} min, {o['lifecycle']})"
            )
        lines.append("Reply with '1', '2', ... to hold an option.")
        if state.get("location_prompted") and not loc and not state.get("location_skipped"):
            lines.append("You can still reply 'yes' to share location for a safer buffer.")

    reply = "\n".join(lines)
    domain.add_message(db, exception.exception_id, sender_type="agent", text=reply)
    result = _base_result(
        exception_id=exception.exception_id,
        shipment_id=shipment.shipment_id,
        reply=reply,
        status=exception.status,
        options=[_option_card(o) for o in options],
        escalated=exception.status == "escalated",
        tools_used=tools_used,
        appointment=appt_ctx,
        client_action=client_action,
        waiting_for_browser=waiting_for_browser,
    )
    if idempotency_key:
        allocator.store_idempotent_result(f"chat:{idempotency_key}", result)
    return result


def _option_card(o: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": o["rank"],
        "slot_id": o["slot_id"],
        "label": f"{o.get('dock_name', o['dock_id'])} {_fmt(o['start_time'])}-{_fmt(o['end_time'])}",
        "lifecycle": o["lifecycle"],
        "start_time": o["start_time"],
        "end_time": o["end_time"],
        "dock_name": o.get("dock_name", o["dock_id"]),
        "buffer_minutes": o.get("buffer_minutes"),
    }


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
