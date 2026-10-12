"""Run-scoped evidence storage and retrieval.

Every read and write here is filtered by ``run_id``: agents can only see the
sources and findings of the run they belong to. Source URLs are normalised and
hashed so the same page is stored once per run. Ownership of referenced
sources is validated before findings or citations are written (in addition to
the composite foreign keys in the schema).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import CitationValidationError
from app.db.engine import Database
from app.db.models import Finding, Source, source_search_vector, utcnow, web_search_query

_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """Canonical form used for de-duplication (not for display)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        )
    )
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


_NON_WORD = re.compile(r"[^\w]+")
_ELLIPSIS = re.compile(r"\.\.\.|…|\[\s*\.\.\.\s*\]")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return " ".join(_NON_WORD.sub(" ", text).split())


def excerpt_in_source(excerpt: str, source_text: str) -> bool:
    """Deterministically check that a quoted excerpt really is in the source.

    Comparison ignores case, punctuation and whitespace. Excerpts joined with
    ellipses are checked fragment by fragment; every meaningful fragment must
    be present. This rejects paraphrased or invented "quotes".
    """
    haystack = _norm(source_text)
    checked = 0
    for fragment in _ELLIPSIS.split(excerpt):
        needle = _norm(fragment)
        if len(needle) < 8:
            continue
        if needle not in haystack:
            return False
        checked += 1
    return checked > 0


@dataclass(frozen=True)
class SourceUpsert:
    id: uuid.UUID
    created: bool
    upgraded: bool


@dataclass(frozen=True)
class NewFinding:
    source_id: uuid.UUID
    claim: str
    evidence_excerpt: str
    support_level: str


@dataclass(frozen=True)
class FindingView:
    id: uuid.UUID
    task_id: uuid.UUID
    source_id: uuid.UUID
    claim: str
    evidence_excerpt: str
    support_level: str
    source_title: str
    source_url: str
    source_kind: str
    source_publisher: str | None


class EvidenceStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def find_source_ids(self, run_id: uuid.UUID, urls: Iterable[str]) -> dict[str, uuid.UUID]:
        """Map already-stored URLs (by normalised hash) to their source IDs."""
        hashes = {url_hash(u): u for u in urls}
        if not hashes:
            return {}
        async with self._db.session() as session:
            rows = await session.execute(
                select(Source.normalized_url_hash, Source.id).where(
                    Source.run_id == run_id, Source.normalized_url_hash.in_(hashes)
                )
            )
            return {hashes[h]: sid for h, sid in rows.all()}

    async def upsert_source(
        self,
        run_id: uuid.UUID,
        *,
        url: str,
        title: str,
        publisher: str | None,
        published_at: datetime | None,
        content_text: str,
        content_hash: str,
        content_kind: str,
        metadata: dict[str, Any],
    ) -> SourceUpsert:
        """Insert a source once per (run, normalised URL).

        If the URL already exists as a snippet-only source and we now have the
        fetched page, the row is upgraded to ``fetched_page``.
        """
        digest = url_hash(url)
        values = {
            "id": uuid.uuid4(),
            "run_id": run_id,
            "url": url,
            "normalized_url_hash": digest,
            "title": title[:500],
            "publisher": publisher[:200] if publisher else None,
            "published_at": published_at,
            "retrieved_at": utcnow(),
            "content_hash": content_hash,
            "content_text": content_text,
            "content_kind": content_kind,
            "meta": metadata,
        }
        async with self._db.session() as session:
            inserted = await session.scalar(
                pg_insert(Source)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["run_id", "normalized_url_hash"])
                .returning(Source.id)
            )
            if inserted is not None:
                return SourceUpsert(id=inserted, created=True, upgraded=False)
            existing = await session.scalar(
                select(Source).where(Source.run_id == run_id, Source.normalized_url_hash == digest)
            )
            if existing is None:  # pragma: no cover - conflict implies a row exists
                raise CitationValidationError("Source de-duplication failed.")
            upgraded = existing.content_kind == "search_snippet" and content_kind == "fetched_page"
            if upgraded:
                existing.title = title[:500]
                existing.publisher = publisher[:200] if publisher else None
                existing.published_at = published_at or existing.published_at
                existing.retrieved_at = utcnow()
                existing.content_text = content_text
                existing.content_hash = content_hash
                existing.content_kind = content_kind
                existing.meta = {**existing.meta, **metadata, "upgraded_from_snippet": True}
            return SourceUpsert(id=existing.id, created=False, upgraded=upgraded)

    @staticmethod
    async def assert_sources_in_run(
        session: AsyncSession, run_id: uuid.UUID, source_ids: Iterable[uuid.UUID]
    ) -> None:
        """Service-layer ownership check: every source must belong to ``run_id``."""
        wanted = set(source_ids)
        if not wanted:
            return
        found = set(
            await session.scalars(
                select(Source.id).where(Source.run_id == run_id, Source.id.in_(wanted))
            )
        )
        if found != wanted:
            raise CitationValidationError(
                "A citation referenced a source that does not belong to this run."
            )

    async def insert_findings(
        self, run_id: uuid.UUID, task_id: uuid.UUID, findings: Sequence[NewFinding]
    ) -> list[uuid.UUID]:
        if not findings:
            return []
        async with self._db.session() as session:
            await self.assert_sources_in_run(session, run_id, (f.source_id for f in findings))
            rows = [
                Finding(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    task_id=task_id,
                    source_id=f.source_id,
                    claim=f.claim,
                    evidence_excerpt=f.evidence_excerpt,
                    support_level=f.support_level,
                    created_at=utcnow(),
                )
                for f in findings
            ]
            session.add_all(rows)
            return [r.id for r in rows]

    async def list_findings(
        self, run_id: uuid.UUID, ids: Iterable[uuid.UUID] | None = None
    ) -> list[FindingView]:
        query = (
            select(Finding, Source)
            .join(Source, (Source.id == Finding.source_id) & (Source.run_id == Finding.run_id))
            .where(Finding.run_id == run_id)
            .order_by(Finding.created_at, Finding.id)
        )
        if ids is not None:
            query = query.where(Finding.id.in_(list(ids)))
        async with self._db.session() as session:
            rows = (await session.execute(query)).all()
        return [
            FindingView(
                id=f.id,
                task_id=f.task_id,
                source_id=f.source_id,
                claim=f.claim,
                evidence_excerpt=f.evidence_excerpt,
                support_level=f.support_level,
                source_title=s.title,
                source_url=s.url,
                source_kind=s.content_kind,
                source_publisher=s.publisher,
            )
            for f, s in rows
        ]

    async def update_support_levels(self, run_id: uuid.UUID, levels: dict[uuid.UUID, str]) -> None:
        if not levels:
            return
        async with self._db.session() as session:
            for finding_id, level in levels.items():
                await session.execute(
                    update(Finding)
                    .where(Finding.run_id == run_id, Finding.id == finding_id)
                    .values(support_level=level)
                )

    async def get_sources(self, run_id: uuid.UUID, ids: Iterable[uuid.UUID]) -> list[Source]:
        async with self._db.session() as session:
            rows = await session.scalars(
                select(Source).where(Source.run_id == run_id, Source.id.in_(list(ids)))
            )
            return list(rows)

    async def count_sources(self, run_id: uuid.UUID) -> int:
        async with self._db.session() as session:
            return int(
                await session.scalar(
                    select(func.count()).select_from(Source).where(Source.run_id == run_id)
                )
                or 0
            )

    async def rank_sources(
        self, run_id: uuid.UUID, query: str, limit: int = 20
    ) -> list[tuple[uuid.UUID, float]]:
        """Run-scoped full-text relevance ranking of stored sources."""
        vector = source_search_vector()
        ts_query = web_search_query(query)
        async with self._db.session() as session:
            rows = await session.execute(
                select(Source.id, func.ts_rank(vector, ts_query).label("rank"))
                .where(Source.run_id == run_id, vector.op("@@")(ts_query))
                .order_by(func.ts_rank(vector, ts_query).desc())
                .limit(limit)
            )
            return [(row[0], float(row[1])) for row in rows.all()]
