"""Shared per-run execution budgets.

One ``RunBudget`` instance is shared by every agent task in a run, including
tasks running in parallel. Capacity is reserved *before* an operation is
dispatched (``reserve``), under a lock, so concurrent tasks cannot overshoot a
limit. Retries call ``reserve`` again and therefore consume the same budget.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import structlog

from app.core.config import RunLimits
from app.core.errors import BudgetExceededError

Resource = Literal["llm", "search", "fetch", "sources"]

log = structlog.get_logger(__name__)


class RunBudget:
    def __init__(self, limits: RunLimits) -> None:
        self._limits: dict[Resource, int] = {
            "llm": limits.max_llm_calls,
            "search": limits.max_search_calls,
            "fetch": limits.max_fetch_calls,
            "sources": limits.max_sources,
        }
        self._used: dict[Resource, int] = dict.fromkeys(self._limits, 0)
        self._lock = asyncio.Lock()
        # Resources whose limit stopped some work; surfaced as partial-report reasons.
        self.exhausted: set[Resource] = set()

    async def reserve(self, resource: Resource) -> None:
        async with self._lock:
            if self._used[resource] >= self._limits[resource]:
                if resource not in self.exhausted:
                    # run_id is bound in the logging context by the worker.
                    log.warning("budget.exceeded", resource=resource, limit=self._limits[resource])
                self.exhausted.add(resource)
                raise BudgetExceededError(resource, self._limits[resource])
            self._used[resource] += 1

    async def release(self, resource: Resource) -> None:
        """Return an unused reservation (e.g. a source that turned out to be a duplicate)."""
        async with self._lock:
            self._used[resource] = max(0, self._used[resource] - 1)

    def remaining(self, resource: Resource) -> int:
        return self._limits[resource] - self._used[resource]

    def used(self, resource: Resource) -> int:
        return self._used[resource]

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {r: {"used": self._used[r], "limit": self._limits[r]} for r in self._limits}
