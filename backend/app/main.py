"""FastAPI application factory.

One backend process: the API, the SSE streams and the single managed research
worker share an event loop. In production the built frontend is served from
the same origin; in development Vite proxies ``/api`` to this server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import projects, runs, system
from app.api.deps import AppServices
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.security import RateLimiter, SecurityMiddleware
from app.db.engine import Database
from app.services.events import EventBroker
from app.services.readiness import configuration_issues
from app.services.worker import ResearchWorker

log = structlog.get_logger(__name__)

API_PREFIX = "/api/v1"


def build_services(settings: Settings, *, start_worker: bool) -> AppServices:
    """Create the process-wide services (also used directly by tests)."""
    db = Database(settings)
    broker = EventBroker()
    return AppServices(
        settings=settings,
        db=db,
        broker=broker,
        worker=ResearchWorker(settings, db, broker) if start_worker else None,
        run_rate_limiter=RateLimiter(settings.run_create_rate_limit_per_minute),
    )


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = build_services(settings, start_worker=start_worker)
        app.state.services = services
        db, worker = services.db, services.worker
        for issue in configuration_issues(settings):
            log.warning("config.issue", key=issue.key, severity=issue.severity)
        log.info(
            "app.started",
            version=__version__,
            execution_mode=settings.execution_mode,
            host=settings.host,
            port=settings.port,
        )
        if worker is not None:
            await worker.start()
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
            await db.dispose()
            log.info("app.stopped")

    app = FastAPI(
        title="Research Hive",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
        # FastAPI's built-in OpenTelemetry hooks are not used; keep them off so
        # nothing records request data outside our redacting logger.
        telemetry={
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        },
    )
    register_exception_handlers(app)

    # Middleware: the last one added is the outermost. CORS uses an explicit
    # origin list (never "*") and no credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origin_list,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Research-Hive", "Idempotency-Key", "Last-Event-ID"],
        expose_headers=["X-Request-ID"],
        allow_credentials=False,
        max_age=600,
    )
    app.add_middleware(
        SecurityMiddleware,
        allowed_hosts=settings.allowed_host_list,
        allowed_origins=settings.allowed_origin_list,
    )

    api = APIRouter(prefix=API_PREFIX)
    api.include_router(system.router)
    api.include_router(projects.router)
    api.include_router(runs.router)
    app.include_router(api)

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def api_not_found(path: str) -> None:
        raise StarletteHTTPException(status_code=404)

    _mount_frontend(app, settings.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built single-page app, falling back to index.html."""
    index = dist / "index.html"
    if not index.is_file():
        log.info("frontend.not_built", hint="run `npm run build` in frontend/ for same-origin UI")
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        # Only serve real files inside dist/; everything else is a client route.
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
