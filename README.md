# SetuHaul — Driver Exception & Dock Slot Coordination

Deterministic feasibility + Redis-backed allocation, domain APIs, and a driver chat UI. **Capacity truth never comes from an LLM** — Gemini (optional) only understands language and calls tools.

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

## Architecture

```
React chat UI
  → POST /api/chat
  → Gemini via LangChain (if GEMINI_API_KEY set)
       → LangChain tools
       → existing deterministic Python services
       → SQLite + Redis
       → tool results back to Gemini
       → natural-language driver reply
  → else rule/regex orchestrator (fallback)
```

Lifecycle is explicit: **shown → held → confirmed** (or **stale / released / expired / escalated**). Showing options does not reserve capacity. Holds use Redis `SET NX`; confirmation updates SQLite.

### LLM vs operational truth

| Layer | Role |
|-------|------|
| Gemini + LangChain | Intent, NLU, clarification, tool selection, explaining tool results |
| LangChain tools | Thin wrappers around existing domain / feasibility / allocator / location |
| Feasibility + allocator | Sole source of slots, capacity, holds, confirms |
| SQLite | System of record (appointments, exceptions, messages) |
| Redis | Short-TTL holds + idempotency only |

The agent **must not** invent slot IDs, capacity, ETAs, or confirm without a successful `confirm_hold`.

## Gemini setup (optional)

1. Get a Google AI Studio / Gemini API key.
2. In `.env` set:
   - `GEMINI_API_KEY=...`
   - `GEMINI_MODEL=gemini-2.5-flash` (or another currently supported Gemini model)
3. Restart the API. `POST /api/chat` uses the LLM path when the key is present.
4. Leave `GEMINI_API_KEY` empty to keep the fully functional rule-based fallback.

Code: `apps/api/app/services/agent_llm.py` + `agent_tools.py`.

## LangSmith setup (optional)

Tracing is off unless configured. Leave keys empty for local runs.

```bash
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=setuhaul-fde
LANGSMITH_TRACING=false
```

Set `LANGSMITH_TRACING=true` and a valid `LANGSMITH_API_KEY` to emit LangChain/LangSmith traces around agent runs. Absence of LangSmith never blocks chat.

## Manual configuration

| Item | Notes |
|------|--------|
| `.env` | Copy from `.env.example`. Never commit `.env`. |
| `REDIS_URL` | Default `fakeredis://` works without Docker. Use `redis://localhost:6379/0` with `docker compose up -d redis`. |
| `DATABASE_URL` | Default SQLite file under `data/runtime/`. |
| `SCENARIO_NOW` | Fixed to PDF Jaipur snapshot `2026-08-11T17:25:00+05:30` for demos/tests. |
| `GEMINI_API_KEY` | Optional conversational agent; empty → rules fallback. |
| `LANGSMITH_*` | Optional tracing; not required locally. |
| Python | Use 3.12 (`~/.local/bin/python3.12` or `brew install python@3.12`). |

## Tests

```bash
source .venv312/bin/activate   # or .venv if created with Python 3.12
pip install -r apps/api/requirements.txt
export PYTHONPATH=apps/api
pytest -q
```

Agent tests mock Gemini (`model_factory`); they exercise real tools against the deterministic engine. Concurrency and idempotency tests remain on the allocator path.

## Phase 2 (location, scheduling, metrics)

Additive APIs (Phase 1 unchanged as capacity truth):

- `POST /api/location` / `POST /api/location/decline` — one-time browser location + Geoapify/mock route ETA
- `POST /api/scheduling/facilities/{id}/run` — facility-level rule-based schedule proposal
- `GET /api/metrics/summary` — before (baseline) vs after (live CaseMetric) comparison

Set `GEOAPIFY_API_KEY` and `GEOAPIFY_MOCK=false` to call the real Geoapify Routing API; otherwise a deterministic mock is used.

## Out of scope / later

AgentCore, production Redis/Postgres migration, Vercel/CloudWatch, continuous GPS tracking, national network optimisation.
