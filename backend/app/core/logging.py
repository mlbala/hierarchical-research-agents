"""Structured JSON logging with secret redaction.

Console logs are for operators and are kept separate from the user-visible
``run_events`` table. Both pass through the same sanitisation helpers so that
API keys, passwords, database URLs, authorization headers and URL query
strings never end up in either place.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import structlog
from structlog.types import EventDict, WrappedLogger

REDACTED = "[REDACTED]"

# Keys whose *values* are always secret, regardless of content. Token counts
# (``input_tokens``) are deliberately not matched.
_SENSITIVE_KEY = re.compile(
    r"(?i)(passw|secret|api[_-]?key|authorization|cookie|credential|"
    r"access[_-]?token|refresh[_-]?token|bearer|dsn|database[_-]?url|sslkey)"
)

# Secret-looking substrings that may appear inside free text (exception
# messages, provider errors). Order matters: URLs with credentials first.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(postgres(?:ql)?(?:\+\w+)?://)[^\s'\"]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)\b(https?://)[^/\s:@'\"]+:[^/\s@'\"]+@"), r"\1" + REDACTED + "@"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"tvly-[A-Za-z0-9_\-]{8,}"), REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{8,}"), "Bearer " + REDACTED),
    (
        re.compile(r"(?i)\b(x-api-key|api[_-]?key|password|passwd|pwd)\s*[=:]\s*[^\s,;'\"]+"),
        r"\1=" + REDACTED,
    ),
)

_URL_KEYS = frozenset({"url", "urls", "final_url", "redirect_url", "location", "path"})
_MAX_LOG_STRING = 2_000


def strip_url_query(url: str) -> str:
    """Drop query string, fragment and userinfo from a URL for safe logging."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[unparseable-url]"
    if not parts.scheme:
        # Not an absolute URL (e.g. a request path): just drop the query.
        return url.split("?", 1)[0].split("#", 1)[0]
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def redact_text(value: str) -> str:
    """Remove secret-looking substrings from free text."""
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def sanitize_value(key: str, value: Any, *, max_string: int = _MAX_LOG_STRING) -> Any:
    """Recursively sanitise a value destined for logs or event payloads."""
    if _SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        if key in _URL_KEYS:
            value = strip_url_query(value)
        value = redact_text(value)
        return value if len(value) <= max_string else value[:max_string] + "…"
    if isinstance(value, Mapping):
        return {str(k): sanitize_value(str(k), v, max_string=max_string) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        child_key = key if key in _URL_KEYS else ""
        return [sanitize_value(child_key, v, max_string=max_string) for v in list(value)[:100]]
    return value


def sanitize_payload(
    payload: Mapping[str, Any] | None, *, max_string: int = 1_000
) -> dict[str, Any]:
    """Sanitise a user-visible run-event payload."""
    if not payload:
        return {}
    return {str(k): sanitize_value(str(k), v, max_string=max_string) for k, v in payload.items()}


def _redact_processor(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """structlog processor applying ``sanitize_value`` to every field."""
    for key in list(event_dict.keys()):
        if key in {"timestamp", "level", "logger"}:
            continue
        event_dict[key] = sanitize_value(key, event_dict[key], max_string=8_000)
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    """Route both structlog and stdlib logging through one JSON renderer."""
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.format_exc_info,
        _redact_processor,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Third-party HTTP clients log full request URLs (including query
    # strings) at INFO/DEBUG; keep them quiet. Uvicorn's access log is
    # replaced by our own request log, which omits query strings.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "anthropic", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
