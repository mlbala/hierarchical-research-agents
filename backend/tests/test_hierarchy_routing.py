"""The fixed hierarchy, tool permissions and bounded graph routing."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.graph import (
    NODE_ROLES,
    build_graph,
    route_after_draft,
    route_after_research_review,
    route_after_writing_review,
)
from app.agents.roles import RESEARCHERS, ROLE_PARENT, TOOL_PERMISSIONS, Role, Tool, can_use
from app.core.errors import ToolPermissionError
from app.tools.registry import RunTools

EXPECTED_EDGES = {
    ("__start__", "validate_input"),
    ("validate_input", "director_plan"),
    ("director_plan", "research_lead_assign"),
    ("research_lead_assign", "data_researcher"),
    ("research_lead_assign", "market_researcher"),
    ("data_researcher", "research_lead_review"),
    ("market_researcher", "research_lead_review"),
    ("research_lead_review", "data_researcher"),
    ("research_lead_review", "market_researcher"),
    ("research_lead_review", "writing_lead_outline"),
    ("writing_lead_outline", "technical_writer"),
    ("technical_writer", "writing_lead_review"),
    ("technical_writer", "summary_writer"),
    ("writing_lead_review", "technical_writer"),
    ("writing_lead_review", "summary_writer"),
    ("summary_writer", "citation_validation"),
    ("citation_validation", "director_handoff"),
    ("director_handoff", "finalize_report"),
    ("finalize_report", "__end__"),
}


def test_hierarchy_has_seven_roles_with_fixed_reporting_lines() -> None:
    assert len(Role) == 7
    assert ROLE_PARENT[Role.RESEARCH_DIRECTOR] is None
    assert ROLE_PARENT[Role.RESEARCH_TEAM_LEAD] == Role.RESEARCH_DIRECTOR
    assert ROLE_PARENT[Role.WRITING_TEAM_LEAD] == Role.RESEARCH_DIRECTOR
    assert ROLE_PARENT[Role.DATA_RESEARCHER] == Role.RESEARCH_TEAM_LEAD
    assert ROLE_PARENT[Role.MARKET_RESEARCHER] == Role.RESEARCH_TEAM_LEAD
    assert ROLE_PARENT[Role.TECHNICAL_WRITER] == Role.WRITING_TEAM_LEAD
    assert ROLE_PARENT[Role.SUMMARY_WRITER] == Role.WRITING_TEAM_LEAD


def test_only_researchers_may_browse() -> None:
    for role in Role:
        browsing = role in RESEARCHERS
        assert can_use(role, Tool.WEB_SEARCH) is browsing
        assert can_use(role, Tool.FETCH_PAGE) is browsing
        assert can_use(role, Tool.RUN_EVIDENCE)
    # No other tools exist: no shell, eval, SQL or arbitrary HTTP.
    assert set(TOOL_PERMISSIONS) == {Tool.WEB_SEARCH, Tool.FETCH_PAGE, Tool.RUN_EVIDENCE}


@pytest.mark.parametrize(
    "role",
    [r for r in Role if r not in RESEARCHERS],
)
async def test_non_researchers_are_denied_browsing_tools(role: Role) -> None:
    # Authorization happens before any budget, network or database access.
    tools = RunTools(SimpleNamespace(run_id=uuid.uuid4()))  # type: ignore[arg-type]
    with pytest.raises(ToolPermissionError):
        await tools.web_search(role, object(), "query text")  # type: ignore[arg-type]
    with pytest.raises(ToolPermissionError):
        await tools.fetch_page(role, object(), "https://example.com/")  # type: ignore[arg-type]


def test_graph_edges_are_exactly_the_permitted_routes() -> None:
    graph = build_graph().get_graph()
    edges = {(e.source, e.target) for e in graph.edges}
    assert edges == EXPECTED_EDGES
    nodes = {n for n in graph.nodes if not n.startswith("__")}
    assert nodes == set(NODE_ROLES)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"research_route": "retry"}, ["data_researcher", "market_researcher"]),
        ({"research_route": "write"}, "writing_lead_outline"),
        ({}, "writing_lead_outline"),
        # Anything unexpected falls through to the safe default.
        ({"research_route": "director_plan"}, "writing_lead_outline"),
    ],
)
def test_research_review_routing(state: dict[str, Any], expected: Any) -> None:
    assert route_after_research_review(state) == expected  # type: ignore[arg-type]


def test_writing_routing_allows_one_revision_only() -> None:
    assert route_after_draft({"revision_count": 0}) == "writing_lead_review"
    # After a revision the draft is never reviewed again (no second loop).
    assert route_after_draft({"revision_count": 1}) == "summary_writer"
    assert route_after_writing_review({"writing_route": "revise"}) == "technical_writer"
    assert route_after_writing_review({"writing_route": "summarize"}) == "summary_writer"
    assert route_after_writing_review({}) == "summary_writer"
