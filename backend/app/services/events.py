"""User-visible run events.

Events are persisted to ``run_events`` first and only then announced to live
SSE subscribers, so a browser that reconnects can always replay everything it
missed from the database using ``Last-Event-ID``. Payloads are sanitised with
the same redaction rules as the console logs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from sqlalchemy import select

from app.core.logging import sanitize_payload
from app.db.engine import Database
from app.db.models import RunEvent, utcnow

Level = Literal["debug", "info", "warning", "error"]


class EventBroker:
    """In-process wake-up signal for SSE streams (the data comes from the DB)."""

    def __init__(self) -> None:
        self._waiters: dict[uuid.UUID, set[asyncio.Event]] = defaultdict(set)

    @contextmanager
    def subscribe(self, run_id: uuid.UUID) -> Iterator[asyncio.Event]:
        event = asyncio.Event()
        self._waiters[run_id].add(event)
        try:
            yield event
        finally:
            waiters = self._waiters.get(run_id)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    self._waiters.pop(run_id, None)

    def notify(self, run_id: uuid.UUID) -> None:
        for event in self._waiters.get(run_id, ()):
            event.set()

    def subscriber_count(self, run_id: uuid.UUID) -> int:
        return len(self._waiters.get(run_id, ()))


class EventRecorder:
    def __init__(self, db: Database, broker: EventBroker) -> None:
        self._db = db
        self._broker = broker

    async def emit(
        self,
        run_id: uuid.UUID,
        event_type: str,
        message: str,
        *,
        level: Level = "info",
        task_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Persist one event (own short transaction), then wake subscribers."""
        async with self._db.session() as session:
            event = RunEvent(
                run_id=run_id,
                task_id=task_id,
                event_type=event_type[:48],
                level=level,
                message=message[:1000],
                payload=sanitize_payload(payload),
                created_at=utcnow(),
            )
            session.add(event)
            await session.flush()
            event_id = event.id
        self._broker.notify(run_id)
        return event_id


async def list_events(
    db: Database, run_id: uuid.UUID, *, after_id: int = 0, limit: int = 200
) -> list[RunEvent]:
    """Events strictly after ``after_id`` in persisted (id) order."""
    async with db.session() as session:
        rows = await session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.id > after_id)
            .order_by(RunEvent.id)
            .limit(limit)
        )
        return list(rows)
