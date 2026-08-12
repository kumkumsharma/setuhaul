"""Gemini + LangChain conversational layer for SetuHaul.

Operational truth remains in feasibility/allocator tools. The LLM only:
understands language, chooses tools, and explains tool results.
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ChatMessage, DriverException
from app.services import allocator, domain
from app.services.agent_tools import (
    AgentSession,
    build_tools,
    reset_session,
    set_session,
)
from app.services.observability import configure_langsmith_env, trace_event


SYSTEM_PROMPT = """You are a SetuHaul driver operations assistant.

Operational availability comes ONLY from tools.
Never invent availability, slot IDs, ETAs, facility rules, or capacity.

Rules:
- Ask for clarification when shipment identity is ambiguous (multiple active shipments).
- Use the existing tools to retrieve operational truth before answering about slots or bookings.
- Displaying a slot does NOT reserve it (lifecycle: shown).
- A slot becomes reserved ONLY after successful create_hold (lifecycle: held).
- A booking becomes confirmed ONLY after successful confirm_hold (lifecycle: confirmed).
- If list_feasible_slots returns no options, clearly say no feasible slot was found and escalate.
  Never manufacture an alternative slot or appointment.
- When holding, pass the exact slot_id returned by list_feasible_slots.
- Prefer short, practical driver-facing language.
- You may extract delay minutes and preferred times from free text, then store them via tools.
"""


def gemini_configured() -> bool:
    settings = get_settings()
    return bool(settings.gemini_api_key and settings.gemini_api_key.strip())


def _load_history(db: Session, exception_id: str | None) -> list:
    if not exception_id:
        return []
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.exception_id == exception_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    history = []
    for row in rows[-20:]:
        if row.sender_type == "driver":
            history.append(HumanMessage(content=row.message_text))
        elif row.sender_type in {"agent", "system"}:
            history.append(AIMessage(content=row.message_text))
    return history


def _build_model():
    settings = get_settings()
    return ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        google_api_key=settings.gemini_api_key,
        temperature=0.2,
    )


def run_tool_loop(
    *,
    model,
    tools,
    messages: list,
    max_iters: int = 8,
) -> tuple[str, list[str]]:
    """Simple tool-calling loop. Returns final assistant text + tool names used this turn."""

    tool_map = {t.name: t for t in tools}
    turn_tools: list[str] = []
    model_with_tools = model.bind_tools(tools)

    for _ in range(max_iters):
        ai: AIMessage = model_with_tools.invoke(messages)  # type: ignore[assignment]
        messages.append(ai)
        tool_calls = getattr(ai, "tool_calls", None) or []
        if not tool_calls:
            content = ai.content
            if isinstance(content, list):
                # Gemini sometimes returns content blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)
            return (str(content or "").strip(), turn_tools)

        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", name)
            if name not in tool_map:
                result = f'{{"error":"unknown_tool","name":"{name}"}}'
            else:
                try:
                    result = tool_map[name].invoke(args)
                except Exception as exc:  # noqa: BLE001
                    result = f'{{"error":"tool_failed","detail":"{exc}"}}'
            turn_tools.append(str(name))
            messages.append(ToolMessage(content=str(result), tool_call_id=str(tc_id)))

    return (
        "I reached the tool-call limit. Please ask again or contact operations.",
        turn_tools,
    )


def handle_chat_llm(
    db: Session,
    *,
    driver_id: str,
    message: str,
    exception_id: str | None = None,
    shipment_id: str | None = None,
    idempotency_key: str | None = None,
    model_factory: Callable | None = None,
) -> dict[str, Any]:
    """LLM-backed chat entrypoint. Raises on hard failure so caller can fall back."""

    if idempotency_key:
        cached = allocator.get_idempotent_result(f"chat:{idempotency_key}")
        if cached:
            return cached

    if not gemini_configured() and model_factory is None:
        raise RuntimeError("GEMINI_API_KEY not configured")

    configure_langsmith_env()

    session = AgentSession(
        db=db,
        driver_id=driver_id,
        exception_id=exception_id,
        shipment_id=shipment_id,
    )
    token = set_session(session)
    try:
        tools = build_tools()
        model = model_factory() if model_factory else _build_model()

        history = _load_history(db, exception_id)
        # Always refresh shipment disambiguation context via tools guidance in user envelope
        envelope = (
            f"Authenticated driver_id={driver_id}. "
            f"Current exception_id={exception_id or 'none'}. "
            f"Preferred shipment_id={shipment_id or 'none'}. "
            f"Driver message: {message}"
        )
        messages: list = [SystemMessage(content=SYSTEM_PROMPT), *history, HumanMessage(content=envelope)]

        trace_event(
            "agent_llm_start",
            {"driver_id": driver_id, "exception_id": exception_id, "shipment_id": shipment_id},
        )
        reply, turn_tools = run_tool_loop(model=model, tools=tools, messages=messages)
        if not reply:
            reply = "I processed your request using operational tools."

        # Persist agent reply; store driver text only if tools did not already persist it
        if session.exception_id:
            last = (
                db.query(ChatMessage)
                .filter(ChatMessage.exception_id == session.exception_id)
                .order_by(ChatMessage.created_at.desc())
                .first()
            )
            if not (
                last
                and last.sender_type == "driver"
                and last.message_text == message
            ):
                domain.add_message(db, session.exception_id, sender_type="driver", text=message)
            domain.add_message(db, session.exception_id, sender_type="agent", text=reply)

        status = "open"
        if session.exception_id:
            exc = db.get(DriverException, session.exception_id)
            if exc:
                status = exc.status
        if session.escalated:
            status = "escalated"
        elif session.hold and session.hold.get("status") == "confirmed":
            status = "confirmed"
        elif session.hold and session.hold.get("status") == "held":
            status = "held"
        elif session.options:
            status = "awaiting_choice"

        tools_used = list(dict.fromkeys(session.tools_used + turn_tools))
        result = {
            "exception_id": session.exception_id or exception_id or "",
            "shipment_id": session.shipment_id or shipment_id or "",
            "reply": reply,
            "status": status,
            "options": session.options,
            "hold": session.hold,
            "appointment": session.appointment,
            "needs_shipment_choice": session.needs_shipment_choice,
            "escalated": session.escalated or status == "escalated",
            "tools_used": tools_used,
            "client_action": session.client_action,
            "eta_comparison": session.eta_comparison,
            "waiting_for_browser": False,
        }
        if idempotency_key:
            allocator.store_idempotent_result(f"chat:{idempotency_key}", result)
        trace_event(
            "agent_llm_complete",
            {
                "exception_id": result["exception_id"],
                "tools_used": tools_used,
                "escalated": result["escalated"],
                "options": len(result["options"]),
            },
        )
        return result
    finally:
        reset_session(token)


def handle_chat_with_fallback(
    db: Session,
    *,
    driver_id: str,
    message: str,
    exception_id: str | None = None,
    shipment_id: str | None = None,
    idempotency_key: str | None = None,
    model_factory: Callable | None = None,
) -> dict[str, Any]:
    """Use Gemini when configured; otherwise / on failure use rule-based handle_chat."""

    from app.services.chat import handle_chat

    if gemini_configured() or model_factory is not None:
        try:
            return handle_chat_llm(
                db,
                driver_id=driver_id,
                message=message,
                exception_id=exception_id,
                shipment_id=shipment_id,
                idempotency_key=idempotency_key,
                model_factory=model_factory,
            )
        except Exception as exc:  # noqa: BLE001
            trace_event("agent_llm_fallback", {"error": str(exc)})
            # fall through

    return handle_chat(
        db,
        driver_id=driver_id,
        message=message,
        exception_id=exception_id,
        shipment_id=shipment_id,
        idempotency_key=idempotency_key,
    )
