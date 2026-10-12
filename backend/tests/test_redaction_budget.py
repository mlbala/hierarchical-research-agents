"""Secret redaction in logs/events/settings and shared execution budgets."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
import structlog
from pydantic import SecretStr

from app.core.config import RunLimits
from app.core.errors import BudgetExceededError
from app.core.logging import configure_logging, redact_text, sanitize_payload, strip_url_query
from app.core.security import RateLimiter, client_ip
from app.services.budget import RunBudget
from tests.conftest import make_settings

SECRETS = [
    "sk-ant-api03-SECRETSECRETSECRET",
    "tvly-dev-SECRETSECRETSECRET",
    "Bearer abcdefghijklmnop123456",
    "postgresql://avnadmin:hunter2@pg.example.aivencloud.com:12345/defaultdb",
    "password=hunter2",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_redact_text_removes_secret_patterns(secret: str) -> None:
    redacted = redact_text(f"failure: {secret} happened")
    assert "SECRETSECRET" not in redacted
    assert "hunter2" not in redacted
    assert "abcdefghijklmnop123456" not in redacted


def test_sensitive_keys_and_url_queries_are_sanitised() -> None:
    payload = sanitize_payload(
        {
            "api_key": "anything",
            "Authorization": "Bearer xyz",
            "pgpassword": "p",
            "url": "https://example.com/page?token=abc&id=1#frag",
            "input_tokens": 1234,
            "nested": {"cookie": "c", "note": "uses sk-ant-api03-SECRETSECRETSECRET"},
        }
    )
    assert payload["api_key"] == "[REDACTED]"
    assert payload["Authorization"] == "[REDACTED]"
    assert payload["pgpassword"] == "[REDACTED]"
    assert payload["url"] == "https://example.com/page"
    assert payload["input_tokens"] == 1234  # token *counts* are not secrets
    assert payload["nested"]["cookie"] == "[REDACTED]"
    assert "SECRET" not in payload["nested"]["note"]
    assert strip_url_query("https://u:p@h.example:8443/x?y=1") == "https://h.example:8443/x"


def test_structured_logs_never_contain_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log = structlog.get_logger("test")
    log.info(
        "provider.failure",
        api_key="sk-ant-api03-SECRETSECRETSECRET",
        url="https://api.example.com/v1?key=SECRETSECRET",
        detail="connect to postgresql://u:hunter2@db.example/x failed",
    )
    logging.getLogger("third.party").warning("token tvly-dev-SECRETSECRETSECRET leaked")
    out = capsys.readouterr().out
    assert "SECRETSECRET" not in out and "hunter2" not in out
    lines = [json.loads(line) for line in out.strip().splitlines()]
    assert lines[0]["event"] == "provider.failure"
    assert "timestamp" in lines[0] and lines[0]["level"] == "info"


def test_settings_never_expose_secrets() -> None:
    settings = make_settings(
        openai_api_key=SecretStr("sk-proj-SECRETSECRETSECRET"),
        anthropic_api_key=SecretStr("sk-ant-api03-SECRETSECRETSECRET"),
        tavily_api_key=SecretStr("tvly-dev-SECRETSECRETSECRET"),
        pgpassword=SecretStr("hunter2"),
    )
    assert "SECRETSECRET" not in repr(settings)
    assert "hunter2" not in repr(settings)
    snapshot = json.dumps(settings.settings_snapshot("standard"))
    assert "SECRETSECRET" not in snapshot and "hunter2" not in snapshot


def test_blank_secret_values_count_as_missing() -> None:
    settings = make_settings(anthropic_api_key="", tavily_api_key="  ", anthropic_model="")
    assert settings.anthropic_api_key is None
    assert settings.tavily_api_key is None
    assert settings.anthropic_model is None


# --- Budgets ------------------------------------------------------------------------

LIMITS = RunLimits(
    max_llm_calls=10,
    max_search_calls=3,
    max_fetch_calls=5,
    max_sources=4,
    research_retries=1,
    writing_revisions=1,
    deadline_seconds=60,
    queries_per_researcher=3,
    pages_per_researcher=5,
    max_findings_per_researcher=10,
)


async def test_budget_is_shared_safely_across_parallel_tasks() -> None:
    budget = RunBudget(LIMITS)

    async def worker() -> bool:
        try:
            await budget.reserve("llm")
        except BudgetExceededError:
            return False
        await asyncio.sleep(0)
        return True

    results = await asyncio.gather(*(worker() for _ in range(50)))
    assert sum(results) == 10
    assert budget.remaining("llm") == 0
    assert "llm" in budget.exhausted


async def test_budget_release_returns_unused_reservations() -> None:
    budget = RunBudget(LIMITS)
    for _ in range(4):
        await budget.reserve("sources")
    with pytest.raises(BudgetExceededError) as excinfo:
        await budget.reserve("sources")
    assert excinfo.value.resource == "sources" and excinfo.value.limit == 4
    await budget.release("sources")
    await budget.reserve("sources")
    assert budget.snapshot()["sources"] == {"used": 4, "limit": 4}


def test_quick_depth_has_tighter_limits() -> None:
    settings = make_settings()
    quick, standard = settings.limits_for("quick"), settings.limits_for("standard")
    assert standard.max_llm_calls == 20 and standard.max_search_calls == 12
    assert standard.max_fetch_calls == 24 and standard.max_sources == 20
    assert standard.research_retries == 1 and standard.writing_revisions == 1
    assert standard.deadline_seconds == 600
    for field in ("max_llm_calls", "max_search_calls", "max_fetch_calls", "max_sources"):
        assert getattr(quick, field) < getattr(standard, field)
    assert quick.research_retries == 0 and quick.writing_revisions == 0


# --- Rate limiting and client IP ------------------------------------------------------


def test_rate_limiter_uses_sliding_window() -> None:
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert limiter.allow("a", now=0) and limiter.allow("a", now=1)
    assert not limiter.allow("a", now=2)
    assert limiter.allow("b", now=2)
    assert limiter.allow("a", now=61.5)


def test_forwarded_headers_are_only_trusted_from_configured_proxies() -> None:
    scope = {
        "type": "http",
        "client": ("203.0.113.9", 1234),
        "headers": [(b"x-forwarded-for", b"198.51.100.1")],
    }
    assert client_ip(scope, []) == "203.0.113.9"
    proxied = {**scope, "client": ("10.0.0.2", 1234)}
    assert client_ip(proxied, ["10.0.0.2"]) == "198.51.100.1"
    spoofed = {
        **proxied,
        "headers": [(b"x-forwarded-for", b"1.1.1.1, 198.51.100.1")],
    }
    # Right-most untrusted hop wins; a spoofed left-most entry is ignored.
    assert client_ip(spoofed, ["10.0.0.2"]) == "198.51.100.1"
