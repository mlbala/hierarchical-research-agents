"""Projects and research runs: creation, listing, state transitions.

Run lifecycle (enforced here and by the worker)::

    queued -> running -> completed | partial | failed | cancelled | interrupted
    queued -> cancelled

Creation is idempotent: the same ``Idempotency-Key`` with the same request
returns the existing run; reusing a key for a different request is a conflict.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.errors import (
    ConfigurationError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    RateLimitedError,
)
from app.db.engine import Database
from app.db.models import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    AgentTask,
    Finding,
    Project,
    Report,
    ResearchRun,
    Source,
    utcnow,
)
from app.services.events import EventRecorder
from app.services.readiness import configuration_issues


class ProjectService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, name: str, description: str) -> Project:
        now = utcnow()
        project = Project(
            id=uuid.uuid4(), name=name, description=description, created_at=now, updated_at=now
        )
        async with self._db.session() as session:
            session.add(project)
        return project

    async def get(self, project_id: uuid.UUID) -> Project:
        async with self._db.session() as session:
            project = await session.get(Project, project_id)
        if project is None:
            raise NotFoundError("Project not found.")
        return project

    async def list_projects(self, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        last_run = (
            select(
                ResearchRun.project_id,
                func.count(ResearchRun.id).label("run_count"),
                func.max(ResearchRun.created_at).label("last_run_at"),
            )
            .group_by(ResearchRun.project_id)
            .subquery()
        )
        async with self._db.session() as session:
            total = int(await session.scalar(select(func.count()).select_from(Project)) or 0)
            rows = (
                await session.execute(
                    select(Project, last_run.c.run_count, last_run.c.last_run_at)
                    .outerjoin(last_run, last_run.c.project_id == Project.id)
                    .order_by(
                        func.coalesce(last_run.c.last_run_at, Project.updated_at).desc(),
                        Project.id,
                    )
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return [
            {"project": p, "run_count": int(count or 0), "last_run_at": last}
            for p, count, last in rows
        ], total


class RunService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        events: EventRecorder,
        *,
        on_queued: Callable[[], None] | None = None,
        on_cancel: Callable[[uuid.UUID], None] | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._events = events
        self._on_queued = on_queued
        self._on_cancel = on_cancel

    # --- Creation -----------------------------------------------------------------

    def _assert_configured(self) -> None:
        errors = [i for i in configuration_issues(self._settings) if i.severity == "error"]
        if errors and not self._settings.demo_mode:
            keys = ", ".join(i.key for i in errors)
            raise ConfigurationError(
                f"Live research is not configured. Missing or invalid: {keys}."
            )

    @staticmethod
    def _same_request(run: ResearchRun, project_id: uuid.UUID, fields: dict[str, Any]) -> bool:
        return (
            run.project_id == project_id
            and run.question == fields["question"]
            and (run.context or None) == (fields["context"] or None)
            and list(run.input_urls) == list(fields["input_urls"])
            and run.depth == fields["depth"]
        )

    async def create(
        self,
        project_id: uuid.UUID,
        *,
        question: str,
        context: str | None,
        input_urls: Sequence[str],
        depth: str,
        idempotency_key: str,
    ) -> tuple[ResearchRun, bool]:
        """Queue a run. Returns ``(run, created)``."""
        fields = {
            "question": question,
            "context": context,
            "input_urls": list(input_urls),
            "depth": depth,
        }
        async with self._db.session() as session:
            existing = await session.scalar(
                select(ResearchRun).where(ResearchRun.idempotency_key == idempotency_key)
            )
            if existing is not None:
                if not self._same_request(existing, project_id, fields):
                    raise ConflictError(
                        "This idempotency key was already used for a different request.",
                        code="IDEMPOTENCY_KEY_REUSED",
                    )
                return existing, False

        self._assert_configured()
        run = ResearchRun(
            id=uuid.uuid4(),
            project_id=project_id,
            question=question,
            context=context,
            input_urls=list(input_urls),
            depth=depth,
            status="queued",
            execution_mode=self._settings.execution_mode,
            settings_snapshot=self._settings.settings_snapshot(depth),  # type: ignore[arg-type]
            warnings=[],
            idempotency_key=idempotency_key,
            created_at=utcnow(),
        )
        try:
            async with self._db.session() as session:
                if await session.get(Project, project_id) is None:
                    raise NotFoundError("Project not found.")
                active = await session.scalar(
                    select(func.count())
                    .select_from(ResearchRun)
                    .where(ResearchRun.status.in_(ACTIVE_RUN_STATUSES))
                )
                if int(active or 0) >= self._settings.max_queued_runs:
                    raise RateLimitedError(
                        "The research queue is full. Wait for a run to finish and try again.",
                        code="QUEUE_FULL",
                    )
                session.add(run)
                await session.execute(
                    update(Project).where(Project.id == project_id).values(updated_at=utcnow())
                )
        except IntegrityError:
            # A concurrent request with the same key won the race.
            async with self._db.session() as session:
                winner = await session.scalar(
                    select(ResearchRun).where(ResearchRun.idempotency_key == idempotency_key)
                )
            if winner is None or not self._same_request(winner, project_id, fields):
                raise ConflictError(
                    "This idempotency key was already used for a different request.",
                    code="IDEMPOTENCY_KEY_REUSED",
                ) from None
            return winner, False

        await self._events.emit(
            run.id,
            "run_queued",
            f"Research queued ({depth} depth, {run.execution_mode} mode).",
            payload={"depth": depth, "execution_mode": run.execution_mode},
        )
        if self._on_queued is not None:
            self._on_queued()
        return run, True

    async def rerun(self, run_id: uuid.UUID, idempotency_key: str) -> tuple[ResearchRun, bool]:
        """Start a *new* run with the same question, context, URLs and depth."""
        source = await self.get(run_id)
        return await self.create(
            source.project_id,
            question=source.question,
            context=source.context,
            input_urls=list(source.input_urls),
            depth=source.depth,
            idempotency_key=idempotency_key,
        )

    # --- Reads ---------------------------------------------------------------------

    async def get(self, run_id: uuid.UUID) -> ResearchRun:
        async with self._db.session() as session:
            run = await session.get(ResearchRun, run_id)
        if run is None:
            raise NotFoundError("Research run not found.")
        return run

    async def detail(self, run_id: uuid.UUID) -> dict[str, Any]:
        """Run plus derived fields the UI needs (report, handoff, parent project)."""
        async with self._db.session() as session:
            run = await session.get(ResearchRun, run_id)
            if run is None:
                raise NotFoundError("Research run not found.")
            report = (
                await session.execute(
                    select(Report.id, Report.is_partial).where(Report.run_id == run_id)
                )
            ).first()
            handoff = await session.scalar(
                select(AgentTask.output)
                .where(
                    AgentTask.run_id == run_id,
                    AgentTask.task_key == "research_director.handoff",
                    AgentTask.status == "completed",
                )
                .order_by(AgentTask.attempt.desc())
                .limit(1)
            )
            counts = (
                await session.execute(
                    select(
                        select(func.count())
                        .select_from(Source)
                        .where(Source.run_id == run_id)
                        .scalar_subquery(),
                        select(func.count())
                        .select_from(Finding)
                        .where(Finding.run_id == run_id)
                        .scalar_subquery(),
                    )
                )
            ).one()
        return {
            "run": run,
            "report_id": report[0] if report else None,
            "is_partial": report[1] if report else None,
            "handoff": handoff,
            "source_count": int(counts[0]),
            "finding_count": int(counts[1]),
        }

    async def list_runs(
        self,
        *,
        project_id: uuid.UUID | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[ResearchRun, bool | None]], int]:
        conditions = []
        if project_id is not None:
            conditions.append(ResearchRun.project_id == project_id)
        if status is not None:
            conditions.append(ResearchRun.status == status)
        async with self._db.session() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(ResearchRun).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(ResearchRun, Report.is_partial)
                    .outerjoin(Report, Report.run_id == ResearchRun.id)
                    .where(*conditions)
                    .order_by(ResearchRun.created_at.desc(), ResearchRun.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
        return [(r, p) for r, p in rows], total

    async def tasks(self, run_id: uuid.UUID) -> list[AgentTask]:
        await self.get(run_id)
        async with self._db.session() as session:
            return list(
                await session.scalars(
                    select(AgentTask)
                    .where(AgentTask.run_id == run_id)
                    .order_by(AgentTask.started_at, AgentTask.id)
                )
            )

    async def findings(
        self, run_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Findings annotated with the research lead's latest review outcome."""
        await self.get(run_id)
        async with self._db.session() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(Finding).where(Finding.run_id == run_id)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(Finding, AgentTask.agent_role)
                    .join(AgentTask, AgentTask.id == Finding.task_id)
                    .where(Finding.run_id == run_id)
                    .order_by(Finding.created_at, Finding.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            review = await session.scalar(
                select(AgentTask.output)
                .where(
                    AgentTask.run_id == run_id,
                    AgentTask.task_key == "research_team_lead.review",
                    AgentTask.status == "completed",
                )
                .order_by(AgentTask.attempt.desc())
                .limit(1)
            )
        rejected = set((review or {}).get("rejected_finding_ids", []))
        return [
            {
                "finding": f,
                "agent_role": role,
                "review_status": (
                    "pending"
                    if review is None
                    else "rejected"
                    if str(f.id) in rejected
                    else "approved"
                ),
            }
            for f, role in rows
        ], total

    async def sources(
        self, run_id: uuid.UUID, *, limit: int, offset: int
    ) -> tuple[list[Source], int]:
        await self.get(run_id)
        async with self._db.session() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(Source).where(Source.run_id == run_id)
                )
                or 0
            )
            rows = await session.scalars(
                select(Source)
                .where(Source.run_id == run_id)
                .order_by(Source.retrieved_at, Source.id)
                .limit(limit)
                .offset(offset)
            )
            return list(rows), total

    async def source(self, run_id: uuid.UUID, source_id: uuid.UUID) -> Source:
        async with self._db.session() as session:
            source = await session.scalar(
                select(Source).where(Source.run_id == run_id, Source.id == source_id)
            )
        if source is None:
            raise NotFoundError("Source not found in this run.")
        return source

    # --- Cancellation ----------------------------------------------------------------

    async def cancel(self, run_id: uuid.UUID) -> ResearchRun:
        """Cancel a queued run immediately, or request cancellation of a running one."""
        now = utcnow()
        async with self._db.session() as session:
            cancelled_queued = await session.scalar(
                update(ResearchRun)
                .where(ResearchRun.id == run_id, ResearchRun.status == "queued")
                .values(
                    status="cancelled",
                    cancel_requested_at=now,
                    finished_at=now,
                    error_code="RUN_CANCELLED",
                    error_message="Cancelled before it started.",
                )
                .returning(ResearchRun.id)
            )
            requested = None
            if cancelled_queued is None:
                requested = await session.scalar(
                    update(ResearchRun)
                    .where(ResearchRun.id == run_id, ResearchRun.status == "running")
                    .values(cancel_requested_at=func.coalesce(ResearchRun.cancel_requested_at, now))
                    .returning(ResearchRun.id)
                )
        if cancelled_queued is not None:
            await self._events.emit(
                run_id, "run_cancelled", "Research cancelled before it started."
            )
        elif requested is not None:
            await self._events.emit(
                run_id,
                "cancel_requested",
                "Cancellation requested; stopping after in-flight work is cancelled.",
                level="warning",
            )
            if self._on_cancel is not None:
                self._on_cancel(run_id)
        else:
            run = await self.get(run_id)
            if run.status in TERMINAL_RUN_STATUSES:
                raise InvalidStateError(f"The run is already {run.status} and cannot be cancelled.")
        return await self.get(run_id)
