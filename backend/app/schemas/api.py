"""Request and response models for the versioned REST API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import UnsafeUrlError
from app.tools.url_safety import validate_public_url

RunStatus = Literal[
    "queued", "running", "completed", "partial", "failed", "cancelled", "interrupted"
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


# --- Projects --------------------------------------------------------------------


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class ProjectOut(OutModel):
    id: uuid.UUID
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    run_count: int = 0
    last_run_at: datetime | None = None


# --- Runs ----------------------------------------------------------------------------


class RunCreate(StrictModel):
    question: str = Field(min_length=10, max_length=2000)
    context: str | None = Field(default=None, max_length=4000)
    input_urls: list[str] = Field(default_factory=list, max_length=5)
    depth: Literal["quick", "standard"] = "standard"

    @field_validator("context")
    @classmethod
    def _empty_context(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("input_urls")
    @classmethod
    def _public_urls(cls, urls: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in urls:
            url = raw.strip()
            if not url:
                continue
            try:
                normalized = validate_public_url(url).url
            except UnsafeUrlError as exc:
                raise ValueError(f"{exc.safe_message} ({url[:80]})") from exc
            if normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned


class RunSummary(OutModel):
    id: uuid.UUID
    project_id: uuid.UUID
    question: str
    depth: str
    status: RunStatus
    execution_mode: Literal["live", "demo"]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    is_partial: bool | None = None


class RunDetail(RunSummary):
    context: str | None
    input_urls: list[str]
    plan: dict[str, Any] | None
    warnings: list[dict[str, Any]]
    cancel_requested_at: datetime | None
    heartbeat_at: datetime | None
    input_tokens: int
    output_tokens: int
    llm_call_count: int
    tool_call_count: int
    estimated_cost_usd: str | None = Field(
        description="Decimal string; null means unknown (no pricing configured)."
    )
    error_message: str | None
    settings_snapshot: dict[str, Any]
    report_id: uuid.UUID | None = None
    handoff: dict[str, Any] | None = None
    source_count: int = 0
    finding_count: int = 0


class TaskOut(OutModel):
    id: uuid.UUID
    parent_task_id: uuid.UUID | None
    agent_role: str
    task_key: str
    attempt: int
    status: str
    input_summary: str
    output: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    input_tokens: int
    output_tokens: int
    error_code: str | None
    error_message: str | None


class FindingOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    source_id: uuid.UUID
    agent_role: str
    claim: str
    evidence_excerpt: str
    support_level: str
    review_status: Literal["pending", "approved", "rejected"]
    created_at: datetime


class SourceOut(BaseModel):
    id: uuid.UUID
    url: str
    title: str
    publisher: str | None
    published_at: datetime | None
    retrieved_at: datetime
    content_kind: Literal["fetched_page", "search_snippet", "demo"]
    content_hash: str
    content_length: int
    excerpt: str
    truncated: bool = False


class SourceDetail(SourceOut):
    content_text: str


class EventOut(OutModel):
    id: int
    task_id: uuid.UUID | None
    event_type: str
    level: str
    message: str
    payload: dict[str, Any]
    created_at: datetime


# --- Reports -------------------------------------------------------------------------


class CitationEvidence(BaseModel):
    claim: str
    excerpt: str
    support_level: str


class CitationOut(BaseModel):
    number: int
    source: SourceOut
    evidence: list[CitationEvidence]


class ReportOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    title: str
    executive_summary: str
    body_markdown: str
    limitations: list[str]
    is_partial: bool
    execution_mode: Literal["live", "demo"]
    created_at: datetime
    updated_at: datetime
    citations: list[CitationOut]


class ReportSearchHit(BaseModel):
    report_id: uuid.UUID
    run_id: uuid.UUID
    project_id: uuid.UUID
    title: str
    question: str
    snippet: str
    is_partial: bool
    execution_mode: Literal["live", "demo"]
    created_at: datetime


# --- System -----------------------------------------------------------------------------


class ConfigIssueOut(BaseModel):
    key: str
    severity: Literal["error", "warning"]
    message: str


class ConfigOut(BaseModel):
    version: str
    execution_mode: Literal["live", "demo"]
    live_ready: bool
    llm_provider: str
    llm_model: str | None
    search_provider: str
    issues: list[ConfigIssueOut]
    limits: dict[str, dict[str, int]]
    max_parallel_agent_tasks: int
    max_concurrent_runs: int
    cost_estimates_enabled: bool
