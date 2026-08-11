# SetuHaul — Driver Exception & Dock Slot Coordination

Phase 1 MVP: deterministic feasibility + Redis-backed allocation, domain APIs, and a minimal driver chat UI. **Capacity truth never comes from an LLM.**

## Quick start

Requires **Python 3.12+** (3.14 may fail building `pydantic-core`). Scripts prefer `python3.12` when available.

```bash
# 1. Copy env
cp .env.example .env

# 2. Backend venv + seed + API
./scripts/seed.sh
./scripts/dev-api.sh

# 3. Frontend (separate terminal)
cd apps/web && npm install && npm run dev
```

- API: http://127.0.0.1:8000/docs  
- Chat UI: http://127.0.0.1:5173  

## Architecture (Phase 1)

```
Driver Chat UI (React)
    → POST /api/chat  (deterministic orchestrator)
        → domain services (shipment/appointment/exception)
        → feasibility engine (rules, ETA precedence, compatibility)
        → allocator (Redis SET NX holds → confirm/cancel in DB)
```

Lifecycle is explicit: **shown → held → confirmed** (or **stale / released / expired**). Showing options does not reserve capacity.

## Manual configuration

| Item | Notes |
|------|--------|
| `.env` | Copy from `.env.example`. Never commit `.env`. |
| `REDIS_URL` | Default `fakeredis://` works without Docker. Use `redis://localhost:6379/0` with `docker compose up -d redis`. |
| `DATABASE_URL` | Default SQLite file under `data/runtime/`. |
| `SCENARIO_NOW` | Fixed to PDF Jaipur snapshot `2026-08-11T17:25:00+05:30` for demos/tests. |
| Python | Use 3.12 (`~/.local/bin/python3.12` or `brew install python@3.12`). |

## Tests

```bash
source .venv312/bin/activate   # or .venv if created with Python 3.12
export PYTHONPATH=apps/api
pytest -q
```

## Phase 2 (location, scheduling, metrics)

Additive APIs (Phase 1 unchanged as capacity truth):

- `POST /api/location` / `POST /api/location/decline` — one-time browser location + Geoapify/mock route ETA
- `POST /api/scheduling/facilities/{id}/run` — facility-level rule-based schedule proposal
- `GET /api/metrics/summary` — before (baseline) vs after (live CaseMetric) comparison

Set `GEOAPIFY_API_KEY` and `GEOAPIFY_MOCK=false` to call the real Geoapify Routing API; otherwise a deterministic mock is used.

## Out of scope / later

Continuous GPS tracking, national network optimisation, commercial penalty workflows.
