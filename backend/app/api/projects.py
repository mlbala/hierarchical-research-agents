"""Project endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

from app.api.deps import IdempotencyKey, Paging, Services
from app.api.runs import enforce_run_rate_limit, run_summary
from app.schemas.api import Page, ProjectCreate, ProjectOut, RunCreate, RunSummary

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, services: Services) -> ProjectOut:
    project = await services.projects.create(body.name, body.description)
    return ProjectOut.model_validate(project)


@router.get("")
async def list_projects(services: Services, paging: Paging) -> Page[ProjectOut]:
    rows, total = await services.projects.list_projects(limit=paging.limit, offset=paging.offset)
    items = [
        ProjectOut.model_validate(row["project"]).model_copy(
            update={"run_count": row["run_count"], "last_run_at": row["last_run_at"]}
        )
        for row in rows
    ]
    return Page(items=items, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/{project_id}")
async def get_project(project_id: uuid.UUID, services: Services) -> ProjectOut:
    return ProjectOut.model_validate(await services.projects.get(project_id))


@router.get("/{project_id}/runs")
async def list_project_runs(
    project_id: uuid.UUID, services: Services, paging: Paging
) -> Page[RunSummary]:
    await services.projects.get(project_id)
    rows, total = await services.runs.list_runs(
        project_id=project_id, status=None, limit=paging.limit, offset=paging.offset
    )
    return Page(
        items=[run_summary(run, partial) for run, partial in rows],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.post("/{project_id}/runs", status_code=status.HTTP_201_CREATED)
async def create_run(
    project_id: uuid.UUID,
    body: RunCreate,
    *,
    key: IdempotencyKey,
    request: Request,
    response: Response,
    services: Services,
) -> RunSummary:
    """Queue a research run. Re-sending the same Idempotency-Key returns the same run."""
    enforce_run_rate_limit(request, services)
    run, created = await services.runs.create(
        project_id,
        question=body.question,
        context=body.context,
        input_urls=body.input_urls,
        depth=body.depth,
        idempotency_key=key,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return run_summary(run, None)
