"""Tests for Gemini/LangChain agent tools + mocked LLM orchestration.

No live Gemini calls — tools hit the real deterministic services.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage

from app.services.agent_llm import handle_chat_llm, handle_chat_with_fallback
from app.services.agent_tools import (
    AgentSession,
    TOOL_NAMES,
    build_tools,
    reset_session,
    set_session,
    _tool_list_feasible_slots,
    _tool_create_hold,
    _tool_confirm_hold,
    _tool_create_exception,
    _tool_list_active_shipments,
)


class ScriptedModel:
    """Minimal chat model stand-in: returns scripted AIMessages, supports bind_tools."""

    def __init__(self, script: list[AIMessage]):
        self._script = list(script)
        self._i = 0

    def bind_tools(self, tools):  # noqa: ARG002
        return self

    def invoke(self, messages):  # noqa: ARG002
        if self._i >= len(self._script):
            return AIMessage(content="Done.")
        msg = self._script[self._i]
        self._i += 1
        return msg


def _ai_tools(calls: list[dict[str, Any]], content: str = "") -> AIMessage:
    return AIMessage(content=content, tool_calls=calls)


def test_tool_catalog_names():
    assert "list_feasible_slots" in TOOL_NAMES
    assert "create_hold" in TOOL_NAMES
    assert "confirm_hold" in TOOL_NAMES
    tools = build_tools()
    assert {t.name for t in tools} == set(TOOL_NAMES)


def test_natural_language_delay_via_scripted_agent(db_session):
    """A: tyre delay + after 7 PM → tools create exception and list real feasible slots."""

    script = [
        _ai_tools(
            [
                {
                    "name": "create_exception",
                    "id": "1",
                    "args": {
                        "shipment_id": "SHP-1042",
                        "exception_type": "tyre_issue",
                        "reported_delay_minutes": 90,
                        "message": "Tyre damaged near Neemrana. Around 90 minutes late.",
                    },
                },
                {
                    "name": "list_feasible_slots",
                    "id": "2",
                    "args": {"shipment_id": "SHP-1042", "after": "2026-08-11T19:00:00+05:30"},
                },
            ]
        ),
        AIMessage(
            content=(
                "I found feasible later slots from the allocation engine. "
                "Showing an option does not reserve it yet."
            )
        ),
    ]

    result = handle_chat_llm(
        db_session,
        driver_id="DRV-027",
        shipment_id="SHP-1042",
        message=(
            "Tyre damaged near Neemrana. Around 90 minutes late. "
            "Skip location. Can I get a slot after 7 PM?"
        ),
        model_factory=lambda: ScriptedModel(script),
    )
    assert result["exception_id"]
    assert "list_feasible_slots" in result["tools_used"]
    assert result["escalated"] is False
    # Options must come from tools / engine — empty is only OK if engine had none
    for opt in result["options"]:
        assert opt["slot_id"].startswith("SLOT-")
        assert "lifecycle" in opt


def test_ambiguous_shipment_asks_which(db_session):
    """B: multi-shipment driver → needs_shipment_choice populated."""

    script = [
        _ai_tools(
            [{"name": "list_active_shipments", "id": "1", "args": {}}],
        ),
        AIMessage(
            content=(
                "You have more than one active shipment. "
                "Please tell me which shipment ID to reschedule."
            )
        ),
    ]
    result = handle_chat_llm(
        db_session,
        driver_id="DRV-MULTI",
        message="I am going to be late. Can you move my slot?",
        model_factory=lambda: ScriptedModel(script),
    )
    assert len(result["needs_shipment_choice"]) == 2
    assert "which shipment" in result["reply"].lower() or "more than one" in result["reply"].lower()


def test_no_feasible_slot_escalates(db_session):
    """C: no feasible slots → escalate, never invent."""

    script = [
        _ai_tools(
            [
                {
                    "name": "create_exception",
                    "id": "1",
                    "args": {
                        "shipment_id": "SHP-NOP",
                        "message": "Need a slot after 6 PM",
                    },
                },
                {
                    "name": "list_feasible_slots",
                    "id": "2",
                    "args": {"shipment_id": "SHP-NOP", "after": "2026-08-11T18:00:00+05:30"},
                },
            ]
        ),
        AIMessage(
            content=(
                "No feasible same-day slot was returned by the allocation engine. "
                "I am escalating to operations and will not invent a slot."
            )
        ),
    ]
    result = handle_chat_llm(
        db_session,
        driver_id="DRV-NOP",
        shipment_id="SHP-NOP",
        message="I will be late. Skip location. What slots after 6 PM?",
        model_factory=lambda: ScriptedModel(script),
    )
    assert result["escalated"] is True
    assert result["options"] == []
    assert "invent" in result["reply"].lower() or "no feasible" in result["reply"].lower()


def test_feasible_options_not_invented(db_session):
    """D: agent explains only tool-returned options."""

    session = AgentSession(db=db_session, driver_id="DRV-027", shipment_id="SHP-1042")
    token = set_session(session)
    try:
        _tool_create_exception(
            shipment_id="SHP-1042",
            message="late",
            reported_delay_minutes=90,
        )
        raw = _tool_list_feasible_slots(
            shipment_id="SHP-1042", after="2026-08-11T19:00:00+05:30"
        )
        data = json.loads(raw)
        assert data["count"] == len(data["options"])
        for o in data["options"]:
            assert o["slot_id"].startswith("SLOT-")
        # Invented ID must be rejected by create_hold
        bad = json.loads(_tool_create_hold(slot_id="SLOT-INVENTED-FAKE"))
        assert bad.get("error") == "unknown_slot_id"
    finally:
        reset_session(token)


def test_hold_and_confirm_go_through_allocator(db_session):
    """E/F: hold + confirm use existing allocator lifecycle."""

    session = AgentSession(db=db_session, driver_id="DRV-027", shipment_id="SHP-1042")
    token = set_session(session)
    try:
        _tool_create_exception(shipment_id="SHP-1042", message="need slot")
        data = json.loads(
            _tool_list_feasible_slots(
                shipment_id="SHP-1042", after="2026-08-11T19:00:00+05:30"
            )
        )
        assert data["options"], "seed should provide later feasible slots for Ravi"
        slot_id = data["options"][0]["slot_id"]
        hold_raw = json.loads(_tool_create_hold(slot_id=slot_id))
        assert hold_raw["lifecycle"] == "held"
        hold_id = hold_raw["hold"]["hold_id"]
        conf = json.loads(_tool_confirm_hold(hold_id=hold_id))
        assert conf["lifecycle"] == "confirmed"
        assert conf["appointment"]["status"] == "confirmed"
        assert conf["appointment"]["slot_id"] == slot_id
    finally:
        reset_session(token)


def test_llm_location_consent_offers_then_requests_browser(db_session):
    """Gemini path: delay offers location; 'yes' returns REQUEST_BROWSER_LOCATION."""
    first = handle_chat_llm(
        db_session,
        driver_id="DRV-027",
        shipment_id="SHP-1042",
        message="Tyre damaged near Neemrana. Repair may take 45 minutes.",
        model_factory=lambda: ScriptedModel([AIMessage(content="should not run")]),
    )
    assert "share your current location" in first["reply"].lower()
    assert first["client_action"] is None
    assert first["options"] == []
    assert first["exception_id"]

    second = handle_chat_llm(
        db_session,
        driver_id="DRV-027",
        shipment_id="SHP-1042",
        exception_id=first["exception_id"],
        message="yes",
        model_factory=lambda: ScriptedModel([AIMessage(content="should not run")]),
    )
    assert second["client_action"] == "REQUEST_BROWSER_LOCATION"
    assert second["waiting_for_browser"] is True
    assert "Share location" in second["reply"] or "share location" in second["reply"].lower()


def test_request_browser_location_tool(db_session):
    from app.services.agent_tools import _tool_request_browser_location

    session = AgentSession(db=db_session, driver_id="DRV-027", shipment_id="SHP-1042")
    token = set_session(session)
    try:
        _tool_create_exception(shipment_id="SHP-1042", message="late")
        raw = json.loads(_tool_request_browser_location())
        assert raw["client_action"] == "REQUEST_BROWSER_LOCATION"
        assert session.client_action == "REQUEST_BROWSER_LOCATION"
        assert session.waiting_for_browser is True
    finally:
        reset_session(token)


def test_fallback_without_llm_key(db_session, monkeypatch):
    """Without OPENROUTER_API_KEY / GEMINI_API_KEY, rules path remains fully functional."""

    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()

    result = handle_chat_with_fallback(
        db_session,
        driver_id="DRV-027",
        shipment_id="SHP-1042",
        message="Tyre damaged near Neemrana. Can I get a slot after 7 PM?",
    )
    assert result["exception_id"]
    assert result.get("reply")
    get_settings.cache_clear()


def test_list_active_shipments_tool_for_multi(db_session):
    session = AgentSession(db=db_session, driver_id="DRV-MULTI")
    token = set_session(session)
    try:
        raw = json.loads(_tool_list_active_shipments())
        assert raw["count"] == 2
        assert len(session.needs_shipment_choice) == 2
    finally:
        reset_session(token)
