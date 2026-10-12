"""The only tools agents can use, with permissions enforced in code.

* ``web_search`` and ``fetch_page``: researchers only.
* ``run_evidence``: any role, but strictly scoped to the current run.

There is no shell, eval, filesystem, SQL or arbitrary-HTTP tool. Arguments are
validated with strict Pydantic schemas before anything is executed, and every
call checks cancellation, the deadline and the shared budget first.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.roles import Role, Tool, can_use
from app.core.errors import AppError, ProviderError, ToolPermissionError, UnsafeUrlError
from app.core.logging import strip_url_query
from app.core.retry import run_with_retries
from app.services.evidence import FindingView
from app.tools.fetch import FetchedPage
from app.tools.search import SearchHit
from app.tools.url_safety import validate_public_url

if TYPE_CHECKING:
    from app.agents.context import RunContext, TaskHandle

log = structlog.get_logger(__name__)


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=3, max_length=300)
    max_results: int = Field(ge=1, le=8)


class FetchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=10, max_length=2048)

    @field_validator("url")
    @classmethod
    def _public(cls, value: str) -> str:
        # Raises UnsafeUrlError (never retried) for non-public destinations.
        return validate_public_url(value).url


class RunTools:
    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx

    def _authorize(self, role: Role, tool: Tool) -> None:
        if not can_use(role, tool):
            log.warning(
                "security.tool_denied", run_id=str(self._ctx.run_id), agent_role=role.value,
                tool=tool.value,
            )  # fmt: skip
            raise ToolPermissionError(f"{role.value} may not use {tool.value}.")

    async def _call[T](
        self, task: TaskHandle, resource: str, operation: Any, limit_seconds: float
    ) -> T:
        ctx = self._ctx

        async def before_attempt(_: int) -> None:
            await ctx.ensure_active()
            await ctx.budget.reserve(resource)  # type: ignore[arg-type]
            ctx.tool_calls += 1

        async def attempt(_: int) -> T:
            try:
                async with asyncio.timeout(min(limit_seconds, ctx.deadline.remaining())):
                    result: T = await operation()
                    return result
            except TimeoutError as exc:
                raise ProviderError(
                    "The tool call timed out.", provider=resource, code="TOOL_TIMEOUT",
                    retryable=True,
                ) from exc  # fmt: skip

        async def on_retry(attempt_no: int, exc: AppError, delay: float) -> None:
            log.warning(
                "tool.retry", run_id=str(ctx.run_id), task_id=str(task.id), tool=resource,
                retry_attempt=attempt_no, delay_s=round(delay, 2), error_code=exc.code,
            )  # fmt: skip
            await ctx.emit(
                "retry",
                f"Retrying {resource} after a transient error ({exc.code}).",
                level="warning",
                task_id=task.id,
                payload={"tool": resource, "attempt": attempt_no, "error_code": exc.code},
            )

        return await run_with_retries(
            attempt,
            policy=ctx.retry_policy,
            deadline=ctx.deadline,
            before_attempt=before_attempt,
            on_retry=on_retry,
        )

    async def web_search(
        self, role: Role, task: TaskHandle, query: str, max_results: int = 5
    ) -> list[SearchHit]:
        self._authorize(role, Tool.WEB_SEARCH)
        args = SearchArgs(query=query.strip(), max_results=max_results)
        hits: list[SearchHit] = await self._call(
            task,
            "search",
            lambda: self._ctx.search.search(args.query, max_results=args.max_results),
            self._ctx.settings.search_timeout_seconds,
        )
        await self._ctx.emit(
            "tool_called",
            f"Searched the web for “{args.query}” ({len(hits)} results).",
            task_id=task.id,
            payload={"tool": "web_search", "query": args.query, "results": len(hits)},
        )
        return hits

    async def fetch_page(self, role: Role, task: TaskHandle, url: str) -> FetchedPage:
        self._authorize(role, Tool.FETCH_PAGE)
        try:
            args = FetchArgs(url=url)
        except UnsafeUrlError:
            log.warning(
                "security.url_rejected", run_id=str(self._ctx.run_id), url=strip_url_query(url)
            )
            raise
        try:
            page: FetchedPage = await self._call(
                task,
                "fetch",
                lambda: self._ctx.fetcher.fetch(args.url),
                self._ctx.settings.fetch_timeout_seconds + 1,
            )
        except UnsafeUrlError as exc:
            log.warning(
                "security.url_rejected", run_id=str(self._ctx.run_id),
                url=strip_url_query(args.url), error_code=exc.code,
            )  # fmt: skip
            raise
        await self._ctx.emit(
            "tool_called",
            f"Read page: {page.title[:120]}",
            task_id=task.id,
            payload={"tool": "fetch_page", "url": strip_url_query(page.final_url)},
        )
        return page

    async def run_evidence(
        self, role: Role, ids: list[uuid.UUID] | None = None
    ) -> list[FindingView]:
        """Findings (with their sources) belonging to the current run only."""
        self._authorize(role, Tool.RUN_EVIDENCE)
        return await self._ctx.evidence.list_findings(self._ctx.run_id, ids)

    async def rank_run_sources(
        self, role: Role, query: str, limit: int = 20
    ) -> list[tuple[uuid.UUID, float]]:
        """Full-text relevance ranking over this run's stored sources."""
        self._authorize(role, Tool.RUN_EVIDENCE)
        return await self._ctx.evidence.rank_sources(self._ctx.run_id, query, limit)
