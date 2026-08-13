from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_parent(url: str) -> None:
    if url.startswith("sqlite:///./"):
        path = Path(url.removeprefix("sqlite:///./"))
        path.parent.mkdir(parents=True, exist_ok=True)


settings = get_settings()
_ensure_sqlite_parent(settings.database_url)

connect_args: dict = {}
_engine_kwargs: dict = {"future": True}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    # Bound waits so a stalled RDS socket cannot hang request workers forever.
    connect_args = {"connect_timeout": 10}
    _engine_kwargs.update(
        pool_pre_ping=True,
        pool_reset_on_return="rollback",
        pool_recycle=300,
    )
_engine_kwargs["connect_args"] = connect_args
engine = create_engine(settings.database_url, **_engine_kwargs)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Local SQLite convenience only. Postgres/RDS schema is owned by Alembic."""
    from app import models  # noqa: F401

    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
