"""Minimal observability helpers (LangSmith optional; always log locally)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.config import get_settings

logger = logging.getLogger("setuhaul")


def configure_langsmith_env() -> None:
    settings = get_settings()
    if settings.langsmith_api_key and settings.langsmith_tracing:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    else:
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
        os.environ.setdefault("LANGSMITH_TRACING", "false")


def trace_event(name: str, payload: dict[str, Any] | None = None) -> None:
    """Record a small structured event. LangSmith is optional — never required for correctness."""
    settings = get_settings()
    data = payload or {}
    logger.info("event=%s payload=%s", name, json.dumps(data, default=str)[:2000])
    if not settings.langsmith_api_key:
        return
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
