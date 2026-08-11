"""Minimal observability helpers (LangSmith optional; always log locally)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger("setuhaul")


def trace_event(name: str, payload: dict[str, Any] | None = None) -> None:
    """Record a small structured event. LangSmith is optional — never required for correctness."""
    settings = get_settings()
    data = payload or {}
    logger.info("event=%s payload=%s", name, json.dumps(data, default=str)[:2000])
    if not settings.langsmith_api_key:
        return
    # Soft integration: if langsmith is installed and keyed, emit a run; otherwise no-op.
    try:
        from langsmith import Client  # type: ignore

        client = Client(api_key=settings.langsmith_api_key)
        client.create_run(
            name=name,
            inputs=data,
            run_type="tool",
            project_name=settings.langsmith_project,
        )
    except Exception:  # noqa: BLE001
        logger.debug("langsmith emit skipped for %s", name)
