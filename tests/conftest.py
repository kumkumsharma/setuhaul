"""Shared pytest fixtures for SetuHaul Phase 1."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

# Force deterministic scenario + in-memory redis before app imports
os.environ["SCENARIO_NOW"] = "2026-08-11T17:25:00+05:30"
os.environ["REDIS_URL"] = "fakeredis://"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["HOLD_TTL_SECONDS"] = "120"
os.environ["GEOAPIFY_MOCK"] = "true"
os.environ["GEOAPIFY_API_KEY"] = ""
os.environ["LOCATION_STALE_MINUTES"] = "30"
# Keep Gemini off in tests unless a case injects a model_factory / key
os.environ["GEMINI_API_KEY"] = ""
os.environ["LANGSMITH_TRACING"] = "false"

from app.config import get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed.load import seed  # noqa: E402
from app.services import redis_client as rc  # noqa: E402


@pytest.fixture()
def db_session():
    get_settings.cache_clear()
    rc.reset_redis_client()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()

    # Point seed helpers at this engine/session
    import app.db as db_mod

    db_mod.engine = engine
    db_mod.SessionLocal = TestingSession

    seed(session)
    # seed() with provided session does not reset; ensure redis clean
    rc.get_redis().flushdb()

    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        get_settings.cache_clear()
        rc.reset_redis_client()


@pytest.fixture()
def client(db_session):
    app = create_app()

    def _override():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
