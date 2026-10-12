"""Safe public web page retrieval.

Guarantees (all enforced here, not in prompts):

* Only public http/https URLs on standard ports (``url_safety``).
* Each redirect target is re-validated; at most three redirects.
* DNS is resolved and validated at connect time (no rebinding window).
* No cookies, auth headers or ambient proxy settings: httpcore is used
  directly, so there is no cookie jar and environment proxies are ignored.
* Connect/read timeouts per operation plus an overall deadline.
* The body is streamed and aborted beyond ``fetch_max_bytes`` (also after
  decompression, preventing zip bombs).
* Only HTML and plain text are accepted; nothing is executed and no
  subresources are requested.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import re
import ssl
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urljoin

import certifi
import httpcore

from app.core.errors import AppError, UnsafeUrlError
from app.tools.extract import ExtractedDocument, extract_html, extract_plain_text
from app.tools.url_safety import Resolver, SafeNetworkBackend, system_resolver, validate_public_url

MAX_REDIRECTS = 3
USER_AGENT = b"ResearchHive/0.1 (self-hosted research assistant)"
_ALLOWED_TYPES = {"text/html": "html", "application/xhtml+xml": "html", "text/plain": "text"}
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_\-]+)""", re.IGNORECASE)


class FetchError(AppError):
    """Retrieval failed for a reason other than URL safety."""

    code = "FETCH_FAILED"
    http_status = 502
    safe_message = "The page could not be retrieved."


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    title: str
    publisher: str | None
    published_at: datetime | None
    text: str
    content_hash: str
    truncated: bool
    redirects: int
    byte_count: int
    content_type: str


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _header(response: httpcore.Response, name: bytes) -> str | None:
    for key, value in response.headers:
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return None


def _parse_content_type(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    parts = [p.strip() for p in value.split(";")]
    media = parts[0].lower()
    charset = None
    for param in parts[1:]:
        if param.lower().startswith("charset="):
            charset = param.split("=", 1)[1].strip("\"' ")
    return media, charset


def _valid_codec(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


class SafePageFetcher:
    """Fetches public pages with SSRF, size and type protections."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        max_chars: int,
        resolver: Resolver = system_resolver,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._max_chars = max_chars
        self._backend = SafeNetworkBackend(resolver=resolver, inner=network_backend)
        # Verified TLS against the Mozilla CA bundle; hostname checking stays on.
        self._ssl = ssl_context or ssl.create_default_context(cafile=certifi.where())

    @property
    def backend(self) -> SafeNetworkBackend:
        return self._backend

    async def fetch(self, url: str) -> FetchedPage:
        try:
            return await asyncio.wait_for(self._fetch(url), timeout=self._timeout)
        except TimeoutError as exc:
            raise FetchError(
                "The page took too long to respond.", code="FETCH_TIMEOUT", retryable=True
            ) from exc

    async def _fetch(self, url: str) -> FetchedPage:
        current = validate_public_url(url)
        timeouts = {
            "connect": self._timeout,
            "read": self._timeout,
            "write": self._timeout,
            "pool": self._timeout,
        }
        async with httpcore.AsyncConnectionPool(
            ssl_context=self._ssl,
            network_backend=self._backend,
            max_connections=2,
            http1=True,
            http2=False,
            retries=0,
        ) as pool:
            for redirects in range(MAX_REDIRECTS + 1):
                headers = [
                    (b"Host", current.host_header.encode("ascii")),
                    (b"User-Agent", USER_AGENT),
                    (b"Accept", b"text/html,application/xhtml+xml,text/plain;q=0.9"),
                    (b"Accept-Encoding", b"gzip, deflate, identity"),
                    (b"Accept-Language", b"en;q=0.9, *;q=0.5"),
                ]
                try:
                    async with pool.stream(
                        "GET", current.url, headers=headers, extensions={"timeout": timeouts}
                    ) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = _header(response, b"location")
                            if not location:
                                raise FetchError("Redirect without a Location header.")
                            if redirects >= MAX_REDIRECTS:
                                raise UnsafeUrlError(
                                    "Too many redirects.", code="TOO_MANY_REDIRECTS"
                                )
                            # Re-validate every hop before following it.
                            current = validate_public_url(urljoin(current.url, location))
                            continue
                        if response.status >= 400:
                            retryable = response.status in (408, 425, 429, 500, 502, 503, 504)
                            raise FetchError(
                                f"The page returned HTTP {response.status}.",
                                code=f"FETCH_HTTP_{response.status}",
                                retryable=retryable,
                            )
                        media, charset = _parse_content_type(_header(response, b"content-type"))
                        kind = _ALLOWED_TYPES.get(media or "")
                        if kind is None:
                            raise FetchError(
                                "Only HTML and plain-text pages are supported.",
                                code="UNSUPPORTED_CONTENT_TYPE",
                            )
                        body = await self._read_body(response)
                        document = await self._extract(body, kind, charset, current.host)
                        return FetchedPage(
                            requested_url=url,
                            final_url=current.url,
                            title=document.title or current.host,
                            publisher=document.publisher,
                            published_at=document.published_at,
                            text=document.text,
                            content_hash=content_hash(document.text),
                            truncated=document.truncated,
                            redirects=redirects,
                            byte_count=len(body),
                            content_type=media or "",
                        )
                except (httpcore.TimeoutException, httpcore.NetworkError) as exc:
                    raise FetchError(
                        "The page could not be reached.", code="FETCH_NETWORK_ERROR", retryable=True
                    ) from exc
                except httpcore.ProtocolError as exc:
                    raise FetchError("The server sent an invalid response.") from exc
        raise FetchError("Too many redirects.", code="TOO_MANY_REDIRECTS")

    async def _read_body(self, response: httpcore.Response) -> bytes:
        declared = _header(response, b"content-length")
        if declared and declared.isdigit() and int(declared) > self._max_bytes:
            raise FetchError("The page is larger than the download limit.", code="FETCH_TOO_LARGE")

        encoding = (_header(response, b"content-encoding") or "identity").lower()
        decompressor = None
        if encoding in ("gzip", "x-gzip"):
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decompressor = zlib.decompressobj()
        elif encoding not in ("identity", ""):
            raise FetchError("Unsupported content encoding.", code="UNSUPPORTED_ENCODING")

        received = 0
        chunks: list[bytes] = []
        size = 0
        async for raw_chunk in response.aiter_stream():
            received += len(raw_chunk)
            chunk = raw_chunk
            if received > self._max_bytes:
                raise FetchError("The page exceeded the download limit.", code="FETCH_TOO_LARGE")
            if decompressor is not None:
                try:
                    # max_length bounds output so a small compressed payload
                    # cannot expand beyond the limit.
                    chunk = decompressor.decompress(chunk, self._max_bytes - size + 1)
                except zlib.error as exc:
                    raise FetchError("The page could not be decoded.") from exc
                if decompressor.unconsumed_tail:
                    raise FetchError(
                        "The page exceeded the download limit.", code="FETCH_TOO_LARGE"
                    )
            size += len(chunk)
            if size > self._max_bytes:
                raise FetchError("The page exceeded the download limit.", code="FETCH_TOO_LARGE")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _extract(
        self, body: bytes, kind: str, charset: str | None, host: str
    ) -> ExtractedDocument:
        codec = _valid_codec(charset)
        if codec is None and kind == "html":
            sniffed = _META_CHARSET.search(body[:4096])
            codec = _valid_codec(sniffed.group(1).decode("ascii", "ignore") if sniffed else None)
        decoded = body.decode(codec or "utf-8", errors="replace")
        if kind == "html":
            # Parsing large documents is CPU-bound; keep the event loop free.
            return await asyncio.to_thread(
                extract_html, decoded, max_chars=self._max_chars, fallback_publisher=host
            )
        return extract_plain_text(
            decoded, max_chars=self._max_chars, fallback_title=host, fallback_publisher=host
        )
