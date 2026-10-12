"""Research-run endpoints, including the SSE progress stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import IdempotencyKey, Paging, Services
from app.core.errors import RateLimitedError
from app.core.security import client_ip
from app.db.models import TERMINAL_RUN_STATUSES, ResearchRun, RunEvent, Source
from app.schemas.api import (
    EventOut,
    FindingOut,
    Page,
    ReportOut,
    RunDetail,
    RunStatus,
    RunSummary,
    SourceDetail,
    SourceOut,
    TaskOut,
)
from app.services.events import list_events
from app.services.pdf import render_report_pdf

router = APIRouter(prefix="/runs", tags=["runs"])

SSE_HEARTBEAT_SECONDS = 15.0
SSE_POLL_SECONDS = 2.0


def enforce_run_rate_limit(request: Request, services: Services) -> None:
    ip = client_ip(request.scope, services.settings.trusted_proxy_list)
    if not services.run_rate_limiter.allow(ip):
        raise RateLimitedError("Too many research runs started recently. Please wait a minute.")


def run_summary(run: ResearchRun, is_partial: bool | None) -> RunSummary:
    summary = RunSummary.model_validate(run)
    return summary.model_copy(update={"is_partial": is_partial})


def source_out(source: Source, *, excerpt_chars: int = 600) -> SourceOut:
    return SourceOut(
        id=source.id,
        url=source.url,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        retrieved_at=source.retrieved_at,
        content_kind=source.content_kind,
        content_hash=source.content_hash,
        content_length=len(source.content_text),
        excerpt=source.content_text[:excerpt_chars],
        truncated=bool((source.meta or {}).get("truncated", False)),
    )


@router.get("")
async def list_runs(
    services: Services,
    paging: Paging,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
) -> Page[RunSummary]:
    rows, total = await services.runs.list_runs(
        project_id=None, status=run_status, limit=paging.limit, offset=paging.offset
    )
    return Page(
        items=[run_summary(r, p) for r, p in rows],
        total=total,
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, services: Services) -> RunDetail:
    detail = await services.runs.detail(run_id)
    run: ResearchRun = detail["run"]
    base = RunSummary.model_validate(run).model_dump()
    return RunDetail(
        **{**base, "is_partial": detail["is_partial"]},
        context=run.context,
        input_urls=list(run.input_urls),
        plan=run.plan,
        warnings=list(run.warnings),
        cancel_requested_at=run.cancel_requested_at,
        heartbeat_at=run.heartbeat_at,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        llm_call_count=run.llm_call_count,
        tool_call_count=run.tool_call_count,
        estimated_cost_usd=(
            str(run.estimated_cost_usd) if run.estimated_cost_usd is not None else None
        ),
        error_message=run.error_message,
        settings_snapshot=run.settings_snapshot,
        report_id=detail["report_id"],
        handoff=detail["handoff"],
        source_count=detail["source_count"],
        finding_count=detail["finding_count"],
    )


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: uuid.UUID, services: Services) -> RunSummary:
    run = await services.runs.cancel(run_id)
    return run_summary(run, None)


@router.post("/{run_id}/rerun", status_code=status.HTTP_201_CREATED)
async def rerun(
    run_id: uuid.UUID,
    key: IdempotencyKey,
    request: Request,
    response: Response,
    services: Services,
) -> RunSummary:
    """Start a new run with the same inputs (interrupted runs are never resumed)."""
    enforce_run_rate_limit(request, services)
    run, created = await services.runs.rerun(run_id, key)
    if not created:
        response.status_code = status.HTTP_200_OK
    return run_summary(run, None)


@router.get("/{run_id}/tasks")
async def list_tasks(run_id: uuid.UUID, services: Services) -> list[TaskOut]:
    return [TaskOut.model_validate(t) for t in await services.runs.tasks(run_id)]


@router.get("/{run_id}/findings")
async def list_findings(run_id: uuid.UUID, services: Services, paging: Paging) -> Page[FindingOut]:
    rows, total = await services.runs.findings(run_id, limit=paging.limit, offset=paging.offset)
    items = [
        FindingOut(
            id=row["finding"].id,
            task_id=row["finding"].task_id,
            source_id=row["finding"].source_id,
            agent_role=row["agent_role"],
            claim=row["finding"].claim,
            evidence_excerpt=row["finding"].evidence_excerpt,
            support_level=row["finding"].support_level,
            review_status=row["review_status"],
            created_at=row["finding"].created_at,
        )
        for row in rows
    ]
    return Page(items=items, total=total, limit=paging.limit, offset=paging.offset)


@router.get("/{run_id}/sources")
async def list_sources(run_id: uuid.UUID, services: Services, paging: Paging) -> Page[SourceOut]:
    rows, total = await services.runs.sources(run_id, limit=paging.limit, offset=paging.offset)
    return Page(
        items=[source_out(s) for s in rows], total=total, limit=paging.limit, offset=paging.offset
    )


@router.get("/{run_id}/sources/{source_id}")
async def get_source(run_id: uuid.UUID, source_id: uuid.UUID, services: Services) -> SourceDetail:
    source = await services.runs.source(run_id, source_id)
    return SourceDetail(**source_out(source).model_dump(), content_text=source.content_text)


@router.get("/{run_id}/events")
async def get_events(
    run_id: uuid.UUID,
    services: Services,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[EventOut]:
    await services.runs.get(run_id)
    events = await list_events(services.db, run_id, after_id=after, limit=limit)
    return [EventOut.model_validate(e) for e in events]


def _sse(event: str, data: dict[str, Any], event_id: int | None = None) -> bytes:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, default=str, separators=(',', ':'))}")
    return ("\n".join(lines) + "\n\n").encode()


def _event_payload(event: RunEvent) -> dict[str, Any]:
    return EventOut.model_validate(event).model_dump(mode="json")


@router.get("/{run_id}/events/stream")
async def stream_events(
    run_id: uuid.UUID,
    request: Request,
    services: Services,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    after: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Server-Sent Events: replay persisted events, then follow live ones.

    The stream resumes from ``Last-Event-ID`` (sent automatically by
    ``EventSource`` on reconnect) or ``?after=``. Events are always read from
    the database, so replay is ordered and nothing is lost across reconnects.
    Heartbeats are sent while idle. The stream ends after the run reaches a
    terminal status; closing it never cancels the run.
    """
    await services.runs.get(run_id)  # 404 before the stream starts
    cursor = after
    if last_event_id and re.fullmatch(r"\d{1,18}", last_event_id):
        cursor = max(cursor, int(last_event_id))

    async def generate() -> AsyncIterator[bytes]:
        nonlocal cursor
        last_sent = asyncio.get_running_loop().time()
        # retry: tells EventSource how long to wait before reconnecting (ms).
        yield b"retry: 3000\n\n"
        with services.broker.subscribe(run_id) as wake:
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    wake.clear()
                    events = await list_events(services.db, run_id, after_id=cursor, limit=200)
                    for event in events:
                        cursor = event.id
                        yield _sse("run_event", _event_payload(event), event.id)
                        last_sent = asyncio.get_running_loop().time()
                    if len(events) == 200:
                        continue  # more backlog to replay
                    run = await services.runs.get(run_id)
                    if run.status in TERMINAL_RUN_STATUSES:
                        # Drain anything written between the two reads, then end.
                        tail = await list_events(services.db, run_id, after_id=cursor, limit=200)
                        for event in tail:
                            cursor = event.id
                            yield _sse("run_event", _event_payload(event), event.id)
                        yield _sse("end", {"status": run.status})
                        break
                    now = asyncio.get_running_loop().time()
                    if now - last_sent >= SSE_HEARTBEAT_SECONDS:
                        yield _sse("heartbeat", {"status": run.status})
                        last_sent = now
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), timeout=SSE_POLL_SECONDS)
            except asyncio.CancelledError:
                # Client went away; subscription cleanup happens in the context manager.
                raise

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{run_id}/report")
async def get_report(run_id: uuid.UUID, services: Services) -> ReportOut:
    data = await services.reports.get_for_run(run_id)
    report = data["report"]
    return ReportOut(
        id=report.id,
        run_id=report.run_id,
        title=report.title,
        executive_summary=report.executive_summary,
        body_markdown=report.body_markdown,
        limitations=list(report.limitations),
        is_partial=report.is_partial,
        execution_mode=data["execution_mode"],
        created_at=report.created_at,
        updated_at=report.updated_at,
        citations=[
            {
                "number": c["number"],
                "source": source_out(c["source"], excerpt_chars=300),
                "evidence": c["evidence"],
            }
            for c in data["citations"]
        ],
    )


@router.get("/{run_id}/report.md")
async def download_report(run_id: uuid.UUID, services: Services) -> Response:
    title, markdown = await services.reports.markdown_for_run(run_id)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "research-report"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}.md"'},
    )


@router.get("/{run_id}/report.pdf")
async def download_report_pdf(run_id: uuid.UUID, services: Services) -> Response:
    title, markdown = await services.reports.markdown_for_run(run_id)
    content = await run_in_threadpool(render_report_pdf, title, markdown)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "research-report"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{slug}.pdf"'},
    )
