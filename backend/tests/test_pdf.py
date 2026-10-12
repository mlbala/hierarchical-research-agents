"""PDF export preserves research content and handles untrusted Markdown safely."""

from io import BytesIO
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pypdf import PdfReader

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from app.services.pdf import render_report_pdf
from app.services.reports import ReportService


def test_pdf_preserves_sections_citations_notices_and_pagination() -> None:
    markdown = (
        """# Café research report

> **Demo mode:** fictional fixture data.
> **Partial report:** missing coverage.

## Executive summary
Useful **evidence** and *uncertainty* [1].

## Detailed analysis
"""
        + ("A long finding with a citation [1]. " * 45 + "\n\n") * 12
        + """
## Limitations and unresolved questions
- Limited coverage.

## Sources
1. [Primary source](https://example.org/source?x=1&y=2) — Retrieved 2026-10-12
"""
    )
    reader = PdfReader(BytesIO(render_report_pdf("Café research report", markdown)))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) > 1
    for expected in (
        "Café",
        "Demo mode",
        "Partial report",
        "Executive summary",
        "[1]",
        "Limitations and unresolved questions",
        "Sources",
        "Primary source",
    ):
        assert expected in text
    assert "Page 1" in text
    links = [
        annotation.get_object()["/A"]["/URI"]
        for page in reader.pages
        for annotation in page.get("/Annots", [])
        if "/A" in annotation.get_object()
    ]
    assert "https://example.org/source?x=1&y=2" in links


def test_pdf_treats_html_as_text_and_does_not_embed_external_images() -> None:
    content = render_report_pdf(
        "Safe export",
        """# Safe export
<script>alert('unsafe')</script>

![tracking image](https://example.org/pixel.png)

[File](file:///etc/passwd) & <b>literal text</b>

```text
<raw> & code
```
""",
    )
    reader = PdfReader(BytesIO(content))
    text = reader.pages[0].extract_text()
    assert "<b>literal text</b>" in text
    assert "<raw> & code" in text
    assert not reader.pages[0].get("/Annots")
    assert not reader.pages[0].images


async def test_pdf_endpoint_download_and_missing_report(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(Settings(_env_file=None, pgsslmode="disable"), start_worker=False)
    async with app.router.lifespan_context(app):
        saved_report = AsyncMock(
            return_value=('Unsafe / " title', "# Saved report\n\nVerified claim [1].")
        )
        monkeypatch.setattr(ReportService, "markdown_for_run", saved_report)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.get(f"/api/v1/runs/{uuid4()}/report.pdf")
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert response.headers["content-disposition"] == (
                'attachment; filename="unsafe-title.pdf"'
            )
            reader = PdfReader(BytesIO(response.content))
            assert "Verified claim [1]." in reader.pages[0].extract_text()
            saved_report.side_effect = NotFoundError("No report exists for this run.")
            missing = await client.get(f"/api/v1/runs/{uuid4()}/report.pdf")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "NOT_FOUND"
