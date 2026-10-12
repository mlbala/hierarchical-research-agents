"""LangGraph node implementations.

Each node is a small, explicit step of the workflow. Nodes read and write the
typed ``ResearchState`` and use ``RunContext`` (the LangGraph runtime context)
for every side effect. Routing decisions that bound the loops are made here in
code and stored in state; the routing functions in ``graph.py`` only read
them. Agents never choose the next node themselves.
"""

from __future__ import annotations

import operator
import re
import uuid
from typing import Annotated, Any, TypedDict

from langgraph.runtime import Runtime

from app.agents import prompts
from app.agents.context import RunContext, TaskHandle
from app.agents.prompts import PromptFinding, PromptSource
from app.agents.roles import ROLE_LABELS, Role
from app.core.errors import (
    AppError,
    BudgetExceededError,
    CitationValidationError,
    ConfigurationError,
    NoUsableEvidenceError,
    ProviderError,
    ResearchTimeoutError,
    RunCancelledError,
    UnsafeUrlError,
)
from app.db.models import utcnow
from app.schemas.agents import (
    AssignmentSet,
    DirectorHandoff,
    DraftReview,
    EvidenceReview,
    ExecutiveSummary,
    ReportDraft,
    ReportOutline,
    ResearchAssignment,
    ResearcherOutput,
    ResearchPlan,
)
from app.services.citations import draft_citation_issues, validate_and_renumber
from app.services.evidence import FindingView, NewFinding, excerpt_in_source
from app.services.reports import CitedSource, ReportContent, ReportService, assemble_body
from app.tools.extract import parse_date
from app.tools.fetch import FetchedPage, FetchError, content_hash
from app.tools.search import SearchHit
from app.tools.url_safety import validate_public_url

# LLM calls the writing phase needs after research: outline, draft, review,
# summary and handoff. Research retries are only allowed if these remain.
WRITING_LLM_CALLS = 5
MAX_WRITER_FINDINGS = 40
# Provider failures that would fail every later step too: stop the run.
FATAL_PROVIDER_CODES = frozenset(
    {"LLM_AUTH_FAILED", "LLM_MODEL_NOT_FOUND", "SEARCH_AUTH_FAILED", "SEARCH_QUOTA_EXCEEDED"}
)
_PRIMARY_HOST = re.compile(r"(\.gov|\.gov\.[a-z]{2}|\.edu|\.int|\.ac\.[a-z]{2}|europa\.eu)$")


class ResearchState(TypedDict, total=False):
    # Input
    question: str
    context: str | None
    input_urls: list[str]
    depth: str
    # Director
    plan: dict[str, Any]
    director_task_id: str
    # Research team
    assignments: dict[str, dict[str, Any]]
    research_pass: int
    research_lead_task_id: str
    research_outcomes: Annotated[list[dict[str, Any]], operator.add]
    research_review: dict[str, Any]
    research_route: str
    approved_finding_ids: list[str]
    # Writing team
    writing_lead_task_id: str
    writer_evidence: list[dict[str, Any]]
    citation_sources: list[dict[str, Any]]
    outline: dict[str, Any]
    draft: dict[str, Any]
    draft_review: dict[str, Any]
    writing_route: str
    revision_count: int
    summary: str
    validated: dict[str, Any]
    handoff: dict[str, Any]
    # Outcome
    partial_reasons: Annotated[list[str], operator.add]
    report_id: str
    final_status: str


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _short(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_fatal(exc: AppError) -> bool:
    if isinstance(exc, RunCancelledError | ResearchTimeoutError | ConfigurationError):
        return True
    return isinstance(exc, ProviderError) and exc.code in FATAL_PROVIDER_CODES


# ---------------------------------------------------------------------------
# 1. Input validation (deterministic)
# ---------------------------------------------------------------------------


async def validate_input(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.ensure_active()
    await ctx.stage("validating", "Validating the research request")
    urls: list[str] = []
    for raw in state.get("input_urls", []):
        try:
            url = validate_public_url(raw).url
        except UnsafeUrlError as exc:
            await ctx.warn(exc.code, f"A reference URL was skipped: {exc.safe_message}")
            continue
        if url not in urls:
            urls.append(url)
    if ctx.execution_mode == "demo" and urls:
        await ctx.warn(
            "DEMO_URLS_NOT_FETCHED",
            "Demo mode does not retrieve reference URLs; only fictional fixtures are used.",
        )
    return {"input_urls": urls}


# ---------------------------------------------------------------------------
# 2. Director planning
# ---------------------------------------------------------------------------


async def director_plan(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.stage("planning", "Research director is planning")
    async with ctx.agent_task(
        Role.RESEARCH_DIRECTOR,
        "research_director.plan",
        input_summary=f"Plan research for: {_short(state['question'])}",
        parent_task_id=None,
    ) as task:
        plan = await ctx.call_llm(
            task,
            system=prompts.system_prompt(Role.RESEARCH_DIRECTOR, "research_director.plan"),
            prompt=prompts.plan_prompt(
                state["question"], state.get("context"), state.get("input_urls", [])
            ),
            schema=ResearchPlan,
            max_tokens=6_000,
        )
        await ctx.save_plan(plan.model_dump())
        task.set_output(
            f"Planned {len(plan.sub_questions)} sub-questions"
            + ("" if plan.context_focus else "; contextual research not needed"),
            objective=plan.objective,
            sub_questions=plan.sub_questions,
        )
    return {"plan": plan.model_dump(), "director_task_id": str(task.id)}


# ---------------------------------------------------------------------------
# 3. Research lead assignment
# ---------------------------------------------------------------------------


def normalize_assignments(
    result: AssignmentSet, plan: ResearchPlan, question: str, max_queries: int
) -> dict[str, ResearchAssignment]:
    """Apply code-level rules to the lead's assignments.

    At most one assignment per researcher, queries capped by depth, the
    context track only when the director asked for it, and a deterministic
    fallback so the data researcher always has work.
    """
    chosen: dict[str, ResearchAssignment] = {}
    for item in result.assignments:
        if item.role in chosen:
            continue
        if item.role == "market_researcher" and not plan.context_focus:
            continue
        queries = list(dict.fromkeys(q.strip() for q in item.search_queries if q.strip()))
        chosen[item.role] = item.model_copy(update={"search_queries": queries[:max_queries]})
    if "data_researcher" not in chosen:
        chosen["data_researcher"] = ResearchAssignment(
            role="data_researcher",
            objective=plan.data_focus,
            search_queries=[question[:200]],
            focus_points=[],
        )
    if plan.context_focus and "market_researcher" not in chosen:
        chosen["market_researcher"] = ResearchAssignment(
            role="market_researcher",
            objective=plan.context_focus,
            search_queries=[f"{question[:150]} alternatives context"],
            focus_points=[],
        )
    return chosen


async def research_lead_assign(
    state: ResearchState, runtime: Runtime[RunContext]
) -> dict[str, Any]:
    ctx = runtime.context
    plan = ResearchPlan.model_validate(state["plan"])
    async with ctx.agent_task(
        Role.RESEARCH_TEAM_LEAD,
        "research_team_lead.assign",
        input_summary="Assign research tasks from the director's plan",
        parent_task_id=_uuid(state.get("director_task_id")),
    ) as task:
        result = await ctx.call_llm(
            task,
            system=prompts.system_prompt(Role.RESEARCH_TEAM_LEAD, "research_team_lead.assign"),
            prompt=prompts.assign_prompt(
                state["question"],
                state.get("context"),
                state["plan"],
                ctx.limits.queries_per_researcher,
            ),
            schema=AssignmentSet,
            max_tokens=4_000,
        )
        assignments = normalize_assignments(
            result, plan, state["question"], ctx.limits.queries_per_researcher
        )
        task.set_output(
            "Assigned " + ", ".join(ROLE_LABELS[Role(r)] for r in assignments),
            assignments={r: a.model_dump() for r, a in assignments.items()},
        )
    await ctx.stage("researching", "Researchers are gathering evidence", research_pass=1)
    return {
        "assignments": {r: a.model_dump() for r, a in assignments.items()},
        "research_pass": 1,
        "research_lead_task_id": str(task.id),
    }


# ---------------------------------------------------------------------------
# 4. Researchers (data and market), run in parallel
# ---------------------------------------------------------------------------


def rank_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """Prefer likely primary sources (government, academic, intergovernmental)."""

    def score(hit: SearchHit) -> tuple[int, float]:
        host = re.sub(r"^https?://", "", hit.url).split("/", 1)[0].lower()
        return (0 if _PRIMARY_HOST.search(host) else 1, -(hit.score or 0.0))

    return sorted(hits, key=score)


async def _store_page(ctx: RunContext, url: str, page: FetchedPage, kind: str) -> uuid.UUID | None:
    try:
        await ctx.budget.reserve("sources")
    except BudgetExceededError:
        return None
    result = await ctx.evidence.upsert_source(
        ctx.run_id,
        url=page.final_url,
        title=page.title,
        publisher=page.publisher,
        published_at=page.published_at,
        content_text=page.text,
        content_hash=page.content_hash,
        content_kind=kind,
        metadata={
            "requested_url": url,
            "redirects": page.redirects,
            "truncated": page.truncated,
            "bytes": page.byte_count,
            "content_type": page.content_type,
        },
    )
    if not result.created:
        await ctx.budget.release("sources")
    return result.id


async def _store_snippet(ctx: RunContext, hit: SearchHit, reason: str) -> uuid.UUID | None:
    if not hit.snippet.strip():
        return None
    try:
        await ctx.budget.reserve("sources")
    except BudgetExceededError:
        return None
    result = await ctx.evidence.upsert_source(
        ctx.run_id,
        url=hit.url,
        title=hit.title,
        publisher=re.sub(r"^https?://", "", hit.url).split("/", 1)[0],
        published_at=parse_date(hit.published_date),
        content_text=hit.snippet,
        content_hash=content_hash(hit.snippet),
        content_kind="search_snippet",
        metadata={"search_score": hit.score, "page_not_read_reason": reason},
    )
    if not result.created:
        await ctx.budget.release("sources")
    return result.id


async def _gather_sources(
    ctx: RunContext,
    task: TaskHandle,
    role: Role,
    assignment: ResearchAssignment,
    input_urls: list[str],
) -> tuple[list[PromptSource], dict[str, uuid.UUID], list[str]]:
    """Search, retrieve and store sources. Returns prompt sources and limits hit."""
    limits_hit: list[str] = []
    stored: list[tuple[uuid.UUID, str, str, str, str]] = []  # id, url, title, kind, text
    kind_for_page = "demo" if ctx.execution_mode == "demo" else "fetched_page"

    # User-supplied reference URLs are read first by the data researcher.
    for url in input_urls if ctx.execution_mode == "live" else []:
        if len(stored) >= ctx.limits.pages_per_researcher:
            break
        try:
            reference = await ctx.tools.fetch_page(role, task, url)
        except BudgetExceededError:
            limits_hit.append("fetch")
            break
        except (UnsafeUrlError, FetchError, ProviderError) as exc:
            if isinstance(exc, ProviderError) and _is_fatal(exc):
                raise
            await ctx.warn(exc.code, f"Reference URL could not be read: {exc.safe_message}")
            continue
        source_id = await _store_page(ctx, url, reference, kind_for_page)
        if source_id is None:
            limits_hit.append("sources")
            break
        stored.append(
            (source_id, reference.final_url, reference.title, kind_for_page, reference.text)
        )

    # Web searches; hits already stored in this run (by any researcher) are skipped.
    hits: dict[str, SearchHit] = {}
    for query in assignment.search_queries:
        try:
            results = await ctx.tools.web_search(role, task, query)
        except BudgetExceededError:
            limits_hit.append("search")
            break
        except ProviderError as exc:
            if _is_fatal(exc):
                raise
            await ctx.warn(exc.code, f"A search failed: {exc.safe_message}", task_id=task.id)
            continue
        for hit in results:
            hits.setdefault(hit.url, hit)
    known = await ctx.evidence.find_source_ids(ctx.run_id, hits)
    candidates = [h for h in rank_hits(list(hits.values())) if h.url not in known]

    remaining_slots = max(0, ctx.limits.pages_per_researcher - len(stored))
    for hit in candidates[:remaining_slots]:
        page: FetchedPage | None = None
        reason = ""
        if "fetch" not in limits_hit:
            try:
                page = await ctx.tools.fetch_page(role, task, hit.url)
            except BudgetExceededError:
                limits_hit.append("fetch")
                reason = "fetch budget exhausted"
            except (UnsafeUrlError, FetchError, ProviderError) as exc:
                if isinstance(exc, ProviderError) and _is_fatal(exc):
                    raise
                reason = exc.code
        else:
            reason = "fetch budget exhausted"
        if page is not None:
            source_id = await _store_page(ctx, hit.url, page, kind_for_page)
            entry = (page.final_url, page.title, kind_for_page, page.text)
        else:
            # Keep the snippet, clearly labelled: the page itself was not read.
            source_id = await _store_snippet(ctx, hit, reason)
            entry = (hit.url, hit.title, "search_snippet", hit.snippet)
        if source_id is None:
            if hit.snippet.strip() or page is not None:
                limits_hit.append("sources")
                break
            continue
        stored.append((source_id, *entry))

    for resource in sorted(set(limits_hit)):
        await ctx.warn(
            "BUDGET_LIMIT",
            f"The run's {resource} limit was reached; some planned research was skipped.",
            task_id=task.id,
        )

    prompt_sources: list[PromptSource] = []
    ref_map: dict[str, uuid.UUID] = {}
    for index, (source_id, url, title, kind, text) in enumerate(stored, start=1):
        ref = f"S{index}"
        ref_map[ref] = source_id
        prompt_sources.append(PromptSource(ref=ref, url=url, title=title, kind=kind, text=text))
    return prompt_sources, ref_map, sorted(set(limits_hit))


async def _research(
    ctx: RunContext,
    task: TaskHandle,
    role: Role,
    assignment: ResearchAssignment,
    state: ResearchState,
) -> dict[str, Any]:
    research_pass = state.get("research_pass", 1)
    input_urls = state.get("input_urls", []) if (
        role == Role.DATA_RESEARCHER and research_pass == 1
    ) else []  # fmt: skip
    sources, ref_map, limits_hit = await _gather_sources(ctx, task, role, assignment, input_urls)
    outcome: dict[str, Any] = {
        "role": role.value,
        "pass": research_pass,
        "sources": len(sources),
        "limits_hit": limits_hit,
        "findings": 0,
    }
    if not sources:
        task.set_output("No usable sources were found for this assignment.", **outcome)
        return {**outcome, "status": "no_sources", "note": "No usable sources found."}

    output = await ctx.call_llm(
        task,
        system=prompts.system_prompt(role, f"{role.value}.research"),
        prompt=prompts.researcher_prompt(
            role=role,
            question=state["question"],
            objective=assignment.objective,
            focus_points=assignment.focus_points,
            sources=sources,
            research_pass=research_pass,
            max_findings=ctx.limits.max_findings_per_researcher,
            excerpt_chars=ctx.settings.llm_source_excerpt_chars,
            total_chars=ctx.settings.llm_max_prompt_chars - 6_000,
        ),
        schema=ResearcherOutput,
        max_tokens=8_000,
    )

    by_ref = {s.ref: s for s in sources}
    accepted: list[NewFinding] = []
    unknown_refs = unverified = 0
    for item in output.findings[: ctx.limits.max_findings_per_researcher]:
        source = by_ref.get(item.source_ref)
        if source is None:
            unknown_refs += 1
            continue
        if not excerpt_in_source(item.evidence_excerpt, source.text):
            unverified += 1
            continue
        level = item.support_level
        if source.kind == "search_snippet" and level == "supported":
            level = "tentative"  # snippet-only evidence is never fully supported
        claim = item.claim.strip()
        if item.is_estimate and not claim.lower().startswith(("estimate", "modelled", "projected")):
            claim = f"Estimate: {claim}"
        accepted.append(
            NewFinding(
                source_id=ref_map[item.source_ref],
                claim=claim,
                evidence_excerpt=item.evidence_excerpt.strip(),
                support_level=level,
            )
        )
    await ctx.evidence.insert_findings(ctx.run_id, task.id, accepted)
    if unknown_refs or unverified:
        await ctx.warn(
            "FINDINGS_DISCARDED",
            f"{ROLE_LABELS[role]}: {unknown_refs + unverified} finding(s) were discarded because "
            "their quoted evidence could not be located in the cited source.",
            task_id=task.id,
        )
    outcome.update(findings=len(accepted), discarded=unknown_refs + unverified)
    task.set_output(
        f"Reported {len(accepted)} findings from {len(sources)} sources to the research lead",
        **outcome,
        coverage_note=output.coverage_note,
        gaps=output.gaps,
    )
    return {
        **outcome,
        "status": "completed",
        "note": output.coverage_note,
        "gaps": output.gaps,
    }


def make_researcher_node(role: Role) -> Any:
    async def researcher(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
        ctx = runtime.context
        research_pass = state.get("research_pass", 1)
        raw = state.get("assignments", {}).get(role.value)
        parent = _uuid(state.get("research_lead_task_id"))
        if raw is None:
            await ctx.record_skipped(
                role,
                f"{role.value}.research",
                reason="No assignment from the research lead for this pass.",
                parent_task_id=parent,
            )
            return {
                "research_outcomes": [
                    {"role": role.value, "pass": research_pass, "status": "skipped"}
                ]
            }
        assignment = ResearchAssignment.model_validate(raw)
        try:
            async with ctx.agent_task(
                role,
                f"{role.value}.research",
                input_summary=_short(assignment.objective, 300),
                parent_task_id=parent,
            ) as task:
                outcome = await _research(ctx, task, role, assignment, state)
        except AppError as exc:
            if _is_fatal(exc):
                raise
            # Contained failure: the other researcher's evidence can still be used.
            return {
                "research_outcomes": [
                    {
                        "role": role.value,
                        "pass": research_pass,
                        "status": "failed",
                        "error_code": exc.code,
                    }
                ],
                "partial_reasons": [f"{ROLE_LABELS[role]} could not finish: {exc.safe_message}"],
            }
        reasons = []
        if outcome.get("status") == "no_sources":
            reasons.append(
                f"{ROLE_LABELS[role]} found no usable sources in research pass {research_pass}."
            )
        if outcome.get("limits_hit"):
            reasons.append(
                f"{ROLE_LABELS[role]} stopped early after reaching the "
                f"{', '.join(outcome['limits_hit'])} limit."
            )
        return {"research_outcomes": [outcome], "partial_reasons": reasons}

    researcher.__name__ = role.value
    return researcher


# ---------------------------------------------------------------------------
# 5. Research lead review (with at most one retry pass)
# ---------------------------------------------------------------------------


def _review_findings(
    findings: list[FindingView],
) -> tuple[list[PromptFinding], dict[str, FindingView]]:
    source_refs: dict[uuid.UUID, str] = {}
    refs: dict[str, FindingView] = {}
    prompt_findings = []
    for index, finding in enumerate(findings, start=1):
        sref = source_refs.setdefault(finding.source_id, f"S{len(source_refs) + 1}")
        ref = f"F{index}"
        refs[ref] = finding
        kind_note = "" if finding.source_kind != "search_snippet" else " (snippet only)"
        prompt_findings.append(
            PromptFinding(
                ref=ref,
                claim=finding.claim,
                excerpt=finding.evidence_excerpt,
                support_level=finding.support_level,
                source_label=f"{sref}{kind_note}",
                source_title=finding.source_title,
            )
        )
    return prompt_findings, refs


async def research_lead_review(
    state: ResearchState, runtime: Runtime[RunContext]
) -> dict[str, Any]:
    ctx = runtime.context
    research_pass = state.get("research_pass", 1)
    await ctx.stage("reviewing_evidence", "Research lead is reviewing the evidence")
    passes_remaining = max(0, ctx.limits.research_retries - (research_pass - 1))
    async with ctx.agent_task(
        Role.RESEARCH_TEAM_LEAD,
        "research_team_lead.review",
        input_summary=f"Review evidence after research pass {research_pass}",
        parent_task_id=_uuid(state.get("director_task_id")),
    ) as task:
        findings = await ctx.tools.run_evidence(Role.RESEARCH_TEAM_LEAD)
        if not findings:
            raise NoUsableEvidenceError()
        prompt_findings, refs = _review_findings(findings[:60])
        notes = [
            f"{o.get('role')} pass {o.get('pass')}: {o.get('status')} "
            f"({o.get('findings', 0)} findings) {o.get('note', '')}"
            for o in state.get("research_outcomes", [])
        ]
        review = await ctx.call_llm(
            task,
            system=prompts.system_prompt(Role.RESEARCH_TEAM_LEAD, "research_team_lead.review"),
            prompt=prompts.review_prompt(
                question=state["question"],
                plan=state["plan"],
                findings=prompt_findings,
                research_pass=research_pass,
                passes_remaining=passes_remaining,
                researcher_notes=notes,
            ),
            schema=EvidenceReview,
            max_tokens=8_000,
        )

        # Apply the lead's support judgements in code.
        rejected: set[uuid.UUID] = set()
        levels: dict[uuid.UUID, str] = {}
        for assessment in review.assessments:
            finding = refs.get(assessment.finding_ref)
            if finding is None:
                continue
            if assessment.verdict == "unsupported":
                rejected.add(finding.id)
            elif assessment.verdict == "weak" and finding.support_level == "supported":
                levels[finding.id] = "tentative"
            elif assessment.verdict == "conflicting":
                levels[finding.id] = "conflicting"
        await ctx.evidence.update_support_levels(ctx.run_id, levels)
        approved = [f.id for f in findings if f.id not in rejected]
        if not approved:
            raise NoUsableEvidenceError()

        wants_more = review.decision != "sufficient" and bool(review.follow_ups)
        can_retry = (
            passes_remaining > 0
            and ctx.budget.remaining("search") > 0
            and ctx.budget.remaining("llm") >= WRITING_LLM_CALLS + 3
        )
        route = "retry" if wants_more and can_retry else "write"
        task.set_output(
            f"{len(approved)} findings approved, {len(rejected)} rejected; "
            + ("requested one more research pass" if route == "retry" else "handed to writing"),
            decision=review.decision,
            route=route,
            approved_count=len(approved),
            rejected_finding_ids=[str(i) for i in rejected],
            contradictions=review.contradictions,
            missing_information=review.missing_information,
            uncovered_sub_questions=review.uncovered_sub_questions,
            review_summary=review.summary,
        )

    update: dict[str, Any] = {
        "research_review": {
            "decision": review.decision,
            "summary": review.summary,
            "contradictions": review.contradictions,
            "missing_information": review.missing_information,
            "uncovered_sub_questions": review.uncovered_sub_questions,
        },
        "approved_finding_ids": [str(i) for i in approved],
        "research_route": route,
        "research_lead_task_id": str(task.id),
    }
    if route == "retry":
        update["assignments"] = {
            f.role: ResearchAssignment(
                role=f.role,
                objective=f.objective,
                search_queries=f.search_queries[: ctx.limits.queries_per_researcher],
            ).model_dump()
            for f in review.follow_ups
        }
        update["research_pass"] = research_pass + 1
        await ctx.stage(
            "researching",
            "Researchers are running a follow-up pass",
            research_pass=research_pass + 1,
        )
        return update

    reasons: list[str] = []
    if review.decision != "sufficient":
        detail = "; ".join(review.missing_information[:3]) or review.summary
        reasons.append(f"The research lead judged the evidence incomplete: {_short(detail, 300)}")
    for question in review.uncovered_sub_questions[:3]:
        reasons.append(f"Not covered by the evidence: {_short(question, 200)}")
    for resource in sorted(ctx.budget.exhausted):
        reasons.append(f"Research stopped early at the run's {resource} limit.")
    update["partial_reasons"] = reasons
    return update


# ---------------------------------------------------------------------------
# 6. Writing lead outline
# ---------------------------------------------------------------------------


def _writer_prompt_findings(evidence: list[dict[str, Any]]) -> list[PromptFinding]:
    return [
        PromptFinding(
            ref=item["ref"],
            claim=item["claim"],
            excerpt=item["excerpt"],
            support_level=item["support_level"],
            source_label=f"[{item['citation']}]",
            source_title=item["source_title"],
        )
        for item in evidence
    ]


def _source_tuples(sources: list[dict[str, Any]]) -> list[tuple[int, str, str, str]]:
    return [
        (s["number"], s["title"], s.get("publisher") or "unknown publisher", s["kind"])
        for s in sources
    ]


async def writing_lead_outline(
    state: ResearchState, runtime: Runtime[RunContext]
) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.stage("outlining", "Writing lead is outlining the report")
    approved_ids = [uuid.UUID(i) for i in state.get("approved_finding_ids", [])]
    findings = await ctx.tools.run_evidence(Role.WRITING_TEAM_LEAD, approved_ids)
    if len(findings) > MAX_WRITER_FINDINGS:
        # Keep the findings whose sources are most relevant to the question.
        ranking = await ctx.tools.rank_run_sources(Role.WRITING_TEAM_LEAD, state["question"], 50)
        order = {sid: i for i, (sid, _) in enumerate(ranking)}
        findings = sorted(findings, key=lambda f: order.get(f.source_id, len(order)))
        findings = findings[:MAX_WRITER_FINDINGS]

    # Provisional citation numbers in order of first appearance.
    numbers: dict[uuid.UUID, int] = {}
    evidence = []
    for index, finding in enumerate(findings, start=1):
        number = numbers.setdefault(finding.source_id, len(numbers) + 1)
        evidence.append(
            {
                "ref": f"F{index}",
                "finding_id": str(finding.id),
                "citation": number,
                "claim": finding.claim,
                "excerpt": finding.evidence_excerpt,
                "support_level": finding.support_level,
                "source_title": finding.source_title,
            }
        )
    sources = await ctx.evidence.get_sources(ctx.run_id, numbers)
    by_id = {s.id: s for s in sources}
    citation_sources = [
        {
            "number": n,
            "source_id": str(sid),
            "title": by_id[sid].title,
            "publisher": by_id[sid].publisher,
            "kind": by_id[sid].content_kind,
        }
        for sid, n in sorted(numbers.items(), key=lambda kv: kv[1])
    ]

    async with ctx.agent_task(
        Role.WRITING_TEAM_LEAD,
        "writing_team_lead.outline",
        input_summary=f"Outline a report from {len(evidence)} approved findings",
        parent_task_id=_uuid(state.get("director_task_id")),
    ) as task:
        outline = await ctx.call_llm(
            task,
            system=prompts.system_prompt(Role.WRITING_TEAM_LEAD, "writing_team_lead.outline"),
            prompt=prompts.outline_prompt(
                question=state["question"],
                plan=state["plan"],
                review_summary=state.get("research_review", {}),
                findings=_writer_prompt_findings(evidence),
                sources=_source_tuples(citation_sources),
            ),
            schema=ReportOutline,
            max_tokens=4_000,
        )
        task.set_output(
            f"Outlined “{_short(outline.title, 100)}” with {len(outline.sections)} sections",
            sections=[s.heading for s in outline.sections],
        )
    return {
        "outline": outline.model_dump(),
        "writer_evidence": evidence,
        "citation_sources": citation_sources,
        "writing_lead_task_id": str(task.id),
        "revision_count": 0,
    }


# ---------------------------------------------------------------------------
# 7. Technical writer (draft and at most one revision)
# ---------------------------------------------------------------------------


async def technical_writer(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    revising = state.get("writing_route") == "revise"
    await ctx.stage(
        "drafting",
        "Technical writer is revising the draft" if revising else "Technical writer is drafting",
    )
    review = state.get("research_review", {})
    gaps = [*review.get("missing_information", []), *review.get("uncovered_sub_questions", [])]
    async with ctx.agent_task(
        Role.TECHNICAL_WRITER,
        "technical_writer.draft",
        input_summary="Revise the draft per review" if revising else "Write the detailed report",
        parent_task_id=_uuid(state.get("writing_lead_task_id")),
    ) as task:
        draft = await ctx.call_llm(
            task,
            system=prompts.system_prompt(Role.TECHNICAL_WRITER, "technical_writer.draft"),
            prompt=prompts.draft_prompt(
                question=state["question"],
                outline=state["outline"],
                review_summary=review,
                findings=_writer_prompt_findings(state.get("writer_evidence", [])),
                sources=_source_tuples(state.get("citation_sources", [])),
                known_gaps=[*gaps, *state.get("partial_reasons", [])],
                revision=state.get("draft_review") if revising else None,
                previous_draft=state.get("draft") if revising else None,
            ),
            schema=ReportDraft,
            max_tokens=16_000,
        )
        task.set_output(
            f"{'Revised' if revising else 'Drafted'} report with "
            f"{len(draft.key_findings)} key findings",
            title=draft.title,
        )
    return {
        "draft": draft.model_dump(),
        "revision_count": state.get("revision_count", 0) + (1 if revising else 0),
    }


# ---------------------------------------------------------------------------
# 8. Writing lead review (decides on the single revision)
# ---------------------------------------------------------------------------


async def writing_lead_review(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.stage("reviewing_draft", "Writing lead is reviewing the draft")
    draft = ReportDraft.model_validate(state["draft"])
    valid = {s["number"] for s in state.get("citation_sources", [])}
    automated = draft_citation_issues(
        draft.direct_answer,
        [k.statement for k in draft.key_findings],
        draft.detailed_analysis,
        valid,
    )
    revisions_left = ctx.limits.writing_revisions - state.get("revision_count", 0)
    async with ctx.agent_task(
        Role.WRITING_TEAM_LEAD,
        "writing_team_lead.review",
        input_summary="Review the draft for clarity and evidence coverage",
        parent_task_id=_uuid(state.get("director_task_id")),
    ) as task:
        review = await ctx.call_llm(
            task,
            system=prompts.system_prompt(Role.WRITING_TEAM_LEAD, "writing_team_lead.review"),
            prompt=prompts.draft_review_prompt(
                question=state["question"],
                draft=state["draft"],
                findings=_writer_prompt_findings(state.get("writer_evidence", [])),
                automated_issues=automated,
                review_round=state.get("revision_count", 0) + 1,
                revisions_available=max(0, revisions_left),
            ),
            schema=DraftReview,
            max_tokens=4_000,
        )
        approved = review.approved and not automated
        can_revise = revisions_left > 0 and ctx.budget.remaining("llm") >= 3
        route = "revise" if not approved and can_revise else "summarize"
        issues = [*automated, *review.issues]
        task.set_output(
            "Approved the draft" if approved else (
                "Requested one revision" if route == "revise" else "Accepted with open issues"
            ),
            approved=approved,
            issues=issues,
        )  # fmt: skip
    reasons = []
    if not approved and route == "summarize":
        reasons.append(
            "The writing lead flagged issues that could not be revised within limits: "
            + _short("; ".join(issues), 300)
        )
    return {
        "draft_review": {"issues": issues, "revision_instructions": review.revision_instructions},
        "writing_route": route,
        "partial_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# 9. Executive summary
# ---------------------------------------------------------------------------


def _content_from_state(
    state: ResearchState, summary: str, texts: list[str] | None = None
) -> ReportContent:
    draft = ReportDraft.model_validate(state["draft"])
    direct, analysis = draft.direct_answer, draft.detailed_analysis
    key_findings = [k.statement for k in draft.key_findings]
    if texts is not None:
        direct, analysis, key_findings = texts[0], texts[-1], texts[1:-1]
    limitations = list(
        dict.fromkeys(
            [
                *draft.limitations,
                *[f"Unresolved: {q}" for q in draft.unresolved_questions],
                *state.get("partial_reasons", []),
            ]
        )
    )
    return ReportContent(
        title=draft.title,
        executive_summary=summary,
        direct_answer=direct,
        key_findings=key_findings,
        detailed_analysis=analysis,
        limitations=limitations,
        is_partial=bool(state.get("partial_reasons")),
        partial_reasons=list(state.get("partial_reasons", [])),
        execution_mode="",
    )


def _provisional_sources(state: ResearchState) -> list[CitedSource]:
    return [
        CitedSource(
            number=s["number"],
            source_id=uuid.UUID(s["source_id"]),
            title=s["title"],
            url="",
            publisher=s.get("publisher"),
            published_at=None,
            retrieved_at=utcnow(),
            content_kind=s["kind"],
        )
        for s in state.get("citation_sources", [])
    ]


async def summary_writer(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.stage("summarizing", "Summary writer is writing the executive summary")
    body = assemble_body(_content_from_state(state, ""), _provisional_sources(state))
    async with ctx.agent_task(
        Role.SUMMARY_WRITER,
        "summary_writer.summary",
        input_summary="Summarise the finished report without adding new claims",
        parent_task_id=_uuid(state.get("writing_lead_task_id")),
    ) as task:
        result = await ctx.call_llm(
            task,
            system=prompts.system_prompt(Role.SUMMARY_WRITER, "summary_writer.summary"),
            prompt=prompts.summary_prompt(report_markdown=body),
            schema=ExecutiveSummary,
            max_tokens=4_000,
        )
        task.set_output(f"Wrote a {len(result.summary.split())}-word executive summary")
    return {"summary": result.summary}


# ---------------------------------------------------------------------------
# 10. Deterministic citation validation
# ---------------------------------------------------------------------------


async def citation_validation(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.ensure_active()
    await ctx.stage("validating_citations", "Validating citations")
    draft = ReportDraft.model_validate(state["draft"])
    provisional = {
        s["number"]: uuid.UUID(s["source_id"]) for s in state.get("citation_sources", [])
    }
    texts = [
        draft.direct_answer,
        *[k.statement for k in draft.key_findings],
        draft.detailed_analysis,
    ]
    result = validate_and_renumber(texts, state.get("summary", ""), provisional)
    if not result.citations:
        raise CitationValidationError(
            "The report contained no valid citations, so it was not published."
        )
    # Ownership check against the database (sources must belong to this run).
    async with ctx.db.session() as session:
        await ctx.evidence.assert_sources_in_run(
            session, ctx.run_id, [s for _, s in result.citations]
        )
    if result.removed_markers:
        await ctx.warn(
            "INVALID_CITATIONS_REMOVED",
            f"Removed {len(result.removed_markers)} citation marker(s) that matched no source.",
        )
    if result.removed_summary_markers:
        await ctx.warn(
            "SUMMARY_CITATIONS_REMOVED",
            "Removed executive-summary citations that do not appear in the report body.",
        )
    if result.unsupported_summary_figures:
        await ctx.warn(
            "SUMMARY_FIGURES_UNVERIFIED",
            "The executive summary mentions figures not found in the report body: "
            + ", ".join(result.unsupported_summary_figures[:6]),
        )
    await ctx.emit(
        "citations_validated",
        f"Validated {len(result.citations)} citations against this run's sources.",
        payload={"citations": len(result.citations), "removed": len(result.removed_markers)},
    )
    return {
        "validated": {
            "texts": result.texts,
            "summary": result.summary,
            "citations": [[n, str(s)] for n, s in result.citations],
        }
    }


# ---------------------------------------------------------------------------
# 11. Director handoff and 12. final report
# ---------------------------------------------------------------------------


async def director_handoff(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.stage("handoff", "Research director is reviewing the final report")
    content = _content_from_state(state, state["validated"]["summary"])
    async with ctx.agent_task(
        Role.RESEARCH_DIRECTOR,
        "research_director.handoff",
        input_summary="Receive the completed report and write the handoff note",
        parent_task_id=None,
    ) as task:
        handoff = await ctx.call_llm(
            task,
            system=prompts.system_prompt(Role.RESEARCH_DIRECTOR, "research_director.handoff"),
            prompt=prompts.handoff_prompt(
                question=state["question"],
                executive_summary=state["validated"]["summary"],
                limitations=content.limitations,
                partial_reasons=state.get("partial_reasons", []),
            ),
            schema=DirectorHandoff,
            max_tokens=3_000,
        )
        task.set_output(
            handoff.handoff_summary, confidence=handoff.confidence, caveats=handoff.caveats
        )
    return {"handoff": handoff.model_dump()}


async def finalize_report(state: ResearchState, runtime: Runtime[RunContext]) -> dict[str, Any]:
    ctx = runtime.context
    await ctx.stage("finalizing", "Saving the final report")
    validated = state["validated"]
    content = _content_from_state(state, validated["summary"], validated["texts"])
    if ctx.execution_mode == "demo":
        content.limitations.insert(
            0, "Demo mode: all evidence is fictional fixture data, not real research."
        )
    citations = [(int(n), uuid.UUID(sid)) for n, sid in validated["citations"]]
    report = await ReportService(ctx.db).save(ctx.run_id, content, citations)
    status = "partial" if content.is_partial else "completed"
    await ctx.emit(
        "report_ready",
        "Partial report saved." if content.is_partial else "Report saved.",
        payload={"report_id": str(report.id), "is_partial": content.is_partial},
    )
    return {"report_id": str(report.id), "final_status": status}
