"""Readable-text extraction from HTML and plain text.

Pages are parsed as inert data: scripts, styles, frames and embedded objects
are removed and never executed, and no subresources are loaded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from bs4 import BeautifulSoup, Tag

_DROP_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object",
    "embed", "form", "nav", "footer", "aside", "button", "select", "input",
)  # fmt: skip
_DATE_META = (
    ("property", "article:published_time"),
    ("name", "article:published_time"),
    ("name", "date"),
    ("name", "dc.date"),
    ("name", "dc.date.issued"),
    ("name", "dcterms.date"),
    ("name", "citation_publication_date"),
    ("itemprop", "datePublished"),
)
_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t\r\f\v]+")


@dataclass(frozen=True)
class ExtractedDocument:
    title: str
    publisher: str | None
    published_at: datetime | None
    text: str
    truncated: bool


def parse_date(value: str | None) -> datetime | None:
    """Parse common ISO-8601 forms; return None when unsure (never guess)."""
    if not value:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        # Fall back to a bare YYYY-MM-DD prefix (e.g. "2025-03-14 10:00 CET").
        try:
            parsed = datetime.fromisoformat(candidate[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if not (1990 <= parsed.year <= datetime.now(UTC).year + 1):
        return None
    return parsed.astimezone(UTC)


def normalize_text(text: str, max_chars: int) -> tuple[str, bool]:
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    joined = "\n".join(lines)
    joined = _BLANK_LINES.sub("\n\n", joined).strip()
    if len(joined) > max_chars:
        return joined[:max_chars].rstrip() + " …", True
    return joined, False


def _meta(soup: BeautifulSoup, attr: str, value: str) -> str | None:
    tag = soup.find("meta", attrs={attr: value})
    if isinstance(tag, Tag):
        content = tag.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def extract_html(html: str, *, max_chars: int, fallback_publisher: str | None) -> ExtractedDocument:
    soup = BeautifulSoup(html, "lxml")

    title = _meta(soup, "property", "og:title") or (
        soup.title.get_text(" ", strip=True) if soup.title else ""
    )
    publisher = (
        _meta(soup, "property", "og:site_name")
        or _meta(soup, "name", "publisher")
        or _meta(soup, "name", "application-name")
        or fallback_publisher
    )
    published = None
    for attr, value in _DATE_META:
        published = parse_date(_meta(soup, attr, value))
        if published:
            break
    if published is None:
        time_tag = soup.find("time", attrs={"datetime": True})
        if isinstance(time_tag, Tag):
            dt = time_tag.get("datetime")
            published = parse_date(dt if isinstance(dt, str) else None)

    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()

    # Prefer the main content region when it carries most of the text.
    container: Tag | BeautifulSoup = soup
    for selector in ("main", "article"):
        region = soup.find(selector)
        if isinstance(region, Tag) and len(region.get_text(" ", strip=True)) > 500:
            container = region
            break

    text, truncated = normalize_text(container.get_text("\n"), max_chars)
    return ExtractedDocument(
        title=title[:500],
        publisher=publisher[:200] if publisher else None,
        published_at=published,
        text=text,
        truncated=truncated,
    )


def extract_plain_text(
    body: str, *, max_chars: int, fallback_title: str, fallback_publisher: str | None
) -> ExtractedDocument:
    text, truncated = normalize_text(body, max_chars)
    return ExtractedDocument(
        title=fallback_title[:500],
        publisher=fallback_publisher,
        published_at=None,
        text=text,
        truncated=truncated,
    )
