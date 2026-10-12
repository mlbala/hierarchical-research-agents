"""Worker execution: hierarchy, bounded loops, budgets, failures, cancellation,
single-worker locking and interrupted-run recovery (demo/fake providers only).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select, update

from app.agents.demo_llm import DemoLLM
from app.agents.llm import LLMOutputError, LLMRequest, LLMResult, LLMUsage
from app.agents.roles import ROLE_PARENT, Role
from app.core.config import Settings
from app.core.errors import ProviderError
from app.db.engine import Database
from app.db.models import AgentTask, Report, ResearchRun, RunEvent, Source, utcnow
from app.schemas.agents import DraftReview, EvidenceReview, FollowUp, ResearchPlan
from app.services.events import EventBroker
from app.services.runs import RunService
from app.services.worker import Providers, ResearchWorker
from app.tools.demo import DemoPageFetcher, DemoSearch
from app.tools.search import SearchHit
from tests.conftest import queue_run

pytestmark = pytest.mark.db


def providers_with(llm: Any = None, search: Any = None) -> Any:
    def factory(settings: Settings, mode: str) -> Providers:
        return (llm or DemoLLM(), search or DemoSearch(), DemoPageFetcher())

    return factory


async def execute(
    db: Database, run_id: uuid.UUID, factory: Any = None, settings: Settings | None = None
) -> ResearchRun:
    worker = ResearchWorker(
        settings or db.settings, db, EventBroker(), provider_factory=factory or providers_with()
    )
    claimed = await worker.claim_next()
    assert claimed == run_id
    await worker.execute(run_id, asyncio.Event())
    async with db.session() as session:
        run = await session.get(ResearchRun, run_id)
    assert run is not None
    return run


async def tasks_of(db: Database, run_id: uuid.UUID) -> list[AgentTask]:
    async with db.session() as session:
        return list(
            await session.scalars(
                select(AgentTask).where(AgentTask.run_id == run_id).order_by(AgentTask.started_at)
            )
        )


async def test_demo_run_respects_the_reporting_hierarchy(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id)
    finished = await execute(db, run.id)
    assert finished.status == "completed"
    assert finished.llm_call_count == 14 and finished.tool_call_count > 0
    tasks = await tasks_of(db, run.id)
    by_id = {t.id: t for t in tasks}
    assert {t.agent_role for t in tasks} == {r.value for r in Role}
    for task in tasks:
        assert task.status == "completed"
        expected_parent = ROLE_PARENT[Role(task.agent_role)]
        if expected_parent is None:
            assert task.parent_task_id is None
        else:
            assert by_id[task.parent_task_id].agent_role == expected_parent.value  # type: ignore[index]
    attempts = {(t.task_key, t.attempt) for t in tasks}
    assert ("data_researcher.research", 2) in attempts  # one extra research pass
    assert ("technical_writer.draft", 2) in attempts  # one revision
    async with db.session() as session:
        assert await session.scalar(select(Report.id).where(Report.run_id == run.id))


class GreedyLLM(DemoLLM):
    """Always asks for more research and never approves a draft."""

    async def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        result = await super().generate(request, schema)
        output: Any = result.output
        if isinstance(output, EvidenceReview):
            output = output.model_copy(
                update={
                    "decision": "needs_more_research",
                    "follow_ups": [
                        FollowUp(
                            role="data_researcher",
                            objective="More, forever.",
                            search_queries=["voucher recipients long-term follow-up survey"],
                        )
                    ],
                    "missing_information": ["Everything else."],
                }
            )
        if isinstance(output, DraftReview):
            output = DraftReview(approved=False, issues=["Never good enough."])
        return LLMResult(output=output, usage=result.usage)


async def test_review_loops_are_bounded_even_when_agents_keep_asking(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id)
    finished = await execute(db, run.id, providers_with(llm=GreedyLLM()))
    assert finished.status == "partial"
    tasks = await tasks_of(db, run.id)
    count = lambda key: sum(1 for t in tasks if t.task_key == key)  # noqa: E731
    assert count("research_team_lead.review") == 2  # initial + one retry pass, no more
    assert count("data_researcher.research") == 2
    assert count("technical_writer.draft") == 2  # draft + one revision, no more
    assert count("writing_team_lead.review") == 1
    async with db.session() as session:
        report = await session.scalar(select(Report).where(Report.run_id == run.id))
    assert report is not None and report.is_partial
    # The lead still wanted more research after the only permitted retry.
    assert any("judged the evidence incomplete" in item for item in report.limitations)


async def test_llm_budget_bounds_the_whole_run(
    migrated_database: Settings, db: Database, run_service: RunService, project: Any
) -> None:
    tight = migrated_database.model_copy(update={"max_llm_calls_per_run": 10})
    run = await queue_run(run_service, project.id)
    finished = await execute(db, run.id, settings=tight)
    # Not enough budget for the retry pass or the revision: both are skipped
    # in code, and the run finishes within exactly ten model calls.
    assert finished.llm_call_count == 10
    assert finished.status == "partial"
    keys = [t.task_key for t in await tasks_of(db, run.id)]
    assert keys.count("research_team_lead.review") == 1
    assert keys.count("technical_writer.draft") == 1


class FlakyLLM(DemoLLM):
    """Fails in configurable ways for the director's plan."""

    def __init__(self, failures: list[Exception]) -> None:
        super().__init__()
        self.failures = failures
        self.plan_calls = 0

    async def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        if schema is ResearchPlan:
            self.plan_calls += 1
            if self.failures:
                raise self.failures.pop(0)
        return await super().generate(request, schema)


def malformed() -> LLMOutputError:
    return LLMOutputError(
        "bad", code="LLM_INVALID_OUTPUT", usage=LLMUsage(7, 3), problems=["objective: missing"]
    )


async def test_malformed_output_gets_one_corrective_retry(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id, depth="quick")
    llm = FlakyLLM([malformed()])
    finished = await execute(db, run.id, providers_with(llm=llm))
    assert finished.status == "completed"
    assert llm.plan_calls == 2
    assert finished.llm_call_count == 11  # the repair attempt consumed budget
    assert finished.input_tokens == 7  # usage of the rejected attempt was recorded


async def test_repeated_malformed_output_fails_the_run_clearly(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id, depth="quick")
    finished = await execute(db, run.id, providers_with(llm=FlakyLLM([malformed(), malformed()])))
    assert finished.status == "failed"
    assert finished.error_code == "LLM_INVALID_OUTPUT"
    plan = [t for t in await tasks_of(db, run.id) if t.task_key == "research_director.plan"]
    assert plan[0].status == "failed" and plan[0].error_code == "LLM_INVALID_OUTPUT"


async def test_transient_provider_errors_are_retried(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id, depth="quick")
    busy = ProviderError("busy", provider="llm", code="LLM_UNAVAILABLE", retryable=True)
    llm = FlakyLLM([busy])
    finished = await execute(db, run.id, providers_with(llm=llm))
    assert finished.status == "completed" and llm.plan_calls == 2
    async with db.session() as session:
        events = list(
            await session.scalars(select(RunEvent.event_type).where(RunEvent.run_id == run.id))
        )
    assert "retry" in events


async def test_invalid_credentials_fail_fast_without_retry(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id, depth="quick")
    denied = ProviderError("denied", provider="llm", code="LLM_AUTH_FAILED")
    llm = FlakyLLM([denied, denied, denied])
    finished = await execute(db, run.id, providers_with(llm=llm))
    assert finished.status == "failed" and finished.error_code == "LLM_AUTH_FAILED"
    assert llm.plan_calls == 1
    assert finished.error_message == "denied"


class EmptySearch(DemoSearch):
    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        return []


async def test_no_usable_evidence_fails_instead_of_writing_from_memory(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id, depth="quick")
    finished = await execute(db, run.id, providers_with(search=EmptySearch()))
    assert finished.status == "failed" and finished.error_code == "NO_USABLE_EVIDENCE"
    keys = {t.task_key for t in await tasks_of(db, run.id)}
    assert "technical_writer.draft" not in keys
    async with db.session() as session:
        assert await session.scalar(select(Report.id).where(Report.run_id == run.id)) is None


class MarketSearchDown(DemoSearch):
    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        if any(word in query for word in ("price", "alternatives", "equity", "winter")):
            self.calls += 1
            raise ProviderError(
                "down", provider="search", code="SEARCH_UNAVAILABLE", retryable=True
            )
        return await super().search(query, max_results=max_results)


async def test_partial_evidence_produces_a_marked_partial_report(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id, depth="quick")
    search = MarketSearchDown()
    finished = await execute(db, run.id, providers_with(search=search))
    assert finished.status == "partial"
    # Quick depth allows 4 searches in total. The data researcher used 2, so the
    # market researcher's retries stop after 2 attempts: retries share the budget.
    assert search.calls == 2
    async with db.session() as session:
        report = await session.scalar(select(Report).where(Report.run_id == run.id))
    assert report is not None and report.is_partial
    assert any("Market Researcher found no usable sources" in x for x in report.limitations)
    assert any("search limit" in x for x in report.limitations)
    async with db.session() as session:
        retries = list(
            await session.scalars(
                select(RunEvent.payload).where(
                    RunEvent.run_id == run.id, RunEvent.event_type == "retry"
                )
            )
        )
    assert [r["error_code"] for r in retries] == ["SEARCH_UNAVAILABLE"] * 2


class SlowLLM(DemoLLM):
    def __init__(self) -> None:
        super().__init__(step_delay_ms=150)


async def test_cancellation_stops_work_and_keeps_evidence(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id)
    worker = ResearchWorker(
        db.settings, db, EventBroker(), provider_factory=providers_with(llm=SlowLLM())
    )
    await worker.claim_next()
    worker._spawn(run.id)

    async def sources() -> int:
        async with db.session() as session:
            return len(
                list(await session.scalars(select(Source.id).where(Source.run_id == run.id)))
            )

    for _ in range(200):
        if await sources() > 0:
            break
        await asyncio.sleep(0.05)
    await run_service.cancel(run.id)  # persists the request
    worker.request_cancel(run.id)
    for _ in range(100):
        if not worker.active_run_ids:
            break
        await asyncio.sleep(0.05)

    async with db.session() as session:
        finished = await session.get(ResearchRun, run.id)
    assert finished is not None and finished.status == "cancelled"
    assert finished.cancel_requested_at is not None and finished.finished_at is not None
    assert await sources() > 0  # evidence collected before cancelling is kept
    statuses = {t.status for t in await tasks_of(db, run.id)}
    assert "running" not in statuses
    async with db.session() as session:
        report = await session.scalar(select(Report.id).where(Report.run_id == run.id))
    assert report is None


async def test_only_one_worker_holds_the_execution_lock(db: Database) -> None:
    first = ResearchWorker(db.settings, db, EventBroker())
    second = ResearchWorker(db.settings, db, EventBroker())
    try:
        await first.tick()
        await second.tick()
        assert first.lock_held and not second.lock_held
        await first.stop()
        await second.tick()
        assert second.lock_held
    finally:
        await first.stop()
        await second.stop()


async def test_claims_are_atomic(db: Database, run_service: RunService, project: Any) -> None:
    run = await queue_run(run_service, project.id)
    workers = [ResearchWorker(db.settings, db, EventBroker()) for _ in range(5)]
    claims = await asyncio.gather(*(w.claim_next() for w in workers))
    assert [c for c in claims if c is not None] == [run.id]


async def test_restart_marks_orphaned_runs_interrupted(
    db: Database, run_service: RunService, project: Any
) -> None:
    orphan = await queue_run(run_service, project.id)
    cancelling = await queue_run(run_service, project.id)
    waiting = await queue_run(run_service, project.id)
    async with db.session() as session:
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id.in_([orphan.id, cancelling.id]))
            .values(status="running", started_at=utcnow())
        )
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == cancelling.id)
            .values(cancel_requested_at=utcnow())
        )
        session.add(
            AgentTask(
                id=uuid.uuid4(),
                run_id=orphan.id,
                agent_role="data_researcher",
                task_key="data_researcher.research",
                attempt=1,
                status="running",
                started_at=utcnow(),
            )
        )
    # A fresh worker (the "restarted process") acquires the lock and recovers.
    worker = ResearchWorker(db.settings, db, EventBroker(), poll_interval=60)
    try:
        await worker._acquire_lock()
        assert worker.lock_held
    finally:
        await worker.stop()
    async with db.session() as session:
        statuses = {
            r.id: r
            for r in await session.scalars(
                select(ResearchRun).where(ResearchRun.project_id == project.id)
            )
        }
        orphan_tasks = list(
            await session.scalars(select(AgentTask.status).where(AgentTask.run_id == orphan.id))
        )
    assert statuses[orphan.id].status == "interrupted"
    assert statuses[orphan.id].error_code == "WORKER_INTERRUPTED"
    assert statuses[cancelling.id].status == "cancelled"
    assert statuses[waiting.id].status == "queued"  # queued jobs are retained
    assert orphan_tasks == ["interrupted"]
    rerun, created = await run_service.rerun(orphan.id, f"rerun-{uuid.uuid4()}")
    assert created and rerun.status == "queued" and rerun.id != orphan.id
