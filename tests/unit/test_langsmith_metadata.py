"""Phase C: LangSmith root metadata/tags (no live LangSmith network calls)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from app.config import get_settings
from app.services.agent_llm import run_tool_loop
from app.services.observability import (
    build_langsmith_run_metadata,
    build_langsmith_run_tags,
    configure_langsmith_env,
    langsmith_tracing_enabled,
    reset_request_id,
    resolve_llm_provider_model,
    set_request_id,
)


def test_build_langsmith_metadata_includes_request_and_case_ids(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    get_settings.cache_clear()

    token = set_request_id("cw-request-xyz")
    try:
        provider, model = resolve_llm_provider_model()
        meta = build_langsmith_run_metadata(
            driver_id="DRV-027",
            exception_id="EXC-1",
            shipment_id="SHP-1042",
            provider=provider,
            model=model,
        )
    finally:
        reset_request_id(token)
        get_settings.cache_clear()

    assert meta["request_id"] == "cw-request-xyz"
    assert meta["driver_id"] == "DRV-027"
    assert meta["exception_id"] == "EXC-1"
    assert meta["shipment_id"] == "SHP-1042"
    assert meta["provider"] == "openrouter"
    assert meta["model"] == "openai/gpt-4o-mini"
    assert meta["environment"] == "staging"
    assert meta["git_sha"] == "abc1234"
    assert "langsmith_api_key" not in meta
    assert "api_key" not in meta
    assert "prompt" not in meta


def test_build_langsmith_metadata_omits_empty_git_sha(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("GIT_SHA", "")
    get_settings.cache_clear()
    try:
        meta = build_langsmith_run_metadata(driver_id="DRV-027")
    finally:
        get_settings.cache_clear()
    assert meta["environment"] == "local"
    assert "git_sha" not in meta
    assert "request_id" not in meta  # no request context


def test_build_langsmith_tags(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    try:
        tags = build_langsmith_run_tags(provider="openrouter")
    finally:
        get_settings.cache_clear()
    assert tags[0] == "setuhaul"
    assert "agent" in tags
    assert "prod" in tags
    assert "openrouter" in tags
    assert "none" not in build_langsmith_run_tags(provider="none")


def test_resolve_provider_prefers_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    get_settings.cache_clear()
    try:
        assert resolve_llm_provider_model() == ("openrouter", "openai/gpt-4o-mini")
    finally:
        get_settings.cache_clear()


def test_resolve_provider_gemini_when_no_openrouter(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    get_settings.cache_clear()
    try:
        assert resolve_llm_provider_model() == ("gemini", "gemini-2.5-flash")
    finally:
        get_settings.cache_clear()


def test_tracing_disabled_skips_traceable(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    configure_langsmith_env(force=True)
    assert langsmith_tracing_enabled() is False

    model = MagicMock()
    model.bind_tools.return_value = model
    model.invoke.return_value = AIMessage(content="hello")

    with patch("langsmith.traceable") as traceable:
        reply, tools = run_tool_loop(
            model=model,
            tools=[],
            messages=[],
            driver_id="DRV-027",
        )
    assert reply == "hello"
    assert tools == []
    traceable.assert_not_called()
    get_settings.cache_clear()


def test_traceable_receives_metadata_tags_and_request_id(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test_key_not_real")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("GIT_SHA", "deadbeef")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    get_settings.cache_clear()
    configure_langsmith_env(force=True)
    assert langsmith_tracing_enabled() is True

    model = MagicMock()
    model.bind_tools.return_value = model
    model.invoke.return_value = AIMessage(content="ok")

    captured: dict = {}

    def fake_traceable(**kwargs):
        captured.update(kwargs)

        def deco(fn):
            return fn

        return deco

    token = set_request_id("match-cloudwatch-rid")
    try:
        with patch("langsmith.traceable", side_effect=fake_traceable):
            reply, _ = run_tool_loop(
                model=model,
                tools=[],
                messages=[],
                driver_id="DRV-027",
                exception_id="EXC-9",
                shipment_id="SHP-1042",
            )
    finally:
        reset_request_id(token)
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        monkeypatch.setenv("LANGSMITH_API_KEY", "")
        get_settings.cache_clear()
        configure_langsmith_env(force=True)

    assert reply == "ok"
    assert captured["name"] == "setuhaul_driver_agent"
    assert captured["run_type"] == "chain"
    meta = captured["metadata"]
    assert meta["request_id"] == "match-cloudwatch-rid"
    assert meta["driver_id"] == "DRV-027"
    assert meta["exception_id"] == "EXC-9"
    assert meta["shipment_id"] == "SHP-1042"
    assert meta["provider"] == "openrouter"
    assert meta["model"] == "openai/gpt-4o-mini"
    assert meta["environment"] == "dev"
    assert meta["git_sha"] == "deadbeef"
    assert "setuhaul" in captured["tags"]
    assert "agent" in captured["tags"]
    assert "dev" in captured["tags"]
    assert "openrouter" in captured["tags"]


def test_tracing_fail_open_when_traceable_raises(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test_key_not_real")
    get_settings.cache_clear()
    configure_langsmith_env(force=True)

    model = MagicMock()
    model.bind_tools.return_value = model
    model.invoke.return_value = AIMessage(content="still works")

    with patch("langsmith.traceable", side_effect=RuntimeError("langsmith down")):
        reply, tools = run_tool_loop(model=model, tools=[], messages=[])

    assert reply == "still works"
    assert tools == []

    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    configure_langsmith_env(force=True)


def test_configure_langsmith_fail_open():
    with patch(
        "app.services.observability.get_settings",
        side_effect=RuntimeError("boom"),
    ):
        configure_langsmith_env(force=True)  # must not raise
    get_settings.cache_clear()
    configure_langsmith_env(force=True)
