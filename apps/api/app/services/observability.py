"""Minimal observability helpers (LangSmith optional; always log locally)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.config import get_settings

logger = logging.getLogger("setuhaul")


def configure_langsmith_env() -> None:
    """Enable LangChain/LangSmith auto-tracing when key + LANGSMITH_TRACING=true.

    Sets the env vars LangChain reads before Gemini/tool invokes so LLM and
    tool runs appear as a real trace tree (not ad-hoc create_run stubs).
    """
    settings = get_settings()
    if settings.langsmith_api_key and settings.langsmith_api_key.strip() and settings.langsmith_tracing:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
        project = settings.langsmith_project or "setuhaul-fde"
        os.environ["LANGCHAIN_PROJECT"] = project
        os.environ["LANGSMITH_PROJECT"] = project
        logger.info("langsmith_tracing_enabled project=%s", project)
    else:
        # Explicitly disable so a prior enable in-process cannot leak into tests/fallback.
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"
        logger.debug(
            "langsmith_tracing_disabled key_present=%s flag=%s",
            bool(settings.langsmith_api_key and settings.langsmith_api_key.strip()),
            settings.langsmith_tracing,
        )


def langsmith_tracing_enabled() -> bool:
    settings = get_settings()
    return bool(
        settings.langsmith_api_key
        and settings.langsmith_api_key.strip()
        and settings.langsmith_tracing
    )


def trace_event(name: str, payload: dict[str, Any] | None = None) -> None:
    """Local structured log helper. Real agent traces come from LangChain auto-tracing."""
    data = payload or {}
    logger.info("event=%s payload=%s", name, json.dumps(data, default=str)[:2000])
