"""SSRF defences: static URL checks, DNS validation, rebinding, redirects,
size/type limits. No real network access is used: DNS and sockets are faked.
"""

from __future__ import annotations

import gzip
import ipaddress
from collections.abc import Iterable

import httpcore
import pytest

from app.core.errors import UnsafeUrlError
from app.tools.fetch import FetchError, SafePageFetcher
from app.tools.url_safety import (
    SafeNetworkBackend,
    is_public_ip,
    resolve_public_addresses,
    validate_public_url,
)

PUBLIC_IP = "93.184.216.34"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<script>alert(1)</script>",
        "http://user:pass@example.com/",
        "http://example.com@evil.com/",
        "http://example.com:8080/",
        "https://example.com:8443/",
        "http://localhost/",
        "http://LOCALHOST./",
        "http://service.internal/",
        "http://printer.local/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://127.0.0.1/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://10.0.0.5/",
        "http://172.16.1.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/",
        "http://0.0.0.0/",
        "http://224.0.1.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://[fd00:ec2::254]/",
        "http://[fe80::1]/",
        "http://exa mple.com/",
        "http://example.com\\@evil/",
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_public_url(url)


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("https://Example.com/path?q=1#frag", "https://example.com/path?q=1"),
        ("http://example.com", "http://example.com/"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://[2606:4700:4700::1111]/", "http://[2606:4700:4700::1111]/"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
    ],
)
def test_accepts_public_urls(url: str, normalized: str) -> None:
    assert validate_public_url(url).url == normalized


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("::ffff:8.8.8.8", True),
        ("::ffff:192.168.0.1", False),
        ("64:ff9b::808:808", False),  # NAT64 is blocked outright
        ("2001::1", False),  # Teredo
        ("ff02::1", False),
        ("198.18.0.1", False),
    ],
)
def test_ip_classification(address: str, public: bool) -> None:
    assert is_public_ip(ipaddress.ip_address(address)) is public


def resolver_for(mapping: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return mapping[host]

    return resolve


async def test_dns_answers_must_all_be_public() -> None:
    resolver = resolver_for(
        {
            "good.example": [PUBLIC_IP, "2606:4700::1111"],
            "mixed.example": [PUBLIC_IP, "10.0.0.7"],
            "private.example": ["192.168.0.10"],
            "scoped.example": ["fe80::1%en0"],
        }
    )
    assert await resolve_public_addresses("good.example", 443, resolver) == [
        PUBLIC_IP,
        "2606:4700::1111",
    ]
    for host in ("mixed.example", "private.example", "scoped.example", "missing.example"):
        with pytest.raises(UnsafeUrlError):
            await resolve_public_addresses(host, 443, resolver)


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Fake socket layer: records what was dialled and replays canned bytes."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.dialled: list[tuple[str, int]] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dialled.append((host, port))
        return httpcore.AsyncMockStream([self.responses[host]])

    async def sleep(self, seconds: float) -> None:
        return None


async def test_connection_uses_the_validated_address_not_a_second_lookup() -> None:
    """DNS rebinding: a second lookup would return 127.0.0.1, but we never do one."""
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])

    async def rebinding_resolver(host: str, port: int) -> list[str]:
        return next(answers)

    inner = RecordingBackend({PUBLIC_IP: b""})
    backend = SafeNetworkBackend(resolver=rebinding_resolver, inner=inner)
    await backend.connect_tcp("rebind.example", 80)
    assert inner.dialled == [(PUBLIC_IP, 80)]
    # The next connection sees the private answer and is refused before dialling.
    with pytest.raises(UnsafeUrlError):
        await backend.connect_tcp("rebind.example", 80)
    assert inner.dialled == [(PUBLIC_IP, 80)]


async def test_non_standard_ports_are_refused_at_connect_time() -> None:
    backend = SafeNetworkBackend(resolver=resolver_for({"x.example": [PUBLIC_IP]}))
    with pytest.raises(UnsafeUrlError):
        await backend.connect_tcp("x.example", 6379)


def http_response(status: int, headers: dict[str, str], body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status} X".encode()]
    headers = {"Content-Length": str(len(body)), **headers}
    lines += [f"{k}: {v}".encode() for k, v in headers.items()]
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def make_fetcher(
    responses: dict[str, bytes], dns: dict[str, list[str]], *, max_bytes: int = 50_000
) -> tuple[SafePageFetcher, RecordingBackend]:
    inner = RecordingBackend(responses)
    fetcher = SafePageFetcher(
        timeout_seconds=5,
        max_bytes=max_bytes,
        max_chars=10_000,
        resolver=resolver_for(dns),
        network_backend=inner,
    )
    return fetcher, inner


async def test_fetches_html_and_strips_active_content() -> None:
    html = (
        b"<html><head><title>Example report</title>"
        b'<meta property="article:published_time" content="2025-03-14T10:00:00Z">'
        b"<script>alert('x')</script><style>p{}</style></head>"
        b"<body><nav>menu</nav><p>Useful finding: 46% of trips are short.</p>"
        b'<img src="https://tracker.example/p.png"></body></html>'
    )
    fetcher, inner = make_fetcher(
        {PUBLIC_IP: http_response(200, {"Content-Type": "text/html; charset=utf-8"}, html)},
        {"site.example": [PUBLIC_IP]},
    )
    page = await fetcher.fetch("http://site.example/report")
    assert page.title == "Example report"
    assert "46% of trips are short" in page.text
    assert "alert" not in page.text and "menu" not in page.text
    assert page.published_at is not None and page.published_at.year == 2025
    assert inner.dialled == [(PUBLIC_IP, 80)]  # no subresources were requested


async def test_redirect_to_private_address_is_blocked() -> None:
    fetcher, inner = make_fetcher(
        {PUBLIC_IP: http_response(302, {"Location": "http://169.254.169.254/latest"})},
        {"site.example": [PUBLIC_IP]},
    )
    with pytest.raises(UnsafeUrlError):
        await fetcher.fetch("http://site.example/start")
    assert inner.dialled == [(PUBLIC_IP, 80)]


async def test_redirect_to_hostname_resolving_privately_is_blocked() -> None:
    fetcher, inner = make_fetcher(
        {PUBLIC_IP: http_response(301, {"Location": "http://internal-looking.example/"})},
        {"site.example": [PUBLIC_IP], "internal-looking.example": ["10.1.2.3"]},
    )
    with pytest.raises(UnsafeUrlError):
        await fetcher.fetch("http://site.example/")
    assert all(host == PUBLIC_IP for host, _ in inner.dialled)


async def test_redirects_are_limited_to_three() -> None:
    fetcher, _inner = make_fetcher(
        {PUBLIC_IP: http_response(302, {"Location": "http://site.example/again"})},
        {"site.example": [PUBLIC_IP]},
    )
    with pytest.raises(UnsafeUrlError) as excinfo:
        await fetcher.fetch("http://site.example/")
    assert excinfo.value.code == "TOO_MANY_REDIRECTS"


@pytest.mark.parametrize(
    ("headers", "body", "code"),
    [
        ({"Content-Type": "application/pdf"}, b"%PDF-1.7", "UNSUPPORTED_CONTENT_TYPE"),
        ({}, b"no content type", "UNSUPPORTED_CONTENT_TYPE"),
        ({"Content-Type": "text/html"}, b"x" * 60_000, "FETCH_TOO_LARGE"),
        ({"Content-Type": "text/html", "Content-Encoding": "br"}, b"xx", "UNSUPPORTED_ENCODING"),
    ],
)
async def test_rejects_unsupported_or_oversized_responses(
    headers: dict[str, str], body: bytes, code: str
) -> None:
    fetcher, _ = make_fetcher(
        {PUBLIC_IP: http_response(200, headers, body)}, {"site.example": [PUBLIC_IP]}
    )
    with pytest.raises(FetchError) as excinfo:
        await fetcher.fetch("http://site.example/")
    assert excinfo.value.code == code


@pytest.mark.parametrize("media_type", ["text/markdown", "text/x-markdown"])
async def test_markdown_pages_are_read_as_text(media_type: str) -> None:
    """Docs sites may negotiate Markdown; the first H1 becomes the title."""
    body = b"---\ntitle: front matter\n---\n\n# Multi-stage builds #\n\nUse stages.\n\n# Next\n"
    fetcher, _ = make_fetcher(
        {PUBLIC_IP: http_response(200, {"Content-Type": f"{media_type}; charset=utf-8"}, body)},
        {"site.example": [PUBLIC_IP]},
    )
    page = await fetcher.fetch("http://site.example/docs")
    assert page.title == "Multi-stage builds"
    assert "Use stages." in page.text
    assert page.content_type == media_type


async def test_markdown_without_heading_is_titled_by_host() -> None:
    fetcher, _ = make_fetcher(
        {PUBLIC_IP: http_response(200, {"Content-Type": "text/markdown"}, b"## Only a section\n")},
        {"site.example": [PUBLIC_IP]},
    )
    page = await fetcher.fetch("http://site.example/docs")
    assert page.title == "site.example"


async def test_decompression_bomb_is_stopped() -> None:
    bomb = gzip.compress(b"\0" * 5_000_000)  # ~5 KB on the wire, 5 MB expanded
    fetcher, _ = make_fetcher(
        {
            PUBLIC_IP: http_response(
                200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, bomb
            )
        },
        {"site.example": [PUBLIC_IP]},
    )
    with pytest.raises(FetchError) as excinfo:
        await fetcher.fetch("http://site.example/")
    assert excinfo.value.code == "FETCH_TOO_LARGE"


async def test_http_errors_are_classified() -> None:
    fetcher, _ = make_fetcher(
        {PUBLIC_IP: http_response(503, {"Content-Type": "text/html"}, b"down")},
        {"site.example": [PUBLIC_IP]},
    )
    with pytest.raises(FetchError) as excinfo:
        await fetcher.fetch("http://site.example/")
    assert excinfo.value.retryable is True
    fetcher, _ = make_fetcher(
        {PUBLIC_IP: http_response(404, {"Content-Type": "text/html"}, b"missing")},
        {"site.example": [PUBLIC_IP]},
    )
    with pytest.raises(FetchError) as excinfo:
        await fetcher.fetch("http://site.example/")
    assert excinfo.value.retryable is False


async def test_no_cookies_or_credentials_are_sent() -> None:
    """The fetch request carries only our fixed headers."""
    sent: list[bytes] = []

    class CapturingStream(httpcore.AsyncMockStream):
        async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
            sent.append(buffer)

    class CapturingBackend(RecordingBackend):
        async def connect_tcp(self, host: str, port: int, *args: object, **kwargs: object):  # type: ignore[no-untyped-def,override]
            return CapturingStream(
                [http_response(200, {"Content-Type": "text/plain"}, b"hello world")]
            )

    fetcher = SafePageFetcher(
        timeout_seconds=5,
        max_bytes=10_000,
        max_chars=1_000,
        resolver=resolver_for({"site.example": [PUBLIC_IP]}),
        network_backend=CapturingBackend({}),
    )
    await fetcher.fetch("http://site.example/")
    request = b"".join(sent).lower()
    assert b"cookie" not in request
    assert b"authorization" not in request
    assert b"host: site.example" in request
    # Sites such as Wikipedia return 403 to bots without a contact URL.
    assert b"user-agent: researchhive/0.1 (+https://" in request
