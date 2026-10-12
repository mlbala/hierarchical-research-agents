"""The fixed seven-role hierarchy and tool permissions.

These tables are the single source of truth for who reports to whom and which
tools each role may use. They are enforced in code (``ToolPermissionError``)
and are not configurable at runtime; agents cannot create roles or tools.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    RESEARCH_DIRECTOR = "research_director"
    RESEARCH_TEAM_LEAD = "research_team_lead"
    DATA_RESEARCHER = "data_researcher"
    MARKET_RESEARCHER = "market_researcher"
    WRITING_TEAM_LEAD = "writing_team_lead"
    TECHNICAL_WRITER = "technical_writer"
    SUMMARY_WRITER = "summary_writer"


class Tool(StrEnum):
    WEB_SEARCH = "web_search"
    FETCH_PAGE = "fetch_page"
    RUN_EVIDENCE = "run_evidence"


# Each worker reports to its team lead; team leads report to the director.
ROLE_PARENT: dict[Role, Role | None] = {
    Role.RESEARCH_DIRECTOR: None,
    Role.RESEARCH_TEAM_LEAD: Role.RESEARCH_DIRECTOR,
    Role.WRITING_TEAM_LEAD: Role.RESEARCH_DIRECTOR,
    Role.DATA_RESEARCHER: Role.RESEARCH_TEAM_LEAD,
    Role.MARKET_RESEARCHER: Role.RESEARCH_TEAM_LEAD,
    Role.TECHNICAL_WRITER: Role.WRITING_TEAM_LEAD,
    Role.SUMMARY_WRITER: Role.WRITING_TEAM_LEAD,
}

RESEARCHERS: tuple[Role, ...] = (Role.DATA_RESEARCHER, Role.MARKET_RESEARCHER)

# Only researchers may browse. Writers and leads read run-scoped evidence only.
TOOL_PERMISSIONS: dict[Tool, frozenset[Role]] = {
    Tool.WEB_SEARCH: frozenset(RESEARCHERS),
    Tool.FETCH_PAGE: frozenset(RESEARCHERS),
    Tool.RUN_EVIDENCE: frozenset(Role),
}

ROLE_LABELS: dict[Role, str] = {
    Role.RESEARCH_DIRECTOR: "Research Director",
    Role.RESEARCH_TEAM_LEAD: "Research Team Lead",
    Role.DATA_RESEARCHER: "Data Researcher",
    Role.MARKET_RESEARCHER: "Market Researcher",
    Role.WRITING_TEAM_LEAD: "Writing Team Lead",
    Role.TECHNICAL_WRITER: "Technical Writer",
    Role.SUMMARY_WRITER: "Summary Writer",
}


def can_use(role: Role, tool: Tool) -> bool:
    return role in TOOL_PERMISSIONS[tool]
