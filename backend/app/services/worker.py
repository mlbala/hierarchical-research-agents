"""The single managed background execution loop.

* Queued runs live in PostgreSQL, so they survive restarts. This is not a
  FastAPI request-scoped background task.
* Only one worker may execute runs: it must hold a session-level PostgreSQL
  advisory lock on a dedicated connection. A second process (or a second
  uvicorn worker) stays in standby and serves the API only.
* Runs are claimed with an atomic ``UPDATE ... WHERE id = (SELECT ... FOR
  UPDATE SKIP LOCKED)``, so a run can never be claimed twice.
* When the lock is acquired, any run still marked ``running`` (other than our
  own) belonged to a worker that is gone: it is marked ``interrupted`` (or
  ``cancelled`` if cancellation had been requested). There is no transparent
  resume and no exactly-once guarantee for external API calls; users re-run.
* Cancellation is persisted by the API, signalled in-process when possible and
  also polled from the database; in-flight work is cancelled via asyncio.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import structlog
from langgraph.errors import GraphRecursionError
from sqlalchemy import select, update

from app.agents.context import RunContext
from app.agents.demo_llm import DemoLLM
from app.agents.graph import RECURSION_LIMIT, build_graph, initial_state
from app.agents.llm import AnthropicLLM, LLMClient
from app.agents.openai_llm import OpenAILLM
from app.core.config import Settings
from app.core.errors import AppError, ResearchTimeoutError, RunCancelledError
from app.core.logging import redact_text
from app.db.engine import Database, open_raw_connection
from app.db.models import AgentTask, ResearchRun, utcnow
from app.services.events import EventBroker, EventRecorder
from app.tools.demo import DemoPageFetcher, DemoSearch
from app.tools.fetch import PageFetcher, SafePageFetcher
from app.tools.search import SearchProvider, TavilySearch

log = structlog.get_logger(__name__)

Providers = tuple[LLMClient, SearchProvider, PageFetcher]
ProviderFactory = Callable[[Settings, str], Providers]

HEARTBEAT_SECONDS = 5.0
LOCK_CHECK_SECONDS = 15.0
INTERRUPTED_MESSAGE = "The server stopped before this run finished. Re-run it to start again."


def default_providers(settings: Settings, execution_mode: str) -> Providers:
    """Demo mode never touches the network; live mode requires full config."""
    if execution_mode == "demo":
        return DemoLLM(settings.demo_step_delay_ms), DemoSearch(), DemoPageFetcher()
    llm: LLMClient = (
        OpenAILLM(settings) if settings.llm_provider == "openai" else AnthropicLLM(settings)
    )
    search = TavilySearch(settings.tavily_api_key, timeout_seconds=settings.search_timeout_seconds)
    fetcher = SafePageFetcher(
        timeout_seconds=settings.fetch_timeout_seconds,
        max_bytes=settings.fetch_max_bytes,
        max_chars=settings.source_max_chars,
    )
    return llm, search, fetcher


def advisory_lock_key(schema: str) -> int:
    """Stable signed 64-bit key, distinct per application schema."""
    digest = hashlib.sha256(f"research-hive-worker:{schema}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@dataclass
class ActiveRun:
    task: asyncio.Task[None]
    cancel_event: asyncio.Event


class ResearchWorker:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        broker: EventBroker,
        *,
        provider_factory: ProviderFactory = default_providers,
        poll_interval: float = 2.0,
    ) -> None:
        self._settings = settings
        self._db = db
        self._events = EventRecorder(db, broker)
        self._providers = provider_factory
        self._poll = poll_interval
        self._graph = build_graph()
        self._wake = asyncio.Event()
        self._active: dict[uuid.UUID, ActiveRun] = {}
        self._lock_conn: psycopg.AsyncConnection[Any] | None = None
        self._lock_key = advisory_lock_key(settings.db_schema)
        self._main: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_lock_check = 0.0
        self._standby_logged = False

    # --- Public API ------------------------------------------------------------------

    @property
    def lock_held(self) -> bool:
        return self._lock_conn is not None

    @property
    def active_run_ids(self) -> list[uuid.UUID]:
        return list(self._active)

    async def start(self) -> None:
        self._stopping = False
        self._main = asyncio.create_task(self._loop(), name="research-worker")

    async def stop(self, grace_seconds: float = 15.0) -> None:
        """Graceful shutdown: in-flight runs are marked interrupted."""
        self._stopping = True
        self._wake.set()
        tasks = [active.task for active in self._active.values()]
        for task in tasks:
            task.cancel()  # cancel_event stays unset -> "interrupted", not "cancelled"
        if tasks:
            await asyncio.wait(tasks, timeout=grace_seconds)
        if self._main is not None:
            self._main.cancel()
            await asyncio.gather(self._main, return_exceptions=True)
        await self._release_lock()

    def notify_new_run(self) -> None:
        self._wake.set()

    def request_cancel(self, run_id: uuid.UUID) -> None:
        active = self._active.get(run_id)
        if active is not None and not active.cancel_event.is_set():
            active.cancel_event.set()
            active.task.cancel()

    # --- Loop ---------------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the loop alive; log once per failure
                log.error("worker.loop_error", error_type=type(exc).__name__, exc_info=exc)
                await self._release_lock()
            self._wake.clear()
            interval = 1.0 if self._active else self._poll
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=interval)

    async def tick(self) -> None:
        """One iteration: hold the lock, sync cancellations, claim work."""
        if not self.lock_held:
            await self._acquire_lock()
            if not self.lock_held:
                return
        await self._check_lock()
        if not self.lock_held:
            return
        await self._sync_cancellations()
        while len(self._active) < self._settings.max_concurrent_runs and not self._stopping:
            run_id = await self.claim_next()
            if run_id is None:
                break
            self._spawn(run_id)

    # --- Single-worker lock -------------------------------------------------------------

    async def _acquire_lock(self) -> None:
        conn = await open_raw_connection(self._settings)
        try:
            cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (self._lock_key,))
            row = await cursor.fetchone()
        except Exception:
            await conn.close()
            raise
        if row is None or not row[0]:
            await conn.close()
            if not self._standby_logged:
                log.info("worker.standby", reason="another worker holds the execution lock")
                self._standby_logged = True
            return
        self._lock_conn = conn
        self._standby_logged = False
        self._last_lock_check = asyncio.get_running_loop().time()
        log.info("worker.lock_acquired")
        await self.recover_interrupted()

    async def _check_lock(self) -> None:
        now = asyncio.get_running_loop().time()
        if self._lock_conn is None or now - self._last_lock_check < LOCK_CHECK_SECONDS:
            return
        self._last_lock_check = now
        try:
            await self._lock_conn.execute("SELECT 1")
        except psycopg.Error:
            log.warning("worker.lock_lost")
            await self._release_lock()

    async def _release_lock(self) -> None:
        conn, self._lock_conn = self._lock_conn, None
        if conn is not None:
            with contextlib.suppress(psycopg.Error):
                await conn.close()  # closing the session releases the advisory lock

    # --- Recovery and claiming -------------------------------------------------------

    async def recover_interrupted(self) -> list[uuid.UUID]:
        """Mark runs left ``running`` by a previous (dead) worker as interrupted."""
        now = utcnow()
        own = list(self._active)
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ResearchRun.id, ResearchRun.cancel_requested_at).where(
                        ResearchRun.status == "running", ResearchRun.id.not_in(own)
                    )
                )
            ).all()
            recovered = []
            for run_id, cancel_requested in rows:
                status = "cancelled" if cancel_requested is not None else "interrupted"
                await session.execute(
                    update(ResearchRun)
                    .where(ResearchRun.id == run_id, ResearchRun.status == "running")
                    .values(
                        status=status,
                        finished_at=now,
                        error_code="RUN_CANCELLED"
                        if status == "cancelled"
                        else "WORKER_INTERRUPTED",
                        error_message=None if status == "cancelled" else INTERRUPTED_MESSAGE,
                    )
                )
                await session.execute(
                    update(AgentTask)
                    .where(AgentTask.run_id == run_id, AgentTask.status == "running")
                    .values(status="interrupted", finished_at=now, error_code="WORKER_INTERRUPTED")
                )
                recovered.append((run_id, status))
        for run_id, status in recovered:
            await self._events.emit(
                run_id,
                f"run_{status}",
                INTERRUPTED_MESSAGE if status == "interrupted" else "Research cancelled.",
                level="warning",
            )
            log.warning("run.recovered_after_restart", run_id=str(run_id), status=status)
        return [r for r, _ in recovered]

    async def claim_next(self) -> uuid.UUID | None:
        """Atomically move the oldest queued run to ``running``."""
        next_queued = (
            select(ResearchRun.id)
            .where(ResearchRun.status == "queued", ResearchRun.cancel_requested_at.is_(None))
            .order_by(ResearchRun.created_at, ResearchRun.id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        now = utcnow()
        async with self._db.session() as session:
            return await session.scalar(
                update(ResearchRun)
                .where(ResearchRun.id == next_queued, ResearchRun.status == "queued")
                .values(status="running", started_at=now, heartbeat_at=now)
                .returning(ResearchRun.id)
            )

    async def _sync_cancellations(self) -> None:
        """Pick up cancellation requests persisted by other processes."""
        if not self._active:
            return
        async with self._db.session() as session:
            ids = await session.scalars(
                select(ResearchRun.id).where(
                    ResearchRun.id.in_(list(self._active)),
                    ResearchRun.cancel_requested_at.is_not(None),
                )
            )
            requested = list(ids)
        for run_id in requested:
            self.request_cancel(run_id)

    def _spawn(self, run_id: uuid.UUID) -> None:
        cancel_event = asyncio.Event()
        task = asyncio.create_task(self.execute(run_id, cancel_event), name=f"run-{run_id}")
        self._active[run_id] = ActiveRun(task=task, cancel_event=cancel_event)

        def _done(_: asyncio.Task[None]) -> None:
            self._active.pop(run_id, None)
            self._wake.set()

        task.add_done_callback(_done)

    # --- Execution ------------------------------------------------------------------

    async def execute(self, run_id: uuid.UUID, cancel_event: asyncio.Event) -> None:
        """Run the graph for one claimed run and persist its terminal state."""
        with structlog.contextvars.bound_contextvars(run_id=str(run_id)):
            async with self._db.session() as session:
                run = await session.get(ResearchRun, run_id)
            if run is None:
                return
            limits = self._settings.limits_for(run.depth)  # type: ignore[arg-type]
            ctx: RunContext | None = None
            status = "failed"
            error: AppError | None = None
            heartbeat: asyncio.Task[None] | None = None
            providers: Providers | None = None
            log.info("run.started", depth=run.depth, execution_mode=run.execution_mode)
            try:
                providers = self._providers(self._settings, run.execution_mode)
                ctx = RunContext(
                    run_id=run_id,
                    settings=self._settings,
                    limits=limits,
                    execution_mode=run.execution_mode,
                    db=self._db,
                    events=self._events,
                    llm=providers[0],
                    search=providers[1],
                    fetcher=providers[2],
                    cancel_event=cancel_event,
                )
                await ctx.emit(
                    "run_started",
                    f"Research started in {run.execution_mode} mode ({run.depth} depth).",
                    payload={"limits": limits.as_dict()},
                )
                heartbeat = asyncio.create_task(self._heartbeat(ctx))
                # The graph deadline is enforced per call by RunContext; this outer
                # timeout is a backstop slightly beyond it.
                async with asyncio.timeout(limits.deadline_seconds + 5):
                    final = await self._graph.ainvoke(
                        initial_state(
                            question=run.question,
                            context=run.context,
                            input_urls=list(run.input_urls),
                            depth=run.depth,
                        ),
                        context=ctx,
                        config={"recursion_limit": RECURSION_LIMIT},
                    )
                status = str(final.get("final_status", "completed"))
            except asyncio.CancelledError:
                if cancel_event.is_set():
                    status, error = "cancelled", RunCancelledError()
                else:
                    status = "interrupted"
                    error = AppError(INTERRUPTED_MESSAGE, code="WORKER_SHUTDOWN")
                    await asyncio.shield(self._finish(run_id, ctx, status, error))
                    raise
            except RunCancelledError as exc:
                status, error = "cancelled", exc
            except (TimeoutError, ResearchTimeoutError):
                status, error = "failed", ResearchTimeoutError()
            except GraphRecursionError:
                status = "failed"
                error = AppError("The workflow exceeded its step limit.", code="ROUTING_LIMIT")
            except AppError as exc:
                status, error = "failed", exc
            except Exception as exc:
                log.error("run.unexpected_error", error_type=type(exc).__name__, exc_info=exc)
                status = "failed"
                error = AppError("An unexpected error stopped the run.", code="INTERNAL_ERROR")
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                if providers is not None:
                    await self._close_providers(providers)
            await asyncio.shield(self._finish(run_id, ctx, status, error))

    async def _heartbeat(self, ctx: RunContext) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await ctx.persist_counters(heartbeat=True)
            except AppError:
                log.warning("run.heartbeat_failed")

    @staticmethod
    async def _close_providers(providers: Providers) -> None:
        for provider in providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):  # best-effort cleanup
                    await close()

    async def _finish(
        self, run_id: uuid.UUID, ctx: RunContext | None, status: str, error: AppError | None
    ) -> None:
        now = utcnow()
        values: dict[str, Any] = {"status": status, "finished_at": now, "heartbeat_at": now}
        if error is not None:
            values["error_code"] = error.code
            values["error_message"] = redact_text(error.safe_message)[:500]
        if ctx is not None:
            values.update(
                input_tokens=ctx.input_tokens,
                output_tokens=ctx.output_tokens,
                llm_call_count=ctx.llm_calls,
                tool_call_count=ctx.tool_calls,
                estimated_cost_usd=ctx.estimated_cost(),
            )
        leftover = "cancelled" if status == "cancelled" else (
            "interrupted" if status == "interrupted" else "failed"
        )  # fmt: skip
        async with self._db.session() as session:
            await session.execute(
                update(ResearchRun)
                .where(ResearchRun.id == run_id, ResearchRun.status == "running")
                .values(**values)
            )
            await session.execute(
                update(AgentTask)
                .where(AgentTask.run_id == run_id, AgentTask.status == "running")
                .values(status=leftover, finished_at=now, error_code=values.get("error_code"))
            )
        messages = {
            "completed": ("run_completed", "Research completed.", "info"),
            "partial": ("run_partial", "Research finished with a partial report.", "warning"),
            "cancelled": (
                "run_cancelled",
                "Research cancelled. Collected evidence was kept.",
                "warning",
            ),
            "interrupted": ("run_interrupted", INTERRUPTED_MESSAGE, "warning"),
            "failed": (
                "run_failed",
                f"Research failed: {error.safe_message if error else 'unknown error'}",
                "error",
            ),
        }
        event_type, message, level = messages[status]
        await self._events.emit(
            run_id,
            event_type,
            message,
            level=level,  # type: ignore[arg-type]
            payload={"status": status, "error_code": error.code if error else None},
        )
        log.info(
            "run.finished",
            run_id=str(run_id),
            status=status,
            error_code=error.code if error else None,
            input_tokens=ctx.input_tokens if ctx else 0,
            output_tokens=ctx.output_tokens if ctx else 0,
            llm_calls=ctx.llm_calls if ctx else 0,
            tool_calls=ctx.tool_calls if ctx else 0,
        )
