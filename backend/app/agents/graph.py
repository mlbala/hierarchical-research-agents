"""The explicit LangGraph workflow.

    validate_input
      -> director_plan
      -> research_lead_assign
      -> data_researcher || market_researcher        (parallel, same super-step)
      -> research_lead_review
           -> (retry, at most once) both researchers -> research_lead_review
           -> writing_lead_outline
      -> technical_writer
           -> writing_lead_review -> (revise, at most once) technical_writer
           -> summary_writer
      -> citation_validation -> director_handoff -> finalize_report -> END

Every conditional edge has an explicit ``path_map``: routing functions can
only select from the listed nodes, and they only read decisions that nodes
computed in code under the run's limits. ``recursion_limit`` is a further
hard stop on the number of super-steps.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents import nodes
from app.agents.context import RunContext
from app.agents.nodes import ResearchState
from app.agents.roles import Role

# Upper bound on super-steps: a full run with both loops uses 14.
RECURSION_LIMIT = 30

NODE_ROLES: dict[str, Role | None] = {
    "validate_input": None,
    "director_plan": Role.RESEARCH_DIRECTOR,
    "research_lead_assign": Role.RESEARCH_TEAM_LEAD,
    "data_researcher": Role.DATA_RESEARCHER,
    "market_researcher": Role.MARKET_RESEARCHER,
    "research_lead_review": Role.RESEARCH_TEAM_LEAD,
    "writing_lead_outline": Role.WRITING_TEAM_LEAD,
    "technical_writer": Role.TECHNICAL_WRITER,
    "writing_lead_review": Role.WRITING_TEAM_LEAD,
    "summary_writer": Role.SUMMARY_WRITER,
    "citation_validation": None,
    "director_handoff": Role.RESEARCH_DIRECTOR,
    "finalize_report": None,
}

RESEARCHER_NODES = ["data_researcher", "market_researcher"]


def route_after_research_review(state: ResearchState) -> list[str] | str:
    """Retry (both researchers, in parallel) or move on to writing."""
    if state.get("research_route") == "retry":
        return list(RESEARCHER_NODES)
    return "writing_lead_outline"


def route_after_draft(state: ResearchState) -> str:
    """The first draft goes to review; a revised draft goes straight on."""
    return "summary_writer" if state.get("revision_count", 0) > 0 else "writing_lead_review"


def route_after_writing_review(state: ResearchState) -> str:
    return "technical_writer" if state.get("writing_route") == "revise" else "summary_writer"


def build_graph() -> Any:
    builder: StateGraph[ResearchState, RunContext, ResearchState, ResearchState] = StateGraph(
        ResearchState, context_schema=RunContext
    )
    builder.add_node("validate_input", nodes.validate_input)
    builder.add_node("director_plan", nodes.director_plan)
    builder.add_node("research_lead_assign", nodes.research_lead_assign)
    builder.add_node("data_researcher", nodes.make_researcher_node(Role.DATA_RESEARCHER))
    builder.add_node("market_researcher", nodes.make_researcher_node(Role.MARKET_RESEARCHER))
    builder.add_node("research_lead_review", nodes.research_lead_review)
    builder.add_node("writing_lead_outline", nodes.writing_lead_outline)
    builder.add_node("technical_writer", nodes.technical_writer)
    builder.add_node("writing_lead_review", nodes.writing_lead_review)
    builder.add_node("summary_writer", nodes.summary_writer)
    builder.add_node("citation_validation", nodes.citation_validation)
    builder.add_node("director_handoff", nodes.director_handoff)
    builder.add_node("finalize_report", nodes.finalize_report)

    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "director_plan")
    builder.add_edge("director_plan", "research_lead_assign")
    # Fan-out: both researchers run in the same super-step (in parallel).
    builder.add_edge("research_lead_assign", "data_researcher")
    builder.add_edge("research_lead_assign", "market_researcher")
    # Fan-in: the review runs once both researchers have reported.
    builder.add_edge("data_researcher", "research_lead_review")
    builder.add_edge("market_researcher", "research_lead_review")
    builder.add_conditional_edges(
        "research_lead_review",
        route_after_research_review,
        [*RESEARCHER_NODES, "writing_lead_outline"],
    )
    builder.add_edge("writing_lead_outline", "technical_writer")
    builder.add_conditional_edges(
        "technical_writer", route_after_draft, ["writing_lead_review", "summary_writer"]
    )
    builder.add_conditional_edges(
        "writing_lead_review", route_after_writing_review, ["technical_writer", "summary_writer"]
    )
    builder.add_edge("summary_writer", "citation_validation")
    builder.add_edge("citation_validation", "director_handoff")
    builder.add_edge("director_handoff", "finalize_report")
    builder.add_edge("finalize_report", END)
    return builder.compile()


def initial_state(
    *, question: str, context: str | None, input_urls: list[str], depth: str
) -> ResearchState:
    return {
        "question": question,
        "context": context,
        "input_urls": input_urls,
        "depth": depth,
        "research_outcomes": [],
        "partial_reasons": [],
    }
