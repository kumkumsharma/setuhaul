"""Shared pytest fixtures for SetuHaul.

Default: isolated in-memory SQLite + fakeredis (fast, safe).

External: TEST_USE_EXTERNAL=1 uses DATABASE_URL / REDIS_URL from the environment
(e.g. RDS + Upstash). Wipes row data per test via TRUNCATE — never drop_all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

USE_EXTERNAL = os.environ.get("TEST_USE_EXTERNAL") == "1"

if USE_EXTERNAL:
    # Load local secrets without printing them; do not override DATABASE_URL / REDIS_URL.
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    os.environ["SCENARIO_NOW"] = "2026-08-11T17:25:00+05:30"
    os.environ["HOLD_TTL_SECONDS"] = "120"
    os.environ["GEOAPIFY_MOCK"] = "true"
    os.environ["GEOAPIFY_API_KEY"] = ""
    os.environ["LOCATION_STALE_MINUTES"] = "30"
    # Keep chat tests on deterministic rules path
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["LANGSMITH_TRACING"] = "false"
else:
    # Force deterministic scenario + in-memory redis before app imports
    os.environ["SCENARIO_NOW"] = "2026-08-11T17:25:00+05:30"
    os.environ["REDIS_URL"] = "fakeredis://"
    os.environ["DATABASE_URL"] = "sqlite://"
    os.environ["HOLD_TTL_SECONDS"] = "120"
    os.environ["GEOAPIFY_MOCK"] = "true"
    os.environ["GEOAPIFY_API_KEY"] = ""
    os.environ["LOCATION_STALE_MINUTES"] = "30"
    os.environ["GEMINI_API_KEY"] = ""
    os.environ["LANGSMITH_TRACING"] = "false"

from app.config import get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed.load import seed  # noqa: E402
from app.services import redis_client as rc  # noqa: E402


def _truncate_all(engine) -> None:
    """Remove all rows; keep schema. Postgres only (external mode).

    Uses AUTOCOMMIT so TRUNCATE cannot leave an open transaction holding locks
    if the test process is interrupted.
    """
    from app import models  # noqa: F401
    from sqlalchemy import create_engine as _create_engine

    table_names = [t.name for t in Base.metadata.sorted_tables]
    if not table_names:
        raise RuntimeError("No SQLAlchemy tables registered; cannot truncate")
    joined = ", ".join(f'"{name}"' for name in table_names)
    # Separate autocommit engine avoids lock contention with the test session.
    url = str(engine.url)
    trunc_engine = _create_engine(
        url,
        poolclass=NullPool,
        pool_pre_ping=True,
        isolation_level="AUTOCOMMIT",
        future=True,
    )
    try:
        with trunc_engine.connect() as conn:
            conn.execute(text("SET lock_timeout = '15s'"))
            conn.execute(text(f"TRUNCATE {joined} CASCADE"))
    finally:
        trunc_engine.dispose()


@pytest.fixture()
def db_session():
    get_settings.cache_clear()
    rc.reset_redis_client()

    if USE_EXTERNAL:
        settings = get_settings()
        assert not settings.database_url.startswith(
            "sqlite"
        ), "TEST_USE_EXTERNAL requires Postgres DATABASE_URL"
        assert not settings.redis_url.startswith(
            "fakeredis"
        ), "TEST_USE_EXTERNAL requires real REDIS_URL"

        # NullPool avoids exhausting RDS connection limits across 35 truncate/seed cycles.
        engine = create_engine(
            settings.database_url,
            poolclass=NullPool,
            pool_pre_ping=True,
            future=True,
        )
        TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        import app.db as db_mod

        db_mod.engine = engine
        db_mod.SessionLocal = TestingSession

        session = TestingSession()
        _truncate_all(engine)
        seed(session)
        try:
            rc.get_redis().flushdb()
        except Exception:
            pass
        try:
            yield session
        finally:
            # Release any open tx before truncate so we never block AccessExclusiveLock.
            try:
                session.rollback()
            except Exception:
                pass
            session.close()
            try:
                _truncate_all(engine)
            except Exception:
                pass
            engine.dispose()
            get_settings.cache_clear()
            rc.reset_redis_client()
        return

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()

    import app.db as db_mod

    db_mod.engine = engine
    db_mod.SessionLocal = TestingSession

    seed(session)
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
