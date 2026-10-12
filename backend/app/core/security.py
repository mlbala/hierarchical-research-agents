"""HTTP hardening: request IDs, Host allowlist, CSRF-style mutation guard,
security headers, request logging and in-memory rate limiting.

These checks protect a *shared, unauthenticated* workspace from cross-site
request forgery, DNS-rebinding and accidental exposure. They are NOT user
authentication: anyone who can reach the server can use it.
"""

from __future__ import annotations

import ipaddress
import time
import uuid
from collections import deque
from collections.abc import Iterable

import structlog
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_response, unexpected_error_response

log = structlog.get_logger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "x-research-hive"
MAX_JSON_BODY_BYTES = 64 * 1024

# Built frontend assets are same-origin; no inline scripts, no remote images.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
)


def client_ip(scope: Scope, trusted_proxies: Iterable[str]) -> str:
    """Direct peer address, or the forwarded client when the peer is trusted.

    Forwarded headers are ignored unless the *direct* peer is an explicitly
    configured trusted proxy, so clients cannot spoof their address.
    """
    peer = (scope.get("client") or ("unknown", 0))[0]
    trusted = set(trusted_proxies)
    if peer not in trusted:
        return str(peer)
    forwarded = Headers(scope=scope).get("x-forwarded-for", "")
    # Walk right-to-left, skipping our own proxies; the first untrusted hop is the client.
    for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            break
        if hop not in trusted:
            return hop
    return str(peer)


class RateLimiter:
    """Sliding-window limiter keyed by client address (single process)."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 10_000:  # bound memory under abuse
            self._hits = {k: v for k, v in self._hits.items() if v}
        return True


class SecurityMiddleware:
    """Pure ASGI middleware (streaming-safe, so it works with SSE)."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: list[str],
        allowed_origins: list[str],
    ) -> None:
        self.app = app
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.allowed_origins = {o.lower().rstrip("/") for o in allowed_origins}

    def _host_allowed(self, headers: Headers) -> bool:
        if "*" in self.allowed_hosts:
            return True
        host = headers.get("host", "").lower()
        if host.startswith("["):
            hostname = host.split("]", 1)[0] + "]"
        else:
            hostname = host.rsplit(":", 1)[0] if ":" in host else host
        return hostname.strip("[]") in self.allowed_hosts or hostname in self.allowed_hosts

    def _mutation_rejection(self, headers: Headers) -> tuple[int, str, str] | None:
        """Checks for state-changing requests. Returns (status, code, message)."""
        origin = headers.get("origin")
        if origin is not None and origin.lower().rstrip("/") not in self.allowed_origins:
            return 403, "ORIGIN_NOT_ALLOWED", "Cross-origin requests are not allowed."
        if origin is None and headers.get("sec-fetch-site", "same-origin") not in (
            "same-origin",
            "none",
        ):
            return 403, "ORIGIN_NOT_ALLOWED", "Cross-site requests are not allowed."
        if headers.get(CSRF_HEADER) != "1":
            return 403, "CSRF_HEADER_MISSING", "Missing required request header."
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return 415, "UNSUPPORTED_MEDIA_TYPE", "Requests must use application/json."
        length = headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_JSON_BODY_BYTES):
            return 413, "PAYLOAD_TOO_LARGE", "The request body is too large."
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        headers = Headers(scope=scope)
        method = scope["method"].upper()
        path = scope.get("path", "")
        started = time.monotonic()
        status_holder = {"status": 500}
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_holder["status"] = message["status"]
                raw = list(message.get("headers", []))
                raw.extend(SECURITY_HEADERS)
                raw.append((b"x-request-id", request_id.encode()))
                if path.startswith("/api/"):
                    raw.append((b"cache-control", b"no-store"))
                message["headers"] = raw
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            rejection: tuple[int, str, str] | None = None
            if not self._host_allowed(headers):
                rejection = (400, "HOST_NOT_ALLOWED", "Invalid Host header.")
            elif method not in SAFE_METHODS and path.startswith("/api/"):
                rejection = self._mutation_rejection(headers)

            if rejection is not None:
                status, code, message = rejection
                log.warning("security.request_rejected", code=code, method=method, path=path)
                response = error_response(
                    status=status, code=code, message=message, request_id=request_id
                )
                await response(scope, receive, send_with_headers)
            else:
                try:
                    await self.app(scope, receive, send_with_headers)
                except Exception as exc:
                    if response_started:
                        # Headers already sent (e.g. mid-stream); nothing safe to add.
                        log.error(
                            "request.failed_mid_response",
                            error_type=type(exc).__name__,
                            path=path,
                        )
                    else:
                        response = unexpected_error_response(exc, request_id=request_id, path=path)
                        await response(scope, receive, send_with_headers)

            if path not in ("/api/v1/health/live",):
                log.info(
                    "http.request",
                    method=method,
                    path=path,
                    status=status_holder["status"],
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
