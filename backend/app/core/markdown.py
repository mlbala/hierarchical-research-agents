"""Server-side Markdown hardening for model-written report text.

The browser renders Markdown with raw HTML disabled and a safe-URL filter;
this module is the second, server-side layer so that the stored report and
the downloadable ``.md`` file are safe in *any* Markdown viewer:

* raw HTML is neutralised (``<`` becomes ``&lt;``), so tags and autolinks
  cannot execute or load anything;
* images are replaced with a text label (remote images are never loaded);
* links keep only http(s)/mailto/fragment targets; others become plain text;
* ``#``/``##`` headings in model text are demoted so the report structure is
  controlled by code.
"""

from __future__ import annotations

import re

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]*)(?:\s+\"[^\"]*\")?\s*\)")
_REF_DEFINITION = re.compile(r"(?m)^[ ]{0,3}\[[^\]]+\]:\s*(\S+).*$")
_TOP_HEADING = re.compile(r"(?m)^[ ]{0,3}#{1,2}(?=\s)")
_SAFE_URL = re.compile(r"^(https?://|mailto:|#)", re.IGNORECASE)


def is_safe_link(url: str) -> bool:
    return bool(_SAFE_URL.match(url.strip()))


def sanitize_markdown(text: str, *, demote_headings: bool = True) -> str:
    text = _CONTROL.sub("", text)
    text = text.replace("<", "&lt;")
    text = _IMAGE.sub(lambda m: f"(image omitted: {m.group(1).strip() or 'untitled'})", text)
    text = _INLINE_LINK.sub(lambda m: m.group(0) if is_safe_link(m.group(2)) else m.group(1), text)
    text = _REF_DEFINITION.sub(lambda m: m.group(0) if is_safe_link(m.group(1)) else "", text)
    if demote_headings:
        text = _TOP_HEADING.sub("###", text)
    return text.strip()


_MD_SPECIAL = re.compile(r"([\\`*_\[\]|<>])")


def escape_inline(text: str) -> str:
    """Escape text that must render literally (titles, publisher names)."""
    return _MD_SPECIAL.sub(r"\\\1", _CONTROL.sub("", text)).replace("\n", " ").strip()


def markdown_link_target(url: str) -> str | None:
    """Return a safe link target for http(s) URLs, else None."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return None
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C")
