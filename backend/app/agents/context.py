"""Per-run execution context shared by all graph nodes.

``RunContext`` is passed to LangGraph as the runtime context. It is the one
place where the bounded-execution rules are enforced for every agent step:

* cancellation and the run deadline are checked before every task, LLM call
  and tool call (``ensure_active``);
* budgets are reserved before dispatch and shared across parallel tasks;
* at most ``max_parallel_agent_tasks`` agent tasks run at once;
* every agent task is recorded in ``agent_tasks`` with timing, tokens and a
  sanitised outcome, and announced as a user-visible run event;
* database sessions are opened only for short writes, never around an LLM or
  network call.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy import update

from app.agents.llm import LLMClient, LLMOutputError, LLMRequest, LLMUsage
from app.agents.roles import ROLE_LABELS, Role
from app.core.config import RunLimits, Settings
from app.core.errors import AppError, ProviderError, ResearchTimeoutError, RunCancelledError
from app.core.logging import redact_text
from app.core.retry import Deadline, RetryPolicy, run_with_retries
from app.db.engine import Database
from app.db.models import AgentTask, ResearchRun, utcnow
from app.services.budget import RunBudget
from app.services.events import EventRecorder, Level
from app.services.evidence import EvidenceStore
from app.tools.fetch import PageFetcher
from app.tools.registry import RunTools
from app.tools.search import SearchProvider

log = structlog.get_logger(__name__)


@dataclass
class TaskHandle:
    """Mutable record of one agent task while it runs."""

    id: uuid.UUID
    role: Role
    task_key: str
    attempt: int
    started_monotonic: float
    input_tokens: int = 0
    output_tokens: int = 0
    output: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    def set_output(self, summary: str, **details: Any) -> None:
        self.output = {"summary": summary[:500], **details}


@dataclass(eq=False, kw_only=True)
class RunContext:
    """LangGraph runtime context (a dataclass, as LangGraph requires)."""

    run_id: uuid.UUID
    settings: Settings
    limits: RunLimits
    execution_mode: str
    db: Database
    events: EventRecorder
    llm: LLMClient
    search: SearchProvider
    fetcher: PageFetcher
    cancel_event: asyncio.Event

    # Derived per-run state (not constructor arguments).
    evidence: EvidenceStore = field(init=False)
    budget: RunBudget = field(init=False)
    deadline: Deadline = field(init=False)
    retry_policy: RetryPolicy = field(init=False)
    tools: RunTools = field(init=False)
    input_tokens: int = field(init=False, default=0)
    output_tokens: int = field(init=False, default=0)
    llm_calls: int = field(init=False, default=0)
    tool_calls: int = field(init=False, default=0)
    warnings: list[dict[str, str]] = field(init=False, default_factory=list)
    _parallel: asyncio.Semaphore = field(init=False, repr=False)
    _attempts: Counter[str] = field(init=False, repr=False, default_factory=Counter)

    def __post_init__(self) -> None:
        self.evidence = EvidenceStore(self.db)
        self.budget = RunBudget(self.limits)
        self.deadline = Deadline(self.limits.deadline_seconds)
        self.retry_policy = RetryPolicy(max_retries=self.settings.transient_retries)
        self._parallel = asyncio.Semaphore(self.settings.max_parallel_agent_tasks)
        self.tools = RunTools(self)

    # --- Guards ---------------------------------------------------------------

    async def ensure_active(self) -> None:
        """Raise if the run was cancelled or ran out of time."""
        if self.cancel_event.is_set():
            raise RunCancelledError()
        if self.deadline.remaining() < 1:
            raise ResearchTimeoutError()

    # --- Events ---------------------------------------------------------------

    async def emit(
        self,
        event_type: str,
        message: str,
        *,
        level: Level = "info",
        task_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.events.emit(
            self.run_id, event_type, message, level=level, task_id=task_id, payload=payload
        )

    async def stage(self, key: str, label: str, **payload: Any) -> None:
        await self.emit("stage_changed", label, payload={"stage": key, **payload})

    async def warn(self, code: str, message: str, *, task_id: uuid.UUID | None = None) -> None:
        """Record a user-visible warning on the run (deduplicated by message)."""
        if any(w["message"] == message for w in self.warnings):
            return
        self.warnings.append({"code": code, "message": message})
        async with self.db.session() as session:
            await session.execute(
                update(ResearchRun)
                .where(ResearchRun.id == self.run_id)
                .values(warnings=list(self.warnings))
            )
        await self.emit(
            "warning", message, level="warning", task_id=task_id, payload={"code": code}
        )

    async def save_plan(self, plan: dict[str, Any]) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(ResearchRun).where(ResearchRun.id == self.run_id).values(plan=plan)
            )

    # --- Agent tasks ------------------------------------------------------------

    def _next_attempt(self, task_key: str) -> int:
        self._attempts[task_key] += 1
        return self._attempts[task_key]

    @asynccontextmanager
    async def agent_task(
        self,
        role: Role,
        task_key: str,
        *,
        input_summary: str,
        parent_task_id: uuid.UUID | None,
    ) -> AsyncIterator[TaskHandle]:
        """Record one agent task from start to a terminal status."""
        await self.ensure_active()
        async with self._parallel:
            await self.ensure_active()
            handle = TaskHandle(
                id=uuid.uuid4(),
                role=role,
                task_key=task_key,
                attempt=self._next_attempt(task_key),
                started_monotonic=time.monotonic(),
            )
            async with self.db.session() as session:
                session.add(
                    AgentTask(
                        id=handle.id,
                        run_id=self.run_id,
                        parent_task_id=parent_task_id,
                        agent_role=role.value,
                        task_key=task_key,
                        attempt=handle.attempt,
                        status="running",
                        input_summary=input_summary[:1000],
                        started_at=utcnow(),
                    )
                )
            await self.emit(
                "agent_started",
                f"{ROLE_LABELS[role]} started: {input_summary[:200]}",
                task_id=handle.id,
                payload={"role": role.value, "task_key": task_key, "attempt": handle.attempt},
            )
            bound = structlog.contextvars.bound_contextvars(
                task_id=str(handle.id), agent_role=role.value
            )
            with bound:
                try:
                    yield handle
                except asyncio.CancelledError:
                    status = "cancelled" if self.cancel_event.is_set() else "interrupted"
                    await self._finish(handle, status, error=RunCancelledError())
                    raise
                except RunCancelledError as exc:
                    await self._finish(handle, "cancelled", error=exc)
                    raise
                except AppError as exc:
                    await self._finish(handle, "failed", error=exc)
                    raise
                except Exception as exc:
                    log.error("agent_task.unexpected_error", error_type=type(exc).__name__)
                    await self._finish(handle, "failed", error=AppError(code="INTERNAL_ERROR"))
                    raise
                else:
                    await self._finish(handle, "skipped" if handle.skipped else "completed")

    async def _finish(self, handle: TaskHandle, status: str, error: AppError | None = None) -> None:
        duration_ms = int((time.monotonic() - handle.started_monotonic) * 1000)
        values: dict[str, Any] = {
            "status": status,
            "finished_at": utcnow(),
            "duration_ms": duration_ms,
            "input_tokens": handle.input_tokens,
            "output_tokens": handle.output_tokens,
            "output": handle.output or None,
        }
        if error is not None:
            values["error_code"] = error.code
            values["error_message"] = redact_text(error.safe_message)[:500]
        # Terminal bookkeeping must survive the cancellation that triggered it.
        await asyncio.shield(self._write_task(handle.id, values))
        await self.persist_counters()
        label = ROLE_LABELS[handle.role]
        payload = {
            "role": handle.role.value,
            "task_key": handle.task_key,
            "attempt": handle.attempt,
            "status": status,
            "duration_ms": duration_ms,
            "summary": handle.output.get("summary"),
        }
        if status in ("completed", "skipped"):
            text = handle.output.get("summary") or status
            event = "agent_skipped" if status == "skipped" else "agent_completed"
            await self.emit(event, f"{label} {status}: {text}", task_id=handle.id, payload=payload)
        else:
            payload["error_code"] = values.get("error_code")
            level: Level = "warning" if status in ("cancelled", "interrupted") else "error"
            message = f"{label} {status}"
            if error is not None and status == "failed":
                message += f": {error.safe_message}"
            await self.emit(
                "agent_failed", message, level=level, task_id=handle.id, payload=payload
            )
        log.info(
            "agent_task.finished",
            run_id=str(self.run_id),
            task_id=str(handle.id),
            agent_role=handle.role.value,
            status=status,
            duration_ms=duration_ms,
            input_tokens=handle.input_tokens,
            output_tokens=handle.output_tokens,
            error_code=values.get("error_code"),
        )

    async def _write_task(self, task_id: uuid.UUID, values: dict[str, Any]) -> None:
        async with self.db.session() as session:
            await session.execute(update(AgentTask).where(AgentTask.id == task_id).values(**values))

    async def record_skipped(
        self, role: Role, task_key: str, *, reason: str, parent_task_id: uuid.UUID | None
    ) -> None:
        async with self.agent_task(
            role, task_key, input_summary=reason, parent_task_id=parent_task_id
        ) as task:
            task.skipped = True
            task.set_output(reason)

    # --- LLM calls ----------------------------------------------------------------

    def _record_usage(self, task: TaskHandle, usage: LLMUsage | None) -> None:
        if usage is None:
            return
        task.input_tokens += usage.input_tokens
        task.output_tokens += usage.output_tokens
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens

    async def call_llm[T: BaseModel](
        self,
        task: TaskHandle,
        *,
        system: str,
        prompt: str,
        schema: type[T],
        max_tokens: int,
    ) -> T:
        """Budgeted, deadline-bounded, retried and validated LLM call.

        Transient failures are retried with backoff. A malformed response gets
        exactly one corrective re-request; every attempt reserves LLM budget.
        """
        prompt = prompt[: self.settings.llm_max_prompt_chars]
        request = LLMRequest(
            role=task.role,
            task_key=task.task_key,
            system=system,
            prompt=prompt,
            max_tokens=min(max_tokens, self.settings.llm_max_output_tokens),
        )

        async def before_attempt(_: int) -> None:
            await self.ensure_active()
            await self.budget.reserve("llm")
            self.llm_calls += 1

        async def on_retry(attempt: int, exc: AppError, delay: float) -> None:
            log.warning(
                "llm.retry",
                run_id=str(self.run_id),
                task_id=str(task.id),
                agent_role=task.role.value,
                provider=self.llm.provider,
                model=self.llm.model,
                retry_attempt=attempt,
                delay_s=round(delay, 2),
                error_code=exc.code,
            )
            await self.emit(
                "retry",
                f"Retrying model call after a transient error ({exc.code}).",
                level="warning",
                task_id=task.id,
                payload={"attempt": attempt, "delay_s": round(delay, 2), "error_code": exc.code},
            )

        async def attempt(_: int) -> T:
            timeout = min(self.settings.llm_timeout_seconds, self.deadline.remaining())
            started = time.monotonic()
            try:
                async with asyncio.timeout(timeout):
                    result = await self.llm.generate(request, schema)
            except TimeoutError as exc:
                raise ProviderError(
                    "The model call timed out.",
                    provider=self.llm.provider,
                    code="LLM_TIMEOUT",
                    retryable=True,
                ) from exc
            except ProviderError as exc:
                self._record_usage(task, exc.usage)
                raise
            self._record_usage(task, result.usage)
            log.info(
                "llm.call",
                run_id=str(self.run_id),
                task_id=str(task.id),
                agent_role=task.role.value,
                provider=self.llm.provider,
                model=self.llm.model,
                duration_ms=int((time.monotonic() - started) * 1000),
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
            )
            return result.output

        repaired = False
        while True:
            try:
                return await run_with_retries(
                    attempt,
                    policy=self.retry_policy,
                    deadline=self.deadline,
                    before_attempt=before_attempt,
                    on_retry=on_retry,
                )
            except LLMOutputError as exc:
                if repaired:
                    raise
                repaired = True
                log.warning(
                    "llm.invalid_output",
                    run_id=str(self.run_id),
                    task_id=str(task.id),
                    agent_role=task.role.value,
                    error_code=exc.code,
                    problems=exc.problems,
                )
                await self.emit(
                    "retry",
                    "Model output failed validation; requesting one corrected response.",
                    level="warning",
                    task_id=task.id,
                    payload={"error_code": exc.code, "problems": exc.problems},
                )
                request = LLMRequest(
                    role=request.role,
                    task_key=request.task_key,
                    system=request.system,
                    prompt=(
                        f"{prompt}\n\nYour previous response was rejected by validation "
                        f"({'; '.join(exc.problems)}). Return only JSON that matches the "
                        "schema, respecting every length and item limit."
                    ),
                    max_tokens=request.max_tokens,
                )

    # --- Persistence of counters -------------------------------------------------------

    def estimated_cost(self) -> Decimal | None:
        """Only when explicit pricing is configured; otherwise unknown (None)."""
        price_in = self.settings.llm_input_usd_per_mtok
        price_out = self.settings.llm_output_usd_per_mtok
        if self.execution_mode == "demo":
            return Decimal("0")
        if price_in is None or price_out is None:
            return None
        cost = (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000
        return Decimal(str(round(cost, 6)))

    async def persist_counters(self, *, heartbeat: bool = False) -> None:
        values: dict[str, Any] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "llm_call_count": self.llm_calls,
            "tool_call_count": self.tool_calls,
            "estimated_cost_usd": self.estimated_cost(),
        }
        if heartbeat:
            values["heartbeat_at"] = utcnow()
        async with self.db.session() as session:
            await session.execute(
                update(ResearchRun).where(ResearchRun.id == self.run_id).values(**values)
            )
