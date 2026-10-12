"""Deterministic citation validation, excerpt verification and Markdown safety."""

from __future__ import annotations

import uuid

from app.core.markdown import escape_inline, markdown_link_target, sanitize_markdown
from app.services.citations import (
    draft_citation_issues,
    figures_in,
    markers_in,
    validate_and_renumber,
)
from app.services.evidence import excerpt_in_source, normalize_url, url_hash
from app.tools.demo import DEMO_SOURCES

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_markers_are_renumbered_by_first_appearance() -> None:
    result = validate_and_renumber(
        ["Answer [3] and [1].", "Finding [1, 3].", "Analysis [2]."],
        "Summary [3].",
        {1: A, 2: B, 3: C},
    )
    assert result.texts == ["Answer [1] and [2].", "Finding [2][1].", "Analysis [3]."]
    assert result.summary == "Summary [1]."
    assert result.citations == [(1, C), (2, A), (3, B)]
    assert result.removed_markers == []


def test_unknown_markers_are_removed_and_reported() -> None:
    result = validate_and_renumber(["Claim [1] and [9]."], "", {1: A})
    assert result.texts == ["Claim [1] and."]
    assert result.removed_markers == [9]
    assert result.citations == [(1, A)]


def test_summary_cannot_cite_sources_absent_from_body() -> None:
    result = validate_and_renumber(["Body [1]."], "Summary [1] [2] [7].", {1: A, 2: B})
    assert result.summary == "Summary [1]."
    assert result.removed_summary_markers == [2, 7]


def test_summary_figures_must_appear_in_body() -> None:
    result = validate_and_renumber(
        ["61% replaced trips [1]; 1,200 vouchers."], "61% and 75% of 1,200 riders [1].", {1: A}
    )
    assert result.unsupported_summary_figures == ["75%"]
    assert figures_in("Cost $600 in 2024 [3]") == {"600", "2024"}


def test_links_are_not_mistaken_for_citations() -> None:
    assert markers_in("See [1](https://example.com) and [2].") == [2]


def test_duplicate_numbers_for_one_source_collapse() -> None:
    result = validate_and_renumber(["X [1] Y [2]."], "", {1: A, 2: A})
    assert result.texts == ["X [1] Y [1]."]
    assert result.citations == [(1, A)]


def test_draft_issues_flag_uncited_findings() -> None:
    issues = draft_citation_issues("Answer.", ["Cited [1].", "Uncited."], "Analysis [5].", {1})
    assert "Key finding 2 has no citation marker." in issues
    assert any("[5]" in i for i in issues)
    assert "The direct answer has no citations." in issues


def test_excerpt_verification_rejects_paraphrase() -> None:
    source = "In the survey, 61% of voucher recipients reported replacing at least one car trip."
    assert excerpt_in_source("61% of voucher recipients reported replacing", source)
    assert excerpt_in_source("61 % of Voucher recipients — reported replacing", source)
    assert excerpt_in_source("In the survey ... reported replacing at least one car trip", source)
    assert not excerpt_in_source("most recipients replaced car trips", source)
    assert not excerpt_in_source("61% ... invented continuation of the quote", source)
    assert not excerpt_in_source("...", source)


def test_every_demo_fixture_excerpt_is_verbatim() -> None:
    for source in DEMO_SOURCES:
        for finding in source.findings:
            assert excerpt_in_source(finding.excerpt, source.text), (source.slug, finding.excerpt)


def test_url_normalization_deduplicates_tracking_variants() -> None:
    assert url_hash("https://Example.com/a/?utm_source=x&b=2&a=1#top") == url_hash(
        "https://example.com:443/a?a=1&b=2"
    )
    assert normalize_url("http://example.com") == "http://example.com/"
    assert url_hash("https://example.com/a?id=1") != url_hash("https://example.com/a?id=2")


# --- Markdown / XSS ---------------------------------------------------------------


def test_raw_html_is_neutralised() -> None:
    out = sanitize_markdown('Hi <script>alert(1)</script> <img src=x onerror="alert(1)">')
    assert "<script" not in out and "<img" not in out
    assert "&lt;script>" in out


def test_unsafe_links_lose_their_target() -> None:
    out = sanitize_markdown(
        "[click](javascript:alert(1)) [data](data:text/html;base64,xxx) "
        "[ok](https://example.com) [mail](mailto:a@b.example) [frag](#sources)"
    )
    assert "javascript:" not in out and "data:" not in out
    assert "[ok](https://example.com)" in out
    assert "[mail](mailto:a@b.example)" in out
    assert "[frag](#sources)" in out


def test_images_are_never_embedded() -> None:
    out = sanitize_markdown("![tracking pixel](https://tracker.example/p.png)")
    assert "tracker.example" not in out
    assert "(image omitted: tracking pixel)" in out


def test_reference_definitions_with_unsafe_targets_are_dropped() -> None:
    out = sanitize_markdown("[x][1]\n\n[1]: javascript:alert(1)\n[2]: https://ok.example")
    assert "javascript" not in out
    assert "[2]: https://ok.example" in out


def test_model_headings_cannot_override_report_structure() -> None:
    out = sanitize_markdown("# Fake title\n## Sources\n### Fine")
    assert out.splitlines() == ["### Fake title", "### Sources", "### Fine"]


def test_inline_escaping_and_link_targets() -> None:
    assert escape_inline("Title [1](javascript:x) <b>") == "Title \\[1\\](javascript:x) \\<b\\>"
    assert markdown_link_target("javascript:alert(1)") is None
    assert markdown_link_target("https://ex.com/a b(c)") == "https://ex.com/a%20b%28c%29"
