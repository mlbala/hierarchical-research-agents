"""Deterministic, offline stand-in for the LLM used in demo mode.

It never calls a provider. Outputs are derived from the prompts built by
``app.agents.prompts`` (source handles, finding lines, pass counters) and from
the fixture metadata in ``app.tools.demo``, so the whole pipeline -- excerpt
verification, citation numbering, review loops -- runs exactly as in live
mode, just with fictional data. The first research review and the first draft
review deliberately request one extra pass so the bounded loops are visible.
"""

from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel

from app.agents.llm import LLMRequest, LLMResult, LLMUsage
from app.schemas.agents import (
    AssignmentSet,
    DirectorHandoff,
    DraftReview,
    EvidenceReview,
    ExecutiveSummary,
    ExtractedFinding,
    FindingAssessment,
    FollowUp,
    KeyFinding,
    OutlineSection,
    ReportDraft,
    ReportOutline,
    ResearchAssignment,
    ResearcherOutput,
    ResearchPlan,
)
from app.tools.demo import DEMO_BY_URL, DEMO_TOPIC

_QUESTION = re.compile(r"<question>\n(.*?)\n</question>", re.S)
_SOURCE = re.compile(r'<source id="(S\d+)" url="([^"]+)"')
_REVIEW_FINDING = re.compile(r'<finding id="(F\d+)" source="[^"]*" level="(\w+)">')
_WRITER_FINDING = re.compile(r"^(F\d+) (\[\d+\]) \((\w+)\) (.+)$", re.M)
_INT = r"(\d+)"

DATA_QUERIES = [
    "e-bike voucher pilot evaluation results",
    "share of short car trips statistics",
    "e-bike incentive carbon impact estimate",
]
MARKET_QUERIES = [
    "e-bike retail price overview market",
    "alternatives to subsidies bike-share transit pass comparison",
    "equity of income targeted mobility subsidies",
]


def _number(pattern: str, text: str, default: int) -> int:
    match = re.search(pattern.replace("N", _INT), text)
    return int(match.group(1)) if match else default


def _question(prompt: str) -> str:
    match = _QUESTION.search(prompt)
    return match.group(1).strip() if match else "the research question"


class DemoLLM:
    provider = "demo"
    model = "demo-fixtures"

    def __init__(self, step_delay_ms: int = 0) -> None:
        self._delay = step_delay_ms / 1000

    async def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        if self._delay:
            await asyncio.sleep(self._delay)
        builders = {
            ResearchPlan: self._plan,
            AssignmentSet: self._assign,
            ResearcherOutput: self._research,
            EvidenceReview: self._review,
            ReportOutline: self._outline,
            ReportDraft: self._draft,
            DraftReview: self._draft_review,
            ExecutiveSummary: self._summary,
            DirectorHandoff: self._handoff,
        }
        output = builders[schema](request.prompt)
        # Re-validate through the schema exactly like live output.
        validated = schema.model_validate(output.model_dump())
        return LLMResult(output=validated, usage=LLMUsage())

    async def aclose(self) -> None:
        return None

    # --- Director -------------------------------------------------------------

    def _plan(self, prompt: str) -> ResearchPlan:
        question = _question(prompt)[:200]
        return ResearchPlan(
            objective=(
                f"Demo run for “{question}”. The team works through a fixed, fictional evidence "
                f"set about {DEMO_TOPIC} to show how planning, research, review and writing fit "
                "together."
            ),
            sub_questions=[
                "What outcomes did comparable programmes report?",
                "How large is the pool of short car trips that could shift?",
                "What alternatives, costs and equity effects should be considered?",
                "Where does the evidence conflict or remain uncertain?",
            ],
            data_focus="Programme evaluations, travel statistics and impact estimates.",
            context_focus="Alternatives to vouchers, retail prices and equity considerations.",
            is_commercial=False,
            out_of_scope=["Real policy advice: the demo evidence is fictional."],
        )

    def _handoff(self, prompt: str) -> DirectorHandoff:
        return DirectorHandoff(
            handoff_summary=(
                "The report answers the question using the demo team's fictional evidence. "
                "It finds that voucher programmes can shift some short car trips, but the "
                "fixture cities report conflicting results, so conclusions are tentative."
            ),
            confidence="low",
            caveats=[
                "Demo mode: every source is a fictional fixture.",
                "Survey-based figures may over-represent enthusiastic participants.",
            ],
        )

    # --- Research team lead ---------------------------------------------------------

    def _assign(self, prompt: str) -> AssignmentSet:
        limit = _number("Maximum search queries per researcher: N", prompt, 3)
        assignments = [
            ResearchAssignment(
                role="data_researcher",
                objective="Find programme results, trip statistics and impact estimates.",
                search_queries=DATA_QUERIES[:limit],
                focus_points=["Observed outcomes", "Estimates versus measurements"],
            ),
        ]
        if "market_researcher" in prompt:
            assignments.append(
                ResearchAssignment(
                    role="market_researcher",
                    objective="Study alternatives, prices and equity context.",
                    search_queries=MARKET_QUERIES[:limit],
                    focus_points=["Alternatives", "Costs", "Equity"],
                )
            )
        return AssignmentSet(
            assignments=assignments,
            lead_note="Data first, context second; prefer primary reports.",
        )

    def _review(self, prompt: str) -> EvidenceReview:
        research_pass = _number("Research pass: N", prompt, 1)
        remaining = _number("Additional research passes available: N", prompt, 0)
        assessments = []
        verdicts = {"supported": "supported", "tentative": "weak", "conflicting": "conflicting"}
        for ref, level in _REVIEW_FINDING.findall(prompt):
            verdict = verdicts.get(level, "weak")
            assessments.append(
                FindingAssessment(finding_ref=ref, verdict=verdict, note=f"Demo review: {verdict}.")
            )
        wants_more = research_pass == 1 and remaining > 0
        return EvidenceReview(
            assessments=assessments,
            contradictions=[
                "Northvale reports 61% weekly car-trip replacement; Lakeport reports 22%."
            ],
            missing_information=(["Long-term retention and winter usage."] if wants_more else []),
            uncovered_sub_questions=[],
            decision="needs_more_research" if wants_more else "sufficient",
            follow_ups=(
                [
                    FollowUp(
                        role="data_researcher",
                        objective="Check whether riders keep riding after the pilot.",
                        search_queries=["voucher recipients long-term follow-up survey"],
                    ),
                    FollowUp(
                        role="market_researcher",
                        objective="Check seasonal usage in cold climates.",
                        search_queries=["winter e-bike usage cold climate"],
                    ),
                ]
                if wants_more
                else []
            ),
            summary=(
                "One page contained embedded instructions and no usable evidence; it was "
                "ignored. Evidence on programme outcomes conflicts between cities."
            ),
        )

    # --- Researchers -------------------------------------------------------------------

    def _research(self, prompt: str) -> ResearcherOutput:
        limit = _number("Maximum findings: N", prompt, 10)
        findings: list[ExtractedFinding] = []
        for ref, url in _SOURCE.findall(prompt):
            fixture = DEMO_BY_URL.get(url)
            if fixture is None:
                continue
            for item in fixture.findings:
                findings.append(
                    ExtractedFinding(
                        source_ref=ref,
                        claim=item.claim,
                        evidence_excerpt=item.excerpt,
                        support_level=item.support_level,
                        is_estimate=item.is_estimate,
                    )
                )
        return ResearcherOutput(
            findings=findings[:limit],
            coverage_note=f"Extracted {min(len(findings), limit)} findings from demo fixtures.",
            gaps=[] if findings else ["No relevant fixture matched this assignment."],
        )

    # --- Writing team ------------------------------------------------------------------

    def _outline(self, prompt: str) -> ReportOutline:
        refs = [m[0] for m in _WRITER_FINDING.findall(prompt)]
        return ReportOutline(
            title="Demo report: e-bike subsidy programmes (fictional data)",
            direct_answer_plan="Summarise what the programmes achieved and how certain it is.",
            sections=[
                OutlineSection(
                    heading="Programme outcomes",
                    purpose="Report observed results and retention.",
                    finding_refs=refs[:6],
                ),
                OutlineSection(
                    heading="Context and alternatives",
                    purpose="Prices, alternatives and equity.",
                    finding_refs=refs[6:12],
                ),
            ],
            key_caveats=["Conflicting city results", "Survey response bias", "Fictional data"],
        )

    def _draft(self, prompt: str) -> ReportDraft:
        findings = _WRITER_FINDING.findall(prompt)
        revising = "Revision request" in prompt
        by_level: dict[str, list[tuple[str, str]]] = {}
        for _, marker, level, claim in findings:
            by_level.setdefault(level, []).append((marker, claim.strip()))
        supported = by_level.get("supported", [])
        conflicting = by_level.get("conflicting", [])
        tentative = by_level.get("tentative", [])

        first = supported[0][0] if supported else (findings[0][1] if findings else "")
        conflict_marker = conflicting[0][0] if conflicting else ""
        direct = (
            "In this fictional demo evidence set, e-bike voucher programmes shifted some short car "
            f"trips to e-bikes {first}, but results differed sharply between cities "
            f"{conflict_marker}. The evidence supports a cautious, conditional answer rather "
            "than a firm recommendation."
        ).replace("  ", " ")
        key_findings = [
            KeyFinding(statement=f"{claim} {marker}")
            for marker, claim in (supported + conflicting)[:6]
        ] or [KeyFinding(statement=f"{findings[0][3]} {findings[0][1]}")]

        sections = ["### Programme outcomes"]
        sections += [f"- {claim} {marker}" for marker, claim in supported[:3]]
        sections.append("\n### Travel patterns")
        sections += [f"- {claim} {marker}" for marker, claim in supported[3:5]]
        if tentative:
            sections.append("\n### Estimates and weaker evidence")
            sections += [
                f"- {claim} {marker} This is an estimate and should be treated as uncertain."
                for marker, claim in tentative[:3]
            ]
        if revising and conflicting:
            sections.append("\n### Conflicting evidence")
            sections += [
                f"- {claim} {marker} The cause of the difference is uncertain; the source "
                "suggests terrain, rebate size and parking may matter (inference)."
                for marker, claim in conflicting[:2]
            ]
        if supported[5:]:
            sections.append("\n### Context, alternatives and follow-up evidence")
            sections += [f"- {claim} {marker}" for marker, claim in supported[5:12]]
        return ReportDraft(
            title="Demo report: e-bike subsidy programmes (fictional data)",
            direct_answer=direct,
            key_findings=key_findings,
            detailed_analysis="\n".join(sections),
            limitations=[
                "Survey-based figures may over-represent enthusiastic participants.",
                "Only a small number of comparable programmes were found.",
            ],
            unresolved_questions=[
                "How would results differ in a real city with different terrain and climate?"
            ],
        )

    def _draft_review(self, prompt: str) -> DraftReview:
        review_round = _number("Review round: N", prompt, 1)
        revisions = _number("Revisions available: N", prompt, 0)
        if review_round == 1 and revisions > 0:
            return DraftReview(
                approved=False,
                issues=["Conflicting results between cities are not discussed explicitly."],
                revision_instructions=(
                    "Add a short 'Conflicting evidence' subsection that presents the differing "
                    "city results side by side and labels any explanation as inference."
                ),
            )
        return DraftReview(approved=True, issues=[], revision_instructions="")

    def _summary(self, prompt: str) -> ExecutiveSummary:
        # Reuse sentences from the report itself so no new facts are added.
        direct = re.search(r"## Direct answer\n\n(.+?)\n\n", prompt, re.S)
        bullets = re.findall(r"^- (.+)$", prompt, re.M)[:2]
        parts = [direct.group(1).strip()] if direct else []
        parts += bullets
        text = " ".join(parts) or "This demo report summarises fictional fixture evidence."
        return ExecutiveSummary(summary=text[:1700])
