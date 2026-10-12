"""Typed contracts for every structured LLM output.

Model output is untrusted: it is parsed and validated against these models
before anything is persisted or routed. ``extra="forbid"`` rejects unexpected
fields; length limits bound what can flow into prompts, the database and the
UI. References use short run-local handles (``S3`` for sources, ``F7`` for
findings) that code maps back to database IDs, so the model never handles
UUIDs and cannot reference rows from other runs.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ResearcherRole = Literal["data_researcher", "market_researcher"]
SourceRef = Annotated[str, Field(pattern=r"^S\d{1,3}$", description="Source handle such as S3")]
FindingRef = Annotated[str, Field(pattern=r"^F\d{1,3}$", description="Finding handle such as F7")]
Query = Annotated[str, Field(min_length=3, max_length=200)]
ShortText = Annotated[str, Field(max_length=300)]


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Research Director -------------------------------------------------------


class ResearchPlan(AgentOutput):
    """Bounded plan produced by the research director."""

    objective: str = Field(max_length=600, description="One-paragraph research objective.")
    sub_questions: list[ShortText] = Field(min_length=1, max_length=5)
    data_focus: str = Field(
        max_length=500, description="What the data researcher should establish (facts, stats)."
    )
    context_focus: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "What the market researcher should study (alternatives, competitors, trends, or "
            "practical context for non-commercial questions). Null to skip that track."
        ),
    )
    is_commercial: bool = Field(description="True when the question concerns products/markets.")
    out_of_scope: list[ShortText] = Field(default_factory=list, max_length=5)


class DirectorHandoff(AgentOutput):
    """Final handoff note; restates the report, adds no new facts."""

    handoff_summary: str = Field(max_length=900)
    confidence: Literal["low", "medium", "high"]
    caveats: list[ShortText] = Field(default_factory=list, max_length=4)


# --- Research Team Lead -------------------------------------------------------


class ResearchAssignment(AgentOutput):
    role: ResearcherRole
    objective: str = Field(max_length=500)
    search_queries: list[Query] = Field(min_length=1, max_length=3)
    focus_points: list[ShortText] = Field(default_factory=list, max_length=5)


class AssignmentSet(AgentOutput):
    assignments: list[ResearchAssignment] = Field(min_length=1, max_length=2)
    lead_note: str = Field(default="", max_length=500)


class FindingAssessment(AgentOutput):
    finding_ref: FindingRef
    verdict: Literal["supported", "weak", "unsupported", "conflicting"]
    note: str = Field(default="", max_length=300)


class FollowUp(AgentOutput):
    role: ResearcherRole
    objective: str = Field(max_length=400)
    search_queries: list[Query] = Field(min_length=1, max_length=2)


class EvidenceReview(AgentOutput):
    """Research lead's assessment of coverage and claim support."""

    assessments: list[FindingAssessment] = Field(default_factory=list, max_length=60)
    contradictions: list[ShortText] = Field(default_factory=list, max_length=6)
    missing_information: list[ShortText] = Field(default_factory=list, max_length=6)
    uncovered_sub_questions: list[ShortText] = Field(default_factory=list, max_length=5)
    decision: Literal["sufficient", "needs_more_research", "insufficient_evidence"]
    follow_ups: list[FollowUp] = Field(default_factory=list, max_length=2)
    summary: str = Field(max_length=800)


# --- Researchers ---------------------------------------------------------------


class ExtractedFinding(AgentOutput):
    source_ref: SourceRef
    claim: str = Field(min_length=10, max_length=600)
    evidence_excerpt: str = Field(
        min_length=10,
        max_length=700,
        description="Verbatim quote copied from the source text that supports the claim.",
    )
    support_level: Literal["supported", "tentative", "conflicting"]
    is_estimate: bool = Field(description="True for projections, models or estimates.")


class ResearcherOutput(AgentOutput):
    findings: list[ExtractedFinding] = Field(default_factory=list, max_length=12)
    coverage_note: str = Field(default="", max_length=600)
    gaps: list[ShortText] = Field(default_factory=list, max_length=5)


# --- Writing Team Lead ---------------------------------------------------------


class OutlineSection(AgentOutput):
    heading: str = Field(max_length=120)
    purpose: str = Field(max_length=300)
    finding_refs: list[FindingRef] = Field(default_factory=list, max_length=20)


class ReportOutline(AgentOutput):
    title: str = Field(max_length=160)
    direct_answer_plan: str = Field(max_length=500)
    sections: list[OutlineSection] = Field(min_length=1, max_length=6)
    key_caveats: list[ShortText] = Field(default_factory=list, max_length=5)


class DraftReview(AgentOutput):
    approved: bool
    issues: list[ShortText] = Field(default_factory=list, max_length=8)
    revision_instructions: str = Field(default="", max_length=1200)


# --- Writers --------------------------------------------------------------------


class KeyFinding(AgentOutput):
    statement: str = Field(
        max_length=700, description="One finding with citation markers such as [1] or [2][3]."
    )


class ReportDraft(AgentOutput):
    title: str = Field(max_length=160)
    direct_answer: str = Field(max_length=2000)
    key_findings: list[KeyFinding] = Field(min_length=1, max_length=8)
    detailed_analysis: str = Field(
        max_length=14000, description="Markdown; use ### subheadings; cite with [n] markers."
    )
    limitations: list[Annotated[str, Field(max_length=400)]] = Field(
        default_factory=list, max_length=8
    )
    unresolved_questions: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=6
    )


class ExecutiveSummary(AgentOutput):
    summary: str = Field(max_length=1800)
