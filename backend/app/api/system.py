"""Health, readiness, configuration readiness and saved-report search."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.deps import Paging, Services
from app.core.errors import is_database_unavailable
from app.schemas.api import ConfigIssueOut, ConfigOut, Page, ReportSearchHit
from app.services.readiness import configuration_issues, live_ready

router = APIRouter(tags=["system"])

EXPECTED_MIGRATION = "0001"


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """The process is up (no dependencies checked)."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(services: Services) -> JSONResponse:
    """Database reachability, migration state and configuration problems.

    Never calls paid providers and never reveals secret values.
    """
    checks: dict[str, Any] = {}
    ready = True
    try:
        async with services.db.session() as session:
            await session.execute(text("SELECT 1"))
            version = await session.scalar(
                # db_schema is validated as a plain SQL identifier in Settings.
                text(f'SELECT version_num FROM "{services.settings.db_schema}".alembic_version')  # noqa: S608
            )
        checks["database"] = "ok"
        checks["migrations"] = "ok" if version == EXPECTED_MIGRATION else "pending"
        ready = ready and version == EXPECTED_MIGRATION
    except Exception as exc:
        checks["database"] = "unavailable" if is_database_unavailable(exc) else "error"
        checks["migrations"] = "unknown"
        ready = False
    issues = configuration_issues(services.settings)
    checks["configuration"] = [
        {"key": i.key, "severity": i.severity, "message": i.message} for i in issues
    ]
    if any(i.severity == "error" for i in issues):
        ready = False
    worker = services.worker
    checks["worker"] = (
        "disabled" if worker is None else ("active" if worker.lock_held else "standby")
    )
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )


@router.get("/config")
async def config(services: Services) -> ConfigOut:
    s = services.settings
    return ConfigOut(
        version=__version__,
        execution_mode=s.execution_mode,
        live_ready=live_ready(s),
        llm_provider="demo" if s.demo_mode else s.llm_provider,
        llm_model="demo-fixtures" if s.demo_mode else s.llm_model,
        search_provider="demo" if s.demo_mode else "tavily",
        issues=[
            ConfigIssueOut(key=i.key, severity=i.severity, message=i.message)
            for i in configuration_issues(s)
        ],
        limits={
            "quick": s.limits_for("quick").as_dict(),
            "standard": s.limits_for("standard").as_dict(),
        },
        max_parallel_agent_tasks=s.max_parallel_agent_tasks,
        max_concurrent_runs=s.max_concurrent_runs,
        cost_estimates_enabled=(
            s.llm_input_usd_per_mtok is not None and s.llm_output_usd_per_mtok is not None
        ),
    )


@router.get("/search/reports")
async def search_reports(
    services: Services,
    paging: Paging,
    q: Annotated[str, Query(min_length=2, max_length=200)],
) -> Page[ReportSearchHit]:
    rows, total = await services.reports.search(q, limit=paging.limit, offset=paging.offset)
    items = [
        ReportSearchHit(
            report_id=report.id,
            run_id=run.id,
            project_id=run.project_id,
            title=report.title,
            question=run.question,
            snippet=snippet or report.executive_summary[:240],
            is_partial=report.is_partial,
            execution_mode=run.execution_mode,
            created_at=report.created_at,
        )
        for report, run, _rank, snippet in rows
    ]
    return Page(items=items, total=total, limit=paging.limit, offset=paging.offset)
