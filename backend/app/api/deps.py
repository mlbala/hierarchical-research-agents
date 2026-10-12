"""Shared API dependencies and helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request

from app.core.config import Settings
from app.core.errors import RequestRejectedError
from app.core.security import RateLimiter
from app.db.engine import Database
from app.services.events import EventBroker, EventRecorder
from app.services.reports import ReportService
from app.services.runs import ProjectService, RunService
from app.services.worker import ResearchWorker

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")


@dataclass
class AppServices:
    """Process-wide singletons created in the application lifespan."""

    settings: Settings
    db: Database
    broker: EventBroker
    worker: ResearchWorker | None
    run_rate_limiter: RateLimiter

    @property
    def events(self) -> EventRecorder:
        return EventRecorder(self.db, self.broker)

    @property
    def projects(self) -> ProjectService:
        return ProjectService(self.db)

    @property
    def runs(self) -> RunService:
        worker = self.worker
        return RunService(
            self.db,
            self.settings,
            self.events,
            on_queued=worker.notify_new_run if worker else None,
            on_cancel=worker.request_cancel if worker else None,
        )

    @property
    def reports(self) -> ReportService:
        return ReportService(self.db)


def get_services(request: Request) -> AppServices:
    services: AppServices = request.app.state.services
    return services


Services = Annotated[AppServices, Depends(get_services)]


@dataclass(frozen=True)
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


Paging = Annotated[Pagination, Depends(pagination)]


def idempotency_key(
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """Required on run creation so double-submits never create duplicate jobs."""
    if key is None or not _IDEMPOTENCY_KEY.match(key):
        raise RequestRejectedError(
            "An Idempotency-Key header (8-128 letters, digits, '-', '_', ':' or '.') is required.",
            code="IDEMPOTENCY_KEY_REQUIRED",
        )
    return key


IdempotencyKey = Annotated[str, Depends(idempotency_key)]
