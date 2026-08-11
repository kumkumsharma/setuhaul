"""Datetime helpers — SQLite drops tzinfo; treat naive values as Asia/Kolkata."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def as_utc_iso(dt: datetime) -> str:
    return ensure_aware(dt).isoformat()  # type: ignore[union-attr]
