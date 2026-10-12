"""Deterministic citation validation and renumbering.

Writers cite with provisional ``[n]`` markers taken from a numbered source list
built in code. Before a report is saved this module:

* normalises grouped markers (``[1, 2]`` -> ``[1][2]``);
* removes markers that do not map to a provisional source (and reports them);
* renumbers the remaining markers ``1..k`` in order of first appearance so the
  final report has stable, gap-free citations;
* rejects summary markers that do not appear in the report body;
* flags figures in the executive summary that the body does not contain.

Run ownership of the cited sources is checked against the database by the
caller (``EvidenceStore.assert_sources_in_run``). Note that a valid citation
proves only that the source exists and belongs to the run; whether it
*supports* the claim is judged by the research review.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

# A citation marker: [12] not followed by "(" (which would be a Markdown link)
# and not a footnote/checkbox such as [^1] or [x].
MARKER = re.compile(r"\[(\d{1,3})\](?!\()")
_GROUPED = re.compile(r"\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})+)\]")
_FIGURE = re.compile(r"(?<![\w\[])(\d[\d,]*(?:\.\d+)?%?)(?![\w\]])")


def normalize_grouped_markers(text: str) -> str:
    return _GROUPED.sub(
        lambda m: "".join(f"[{n.strip()}]" for n in re.split(r"[,;]", m.group(1))), text
    )


def markers_in(text: str) -> list[int]:
    return [int(n) for n in MARKER.findall(normalize_grouped_markers(text))]


def figures_in(text: str) -> set[str]:
    """Numeric tokens outside citation markers (e.g. '61%', '1,200', '0.4')."""
    stripped = MARKER.sub(" ", normalize_grouped_markers(text))
    return {m.group(1).rstrip(".,") for m in _FIGURE.finditer(stripped) if len(m.group(1)) > 1}


@dataclass
class CitationResult:
    texts: list[str]
    summary: str
    # Final citation number -> source ID, in order of first appearance.
    citations: list[tuple[int, uuid.UUID]]
    removed_markers: list[int] = field(default_factory=list)
    removed_summary_markers: list[int] = field(default_factory=list)
    unsupported_summary_figures: list[str] = field(default_factory=list)


def validate_and_renumber(
    body_texts: Sequence[str], summary: str, provisional: dict[int, uuid.UUID]
) -> CitationResult:
    """Validate markers against ``provisional`` and renumber by first use.

    ``body_texts`` are processed in document order (direct answer, key
    findings, analysis). Two provisional numbers mapping to the same source
    collapse to one final number.
    """
    final_for_source: dict[uuid.UUID, int] = {}
    removed: list[int] = []

    def renumber(match: re.Match[str]) -> str:
        number = int(match.group(1))
        source_id = provisional.get(number)
        if source_id is None:
            removed.append(number)
            return ""
        if source_id not in final_for_source:
            final_for_source[source_id] = len(final_for_source) + 1
        return f"[{final_for_source[source_id]}]"

    texts = [_tidy(MARKER.sub(renumber, normalize_grouped_markers(t))) for t in body_texts]

    removed_summary: list[int] = []

    def renumber_summary(match: re.Match[str]) -> str:
        number = int(match.group(1))
        source_id = provisional.get(number)
        if source_id is None or source_id not in final_for_source:
            removed_summary.append(number)
            return ""
        return f"[{final_for_source[source_id]}]"

    new_summary = _tidy(MARKER.sub(renumber_summary, normalize_grouped_markers(summary)))
    body_figures: set[str] = set()
    for text in texts:
        body_figures |= figures_in(text)
    unsupported = sorted(figures_in(new_summary) - body_figures)

    citations = sorted(((n, sid) for sid, n in final_for_source.items()), key=lambda x: x[0])
    return CitationResult(
        texts=texts,
        summary=new_summary,
        citations=citations,
        removed_markers=sorted(set(removed)),
        removed_summary_markers=sorted(set(removed_summary)),
        unsupported_summary_figures=unsupported,
    )


def _tidy(text: str) -> str:
    # Removing a marker can leave "word ." or double spaces behind.
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def draft_citation_issues(
    direct_answer: str, key_findings: Sequence[str], analysis: str, valid: set[int]
) -> list[str]:
    """Automated checks the writing lead sees alongside its own review."""
    issues: list[str] = []
    all_markers = markers_in(direct_answer) + markers_in(analysis)
    for index, statement in enumerate(key_findings, start=1):
        found = markers_in(statement)
        all_markers += found
        if not found:
            issues.append(f"Key finding {index} has no citation marker.")
    unknown = sorted({m for m in all_markers if m not in valid})
    if unknown:
        issues.append(f"Unknown citation markers used: {', '.join(f'[{m}]' for m in unknown)}.")
    if not markers_in(analysis):
        issues.append("The detailed analysis has no citations.")
    if not markers_in(direct_answer):
        issues.append("The direct answer has no citations.")
    return issues
