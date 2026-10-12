"""SQLAlchemy ORM models for the ``research_hive`` schema.

The tables mirror the documented schema exactly (see docs/database.md).
Cross-run references are prevented relationally where practical: findings,
agent-task parents and run events use composite foreign keys that include
``run_id``, so a row can only point at a source or task from the *same* run.
Report citations have no ``run_id`` column, so their run ownership is checked
in the service layer before insert.

The migration in ``migrations/versions`` is the source of truth for DDL; these
models must be kept in sync with it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnClause, ColumnElement

SCHEMA = "research_hive"

RUN_STATUSES = ("queued", "running", "completed", "partial", "failed", "cancelled", "interrupted")
TERMINAL_RUN_STATUSES = frozenset({"completed", "partial", "failed", "cancelled", "interrupted"})
ACTIVE_RUN_STATUSES = frozenset({"queued", "running"})
TASK_STATUSES = ("running", "completed", "failed", "skipped", "cancelled", "interrupted")
AGENT_ROLES = (
    "research_director",
    "research_team_lead",
    "data_researcher",
    "market_researcher",
    "writing_team_lead",
    "technical_writer",
    "summary_writer",
)
CONTENT_KINDS = ("fetched_page", "search_snippet", "demo")
SUPPORT_LEVELS = ("supported", "tentative", "conflicting")
EVENT_LEVELS = ("debug", "info", "warning", "error")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 120", name="ck_projects_name_length"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchRun(Base):
    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="ck_research_runs_status"),
        CheckConstraint(_in("depth", ("quick", "standard")), name="ck_research_runs_depth"),
        CheckConstraint(
            _in("execution_mode", ("live", "demo")), name="ck_research_runs_execution_mode"
        ),
        Index("ix_research_runs_status_created_at", "status", "created_at"),
        Index("ix_research_runs_project_id_created_at", "project_id", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.projects.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str | None] = mapped_column(Text)
    input_urls: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    depth: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    execution_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    settings_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        CheckConstraint(_in("status", TASK_STATUSES), name="ck_agent_tasks_status"),
        CheckConstraint(_in("agent_role", AGENT_ROLES), name="ck_agent_tasks_agent_role"),
        CheckConstraint("attempt >= 1", name="ck_agent_tasks_attempt_positive"),
        UniqueConstraint("run_id", "task_key", "attempt", name="uq_agent_tasks_run_task_attempt"),
        # Target for composite foreign keys that pin references to one run.
        UniqueConstraint("id", "run_id", name="uq_agent_tasks_id_run"),
        ForeignKeyConstraint(
            ["parent_task_id", "run_id"],
            [f"{SCHEMA}.agent_tasks.id", f"{SCHEMA}.agent_tasks.run_id"],
            name="fk_agent_tasks_parent_same_run",
        ),
        Index("ix_agent_tasks_run_id", "run_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    agent_role: Mapped[str] = mapped_column(String(32), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(_in("content_kind", CONTENT_KINDS), name="ck_sources_content_kind"),
        UniqueConstraint("run_id", "normalized_url_hash", name="uq_sources_run_url_hash"),
        UniqueConstraint("id", "run_id", name="uq_sources_id_run"),
        Index("ix_sources_run_id", "run_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    publisher: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # ``metadata`` is reserved on declarative classes, hence the attribute name.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(_in("support_level", SUPPORT_LEVELS), name="ck_findings_support_level"),
        ForeignKeyConstraint(
            ["source_id", "run_id"],
            [f"{SCHEMA}.sources.id", f"{SCHEMA}.sources.run_id"],
            name="fk_findings_source_same_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["task_id", "run_id"],
            [f"{SCHEMA}.agent_tasks.id", f"{SCHEMA}.agent_tasks.run_id"],
            name="fk_findings_task_same_run",
            ondelete="CASCADE",
        ),
        Index("ix_findings_run_id", "run_id"),
        Index("ix_findings_source_id", "source_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    support_level: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = ({"schema": SCHEMA},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.research_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    executive_summary: Mapped[str] = mapped_column(Text, nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    is_partial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReportCitation(Base):
    __tablename__ = "report_citations"
    __table_args__ = (
        UniqueConstraint("report_id", "citation_number", name="uq_report_citations_number"),
        UniqueConstraint("report_id", "source_id", name="uq_report_citations_source"),
        CheckConstraint("citation_number >= 1", name="ck_report_citations_number_positive"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.reports.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.sources.id", ondelete="CASCADE"), nullable=False
    )
    citation_number: Mapped[int] = mapped_column(Integer, nullable=False)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        CheckConstraint(_in("level", EVENT_LEVELS), name="ck_run_events_level"),
        ForeignKeyConstraint(
            ["task_id", "run_id"],
            [f"{SCHEMA}.agent_tasks.id", f"{SCHEMA}.agent_tasks.run_id"],
            name="fk_run_events_task_same_run",
            ondelete="CASCADE",
        ),
        Index("ix_run_events_run_id_id", "run_id", "id"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.research_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Full-text search expressions.
#
# These must match the expression indexes created by the migration exactly
# (literal constants, explicit regconfig) so PostgreSQL can use the GIN
# indexes. String constants are emitted as literals, not bind parameters.
# ---------------------------------------------------------------------------

_ENGLISH: ColumnClause[Any] = literal_column("'english'::regconfig")
_EMPTY: ColumnClause[Any] = literal_column("''")
_SPACE: ColumnClause[Any] = literal_column("' '")


def source_search_vector() -> ColumnElement[Any]:
    text = (
        func.coalesce(Source.title, _EMPTY)
        .op("||")(_SPACE)
        .op("||")(func.coalesce(Source.content_text, _EMPTY))
    )
    return func.to_tsvector(_ENGLISH, text)


def report_search_vector() -> ColumnElement[Any]:
    text = (
        func.coalesce(Report.title, _EMPTY)
        .op("||")(_SPACE)
        .op("||")(func.coalesce(Report.executive_summary, _EMPTY))
        .op("||")(_SPACE)
        .op("||")(func.coalesce(Report.body_markdown, _EMPTY))
    )
    return func.to_tsvector(_ENGLISH, text)


def web_search_query(query: str) -> ColumnElement[Any]:
    """Parse user search text safely (bound parameter, websearch syntax)."""
    return func.websearch_to_tsquery(_ENGLISH, query)
