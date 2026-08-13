"""Shared one-time browser location consent (Advanced AddOns PDF §1).

Used by both rule-based chat and Gemini tool path so the interrupt is identical.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models import DriverException
from app.services import domain
from app.services.location import latest_location
from app.services.observability import trace_event


def wants_location_share(text: str) -> bool:
    lower = text.lower().strip()
    return lower in {"yes", "y", "share", "share location", "ok", "sure"} or "share my location" in lower


def declines_location(text: str) -> bool:
    lower = text.lower()
    return any(
        k in lower
        for k in [
            "no location",
            "don't share",
            "do not share",
            "dont share",
            "without location",
            "declared eta",
            "skip location",
            "no thanks",
            "i do not want to share",
            "continue with declared",
            "continue with my declared",
        ]
    ) or lower.strip() in {"no thanks"}


def asks_location_prompt(text: str) -> bool:
    lower = text.lower()
    return "location" in lower and any(k in lower for k in ["share", "send", "use", "gps"])


def looks_like_delay_report(text: str) -> bool:
    lower = text.lower()
    keys = [
        "late",
        "delay",
        "tyre",
        "tire",
        "stuck",
        "breakdown",
        "slot after",
        "slots after",
        "will be late",
        "repair",
    ]
    return any(k in lower for k in keys)


def _state(exc: DriverException) -> dict[str, Any]:
    return json.loads(exc.conversation_state or "{}")


def mark_location_skipped(db: Session, exc: DriverException) -> None:
    domain.update_exception_state(
        db,
        exc,
        {"location_prompted": True, "location_skipped": True, "waiting_for_browser": False},
    )


def request_browser_location(db: Session, exc: DriverException) -> dict[str, Any]:
    """Pause workflow and ask frontend to collect one snapshot."""
    domain.update_exception_state(
        db,
        exc,
        {"location_prompted": True, "waiting_for_browser": True, "location_skipped": False},
    )
    reply = (
        "Please tap Share location so the browser can capture a one-time snapshot. "
        "I will pause until the frontend returns coordinates (or a denial)."
    )
    domain.add_message(db, exc.exception_id, sender_type="agent", text=reply)
    trace_event("request_browser_location", {"exception_id": exc.exception_id})
    return {
        "exception_id": exc.exception_id,
        "shipment_id": exc.shipment_id,
        "reply": reply,
        "status": exc.status,
        "options": [],
        "escalated": False,
        "tools_used": ["REQUEST_BROWSER_LOCATION"],
        "needs_shipment_choice": [],
        "hold": None,
        "appointment": domain.get_appointment_context(db, exc.shipment_id),
        "client_action": "REQUEST_BROWSER_LOCATION",
        "waiting_for_browser": True,
        "eta_comparison": None,
    }


def offer_location_prompt(db: Session, exc: DriverException) -> dict[str, Any]:
    """Ask once whether the driver wants to share location (before options)."""
    domain.update_exception_state(db, exc, {"location_prompted": True})
    reply = (
        "Would you like to share your current location once? "
        "It can improve the ETA buffer for slot suggestions. "
        "Reply 'yes' to share, or 'no' / ask for slots to continue with your declared ETA."
    )
    domain.add_message(db, exc.exception_id, sender_type="agent", text=reply)
    return {
        "exception_id": exc.exception_id,
        "shipment_id": exc.shipment_id,
        "reply": reply,
        "status": exc.status,
        "options": [],
        "escalated": False,
        "tools_used": [],
        "needs_shipment_choice": [],
        "hold": None,
        "appointment": domain.get_appointment_context(db, exc.shipment_id),
        "client_action": None,
        "waiting_for_browser": False,
        "eta_comparison": None,
    }


def evaluate_llm_location_gate(
    db: Session,
    *,
    message: str,
    exception: DriverException | None,
) -> dict[str, Any] | None:
    """Deterministic location consent gate for the Gemini path.

    Returns a chat result to return immediately, or None to continue the LLM loop.
    """
    if not exception:
        return None

    state = _state(exception)
    loc = latest_location(db, exception.exception_id)

    if declines_location(message) or (
        state.get("location_prompted")
        and message.lower().strip() in {"no", "n"}
        and not wants_location_share(message)
    ):
        mark_location_skipped(db, exception)
        return None

    if (
        "continue with" in message.lower()
        or "location shared" in message.lower()
        or "location unavailable" in message.lower()
    ):
        domain.update_exception_state(
            db,
            exception,
            {"location_prompted": True, "location_skipped": True, "waiting_for_browser": False},
        )
        return None

    if wants_location_share(message) and state.get("location_prompted") and not loc:
        return request_browser_location(db, exception)

    if state.get("waiting_for_browser") and not loc and not state.get("location_skipped"):
        # Still waiting — remind and keep client_action
        return request_browser_location(db, exception)

    offer = (
        not state.get("location_prompted")
        and not state.get("location_skipped")
        and not loc
        and (looks_like_delay_report(message) or asks_location_prompt(message))
        and "without location" not in message.lower()
        and "skip location" not in message.lower()
        and "continue with declared" not in message.lower()
    )
    if offer:
        return offer_location_prompt(db, exception)

    return None
