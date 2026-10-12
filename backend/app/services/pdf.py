"""Offline PDF export of saved report Markdown; no browser or external fetches."""

from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import reportlab
from markdown_it import MarkdownIt
from markdown_it.token import Token
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

# ReportLab ships these redistributable fonts on every supported platform.
_FONT_DIR = Path(reportlab.__file__).parent / "fonts"
for _name, _file in (
    ("Hive", "Vera.ttf"),
    ("HiveBold", "VeraBd.ttf"),
    ("HiveItalic", "VeraIt.ttf"),
    ("HiveBoldItalic", "VeraBI.ttf"),
):
    pdfmetrics.registerFont(TTFont(_name, str(_FONT_DIR / _file)))
pdfmetrics.registerFontFamily(
    "Hive", normal="Hive", bold="HiveBold", italic="HiveItalic", boldItalic="HiveBoldItalic"
)


def _inline(tokens: list[Token]) -> str:
    """Translate only supported Markdown tokens to trusted Paragraph markup."""
    parts: list[str] = []
    links: list[bool] = []
    for token in tokens:
        if token.type in {"text", "code_inline"}:
            parts.append(escape(token.content))
        elif token.type in {"softbreak", "hardbreak"}:
            parts.append("<br/>" if token.type == "hardbreak" else " ")
        elif token.type in {"strong_open", "strong_close", "em_open", "em_close"}:
            tag = "b" if token.type.startswith("strong") else "i"
            parts.append(f"<{tag}>" if token.nesting == 1 else f"</{tag}>")
        elif token.type == "link_open":
            href = str(token.attrGet("href") or "")
            parsed = urlsplit(href)
            allowed = parsed.scheme.lower() in {"https", "http"} and bool(parsed.netloc)
            links.append(allowed)
            if allowed:
                parts.append(f'<a href="{escape(href, quote=True)}" color="#25636B">')
        elif token.type == "link_close" and links and links.pop():
            parts.append("</a>")
        # Raw HTML and image tokens are deliberately excluded.
    return "".join(parts)


def render_report_pdf(title: str, markdown: str) -> bytes:
    """Render existing report content with pagination, headings and source links."""
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=48,
        leftMargin=48,
        topMargin=58,
        bottomMargin=52,
        title=title,
        author="Research Hive",
    )
    body = ParagraphStyle(
        "Body",
        fontName="Hive",
        fontSize=10,
        leading=15,
        textColor=colors.HexColor("#243442"),
        spaceAfter=9,
        splitLongWords=True,
    )
    headings = {
        "h1": ParagraphStyle(
            "Title",
            parent=body,
            fontName="HiveBold",
            fontSize=23,
            leading=29,
            spaceAfter=18,
            keepWithNext=True,
        ),
        "h2": ParagraphStyle(
            "Section",
            parent=body,
            fontName="HiveBold",
            fontSize=14,
            leading=19,
            spaceBefore=15,
            spaceAfter=9,
            keepWithNext=True,
        ),
        "h3": ParagraphStyle(
            "Subsection",
            parent=body,
            fontName="HiveBold",
            fontSize=11,
            leading=16,
            spaceBefore=10,
            keepWithNext=True,
        ),
    }
    quote = ParagraphStyle(
        "Notice",
        parent=body,
        backColor=colors.HexColor("#EEF4F5"),
        borderPadding=9,
        leftIndent=10,
        rightIndent=10,
        spaceAfter=14,
    )
    story: list[Any] = []
    heading = ""
    quote_depth = 0
    lists: list[int | None] = []
    item_prefix = ""
    parser = MarkdownIt("commonmark", {"html": False})
    for token in parser.parse(markdown):
        if token.type == "heading_open":
            heading = token.tag
        elif token.type == "heading_close":
            heading = ""
        elif token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            lists.append(
                int(token.attrGet("start") or "1") if token.type == "ordered_list_open" else None
            )
        elif token.type in {"bullet_list_close", "ordered_list_close"}:
            lists.pop()
        elif token.type == "list_item_open":
            number = lists[-1]
            item_prefix = f"{number}. " if number is not None else "• "
            if number is not None:
                lists[-1] = number + 1
        elif token.type == "list_item_close":
            item_prefix = ""
        elif token.type == "inline":
            style = headings.get(heading, headings["h3"]) if heading else body
            if quote_depth:
                style = quote
            if lists:
                style = ParagraphStyle("List", parent=style, leftIndent=14 * len(lists))
            text = escape(item_prefix) + _inline(token.children or [])
            item_prefix = ""
            if text:
                story.append(Paragraph(text, style))
        elif token.type in {"fence", "code_block"}:
            # Paragraphs wrap long lines and split across pages safely.
            for line in token.content.splitlines():
                story.append(Paragraph(escape(line) or "&#160;", body))
        elif token.type == "hr":
            story.extend(
                [HRFlowable(width="100%", color=colors.HexColor("#DCE4E8")), Spacer(1, 10)]
            )

    def page_chrome(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont("Hive", 8)
        canvas.setFillColor(colors.HexColor("#627782"))
        canvas.drawString(48, A4[1] - 32, "RESEARCH HIVE  /  RESEARCH REPORT")
        canvas.drawString(48, 28, "Research Hive")
        canvas.drawRightString(A4[0] - 48, 28, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    return output.getvalue()
