from contextlib import asynccontextmanager
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import allocation, chat, domain
from app.config import get_settings
from app.db import init_db
from app.services import ops_log
from app.services.observability import (
    configure_logging,
    log_request_complete,
    new_request_id,
    reset_request_id,
    set_request_id,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    app = FastAPI(
        title="SetuHaul Driver Exception API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        """Correlation + one structured request-completion log; ops_log stays demo-only."""
        incoming = request.headers.get("x-request-id") or request.headers.get("X-Request-Id")
        request_id = (incoming or "").strip() or new_request_id()
        token = set_request_id(request_id)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            path = request.url.path
            latency_ms = (time.perf_counter() - start) * 1000
            outcome = "ok" if status_code < 400 else "failure"
            # Demo UI buffer (not CloudWatch). Skip health noise.
            if path != "/health":
                ops_log.record_event(
                    kind="http",
                    path=path,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    outcome=outcome,
                )
            # Production stdout JSON → ECS CloudWatch. Omit successful /health.
            if path != "/health" or status_code >= 400:
                log_request_complete(
                    request_id=request_id,
                    method=request.method,
                    path=path,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    outcome=outcome,
                )
            reset_request_id(token)

    app.include_router(domain.router)
    app.include_router(allocation.router)
    app.include_router(chat.router)

    from app.api import location, metrics, scheduling

    app.include_router(location.router)
    app.include_router(scheduling.router)
    app.include_router(metrics.router)

    @app.get("/health")
    def health():
        return {"status": "ok", "scenario_now": settings.scenario_now, "phase": 2}

    return app


app = create_app()
