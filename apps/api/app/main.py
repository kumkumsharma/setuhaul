from contextlib import asynccontextmanager
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api import allocation, chat, domain
from app.config import get_settings
from app.db import init_db
from app.services import ops_log


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
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
    async def ops_request_log(request: Request, call_next):
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            path = request.url.path
            if path != "/health":
                ops_log.record_event(
                    kind="http",
                    path=path,
                    status_code=status_code,
                    latency_ms=(time.perf_counter() - start) * 1000,
                    outcome="ok" if status_code < 400 else "failure",
                )

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
