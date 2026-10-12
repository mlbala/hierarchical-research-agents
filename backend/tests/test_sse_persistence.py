"""Persisted SSE replay and database-level persistence guarantees."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import CitationValidationError
from app.db.engine import Database
from app.db.models import AgentTask, Finding, Report, ResearchRun, RunEvent, utcnow
from app.services.events import EventBroker, EventRecorder, list_events
from app.services.evidence import EvidenceStore, NewFinding
from app.services.reports import ReportContent, ReportService
from app.services.runs import RunService
from tests.conftest import queue_run

pytestmark = pytest.mark.db


def parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            fields[key] = value
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            events.append(fields)
    return events


async def finish_run(db: Database, run_id: uuid.UUID, status: str = "completed") -> None:
    async with db.session() as session:
        await session.execute(
            update(ResearchRun)
            .where(ResearchRun.id == run_id)
            .values(status=status, finished_at=utcnow())
        )


async def test_sse_replays_persisted_events_in_order_from_last_event_id(
    client: httpx.AsyncClient, db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id)
    recorder = EventRecorder(db, EventBroker())
    ids = [
        await recorder.emit(run.id, "agent_started", f"step {i}", payload={"i": i})
        for i in range(5)
    ]
    await finish_run(db, run.id)

    full = parse_sse((await client.get(f"/api/v1/runs/{run.id}/events/stream")).text)
    replayed = [e for e in full if e["event"] == "run_event"]
    assert [int(e["id"]) for e in replayed] == sorted(int(e["id"]) for e in replayed)
    assert full[-1]["event"] == "end" and full[-1]["data"]["status"] == "completed"

    resumed = parse_sse(
        (
            await client.get(
                f"/api/v1/runs/{run.id}/events/stream", headers={"Last-Event-ID": str(ids[2])}
            )
        ).text
    )
    resumed_ids = [int(e["id"]) for e in resumed if e["event"] == "run_event"]
    assert resumed_ids == ids[3:]
    assert [e["data"]["message"] for e in resumed if e["event"] == "run_event"] == [
        "step 3",
        "step 4",
    ]

    by_query = parse_sse(
        (await client.get(f"/api/v1/runs/{run.id}/events/stream", params={"after": ids[3]})).text
    )
    assert [int(e["id"]) for e in by_query if e["event"] == "run_event"] == ids[4:]

    # Malformed Last-Event-ID values are ignored rather than trusted.
    garbage = await client.get(
        f"/api/v1/runs/{run.id}/events/stream", headers={"Last-Event-ID": "1; DROP TABLE"}
    )
    assert garbage.status_code == 200


async def test_sse_stream_wakes_on_new_events_and_ends_at_terminal_status(
    db: Database, run_service: RunService, project: Any
) -> None:
    """Drive the streaming generator directly to observe live delivery."""
    from app.api.deps import AppServices
    from app.api.runs import stream_events
    from app.core.security import RateLimiter

    broker = EventBroker()
    services = AppServices(
        settings=db.settings, db=db, broker=broker, worker=None, run_rate_limiter=RateLimiter(5)
    )
    run = await queue_run(run_service, project.id)

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    response = await stream_events(run.id, FakeRequest(), services, None, 0)  # type: ignore[arg-type]
    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    recorder = EventRecorder(db, broker)
    await recorder.emit(run.id, "stage_changed", "Planning")
    await asyncio.sleep(0.2)
    assert any("Planning" in c for c in chunks)  # delivered without waiting for a poll
    await finish_run(db, run.id, "cancelled")
    await recorder.emit(run.id, "run_cancelled", "Research cancelled.")
    await asyncio.wait_for(consumer, timeout=5)
    events = parse_sse("".join(chunks).replace("retry: 3000\n\n", ""))
    assert events[-1]["event"] == "end" and events[-1]["data"]["status"] == "cancelled"
    assert broker.subscriber_count(run.id) == 0  # subscription cleaned up


async def test_event_payloads_are_sanitised_before_persisting(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id)
    await EventRecorder(db, EventBroker()).emit(
        run.id,
        "tool_called",
        "Read page",
        payload={"url": "https://site.example/p?session=abc", "api_key": "sk-ant-api03-XXXXXXXXXX"},
    )
    events = await list_events(db, run.id)
    payload = events[-1].payload
    assert payload["url"] == "https://site.example/p"
    assert payload["api_key"] == "[REDACTED]"


async def test_cross_run_source_references_are_rejected(
    db: Database, run_service: RunService, project: Any
) -> None:
    store = EvidenceStore(db)
    run_a = await queue_run(run_service, project.id)
    run_b = await queue_run(run_service, project.id)
    source_b = await store.upsert_source(
        run_b.id,
        url="https://example.org/b",
        title="B",
        publisher=None,
        published_at=None,
        content_text="text",
        content_hash="h",
        content_kind="fetched_page",
        metadata={},
    )
    task_a = uuid.uuid4()
    async with db.session() as session:
        session.add(
            AgentTask(
                id=task_a,
                run_id=run_a.id,
                agent_role="data_researcher",
                task_key="data_researcher.research",
                attempt=1,
                status="running",
                started_at=utcnow(),
            )
        )

    # Service layer refuses findings that cite another run's source...
    with pytest.raises(CitationValidationError):
        await store.insert_findings(
            run_a.id, task_a, [NewFinding(source_b.id, "claim", "excerpt", "supported")]
        )
    # ...and the composite foreign key refuses it even if the service is bypassed.
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            session.add(
                Finding(
                    id=uuid.uuid4(),
                    run_id=run_a.id,
                    task_id=task_a,
                    source_id=source_b.id,
                    claim="c",
                    evidence_excerpt="e",
                    support_level="supported",
                    created_at=utcnow(),
                )
            )

    # Report citations are validated against the run before they are written.
    content = ReportContent(
        title="T",
        executive_summary="S",
        direct_answer="A [1]",
        key_findings=["K [1]"],
        detailed_analysis="D [1]",
        limitations=[],
        is_partial=False,
        partial_reasons=[],
        execution_mode="demo",
    )
    with pytest.raises(CitationValidationError):
        await ReportService(db).save(run_a.id, content, [(1, source_b.id)])
    async with db.session() as session:
        assert await session.scalar(select(Report.id).where(Report.run_id == run_a.id)) is None


async def test_sources_are_deduplicated_within_a_run(
    db: Database, run_service: RunService, project: Any
) -> None:
    store = EvidenceStore(db)
    run = await queue_run(run_service, project.id)
    common = {"publisher": None, "published_at": None, "metadata": {}}
    snippet = await store.upsert_source(
        run.id,
        url="https://example.org/page?utm_source=x",
        title="Snippet",
        content_text="short snippet",
        content_hash="a",
        content_kind="search_snippet",
        **common,
    )
    fetched = await store.upsert_source(
        run.id,
        url="https://EXAMPLE.org/page",
        title="Full page",
        content_text="the whole page text",
        content_hash="b",
        content_kind="fetched_page",
        **common,
    )
    assert snippet.created and not fetched.created and fetched.upgraded
    assert snippet.id == fetched.id
    [row] = await store.get_sources(run.id, [snippet.id])
    assert row.content_kind == "fetched_page" and row.content_text == "the whole page text"


async def test_database_constraints_enforce_states_and_unique_attempts(
    db: Database, run_service: RunService, project: Any
) -> None:
    run = await queue_run(run_service, project.id)
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            await session.execute(
                update(ResearchRun).where(ResearchRun.id == run.id).values(status="exploded")
            )

    def task() -> AgentTask:
        return AgentTask(
            id=uuid.uuid4(),
            run_id=run.id,
            agent_role="technical_writer",
            task_key="technical_writer.draft",
            attempt=1,
            status="completed",
            started_at=utcnow(),
        )

    async with db.session() as session:
        session.add(task())
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            session.add(task())
    with pytest.raises(IntegrityError):
        async with db.session() as session:
            bad = task()
            bad.attempt, bad.agent_role = 2, "ceo"
            session.add(bad)


async def test_saved_work_survives_a_new_process(
    migrated_database: Any, db: Database, run_service: RunService, project: Any
) -> None:
    """A fresh engine (as after an application restart) sees everything."""
    run = await queue_run(run_service, project.id)
    await EventRecorder(db, EventBroker()).emit(run.id, "stage_changed", "Planning")
    fresh = Database(migrated_database)
    try:
        async with fresh.session() as session:
            reloaded = await session.get(ResearchRun, run.id)
            events = list(await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id)))
        assert reloaded is not None and reloaded.question == run.question
        assert reloaded.settings_snapshot["execution_mode"] == "demo"
        assert [e.message for e in events][-1] == "Planning"
    finally:
        await fresh.dispose()
