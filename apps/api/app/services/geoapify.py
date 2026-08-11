"""Geoapify routing (or deterministic mock when key missing / GEOAPIFY_MOCK=true)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from app.config import get_settings
from app.services.timeutil import ensure_aware


@dataclass
class RouteResult:
    provider: str
    distance_km: float
    duration_minutes: int
    route_eta: datetime
    ok: bool
    error: str | None = None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def mock_route(
    *,
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    now: datetime,
) -> RouteResult:
    settings = get_settings()
    distance = haversine_km(origin_lat, origin_lon, dest_lat, dest_lon)
    # Truck road factor ~1.25 vs straight-line
    road_km = distance * 1.25
    hours = road_km / max(settings.truck_speed_kmh, 1.0)
    minutes = max(1, int(round(hours * 60)))
    eta = ensure_aware(now) + timedelta(minutes=minutes)  # type: ignore[operator]
    return RouteResult(
        provider="mock",
        distance_km=round(road_km, 2),
        duration_minutes=minutes,
        route_eta=eta,  # type: ignore[arg-type]
        ok=True,
    )


def calculate_route_eta(
    *,
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    now: datetime | None = None,
) -> RouteResult:
    settings = get_settings()
    now = ensure_aware(now or settings.now())
    assert now is not None

    if settings.geoapify_mock or not settings.geoapify_api_key:
        return mock_route(
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            now=now,
        )

    url = "https://api.geoapify.com/v1/routing"
    params = {
        "waypoints": f"{origin_lat},{origin_lon}|{dest_lat},{dest_lon}",
        "mode": "truck",
        "apiKey": settings.geoapify_api_key,
    }
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        features = data.get("features") or []
        if not features:
            return RouteResult(
                provider="geoapify",
                distance_km=0,
                duration_minutes=0,
                route_eta=now,
                ok=False,
                error="no_route",
            )
        props = features[0].get("properties") or {}
        distance_m = float(props.get("distance") or 0)
        duration_s = float(props.get("time") or 0)
        minutes = max(1, int(round(duration_s / 60)))
        return RouteResult(
            provider="geoapify",
            distance_km=round(distance_m / 1000.0, 2),
            duration_minutes=minutes,
            route_eta=now + timedelta(minutes=minutes),
            ok=True,
        )
    except Exception as exc:  # noqa: BLE001 — fall back to declared-ETA workflow
        return RouteResult(
            provider="geoapify",
            distance_km=0,
            duration_minutes=0,
            route_eta=now,
            ok=False,
            error=str(exc),
        )
