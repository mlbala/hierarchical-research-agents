"""Malformed model output, provider error classification and retry policy."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from app.agents.llm import (
    AnthropicLLM,
    LLMOutputError,
    LLMRequest,
    LLMUsage,
    classify_anthropic_error,
    parse_structured_output,
)
from app.agents.roles import Role
from app.core.errors import AppError, ConfigurationError, ProviderError
from app.core.retry import Deadline, RetryPolicy, backoff_delay, run_with_retries
from app.schemas.agents import DirectorHandoff, EvidenceReview, ResearcherOutput
from app.tools.search import TavilySearch, parse_tavily_results
from tests.conftest import make_settings

USAGE = LLMUsage(input_tokens=10, output_tokens=5)


def test_review_accepts_substantive_notes_but_rejects_unbounded_text() -> None:
    review = {"contradictions": ["x" * 1200], "summary": "s" * 2400, "decision": "sufficient"}
    output = EvidenceReview.model_validate(review)
    assert len(output.contradictions[0]) == 1200
    with pytest.raises(ValidationError):
        EvidenceReview.model_validate({**review, "contradictions": ["x" * 1201]})
    with pytest.raises(ValidationError):
        EvidenceReview.model_validate({**review, "summary": "s" * 2401})


# --- Structured output validation ----------------------------------------------


def test_valid_output_parses() -> None:
    text = json.dumps({"handoff_summary": "Done.", "confidence": "medium", "caveats": []})
    result = parse_structured_output(
        stop_reason="end_turn", text=text, schema=DirectorHandoff, usage=USAGE
    )
    assert result.confidence == "medium"


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        '{"handoff_summary": "x"}',  # missing required fields
        '{"handoff_summary": "x", "confidence": "certain", "caveats": []}',  # bad enum
        '{"handoff_summary": "x", "confidence": "low", "caveats": [], "extra": 1}',  # extra
        json.dumps({"handoff_summary": "x" * 5000, "confidence": "low", "caveats": []}),
        "",
    ],
)
def test_malformed_output_is_rejected_with_usage(text: str) -> None:
    with pytest.raises(LLMOutputError) as excinfo:
        parse_structured_output(
            stop_reason="end_turn", text=text, schema=DirectorHandoff, usage=USAGE
        )
    assert excinfo.value.code == "LLM_INVALID_OUTPUT"
    assert excinfo.value.usage == USAGE
    # Problems name fields only; the model's text is never echoed back.
    assert all("x" * 50 not in p for p in excinfo.value.problems)


def test_bad_source_handles_fail_validation() -> None:
    bad = {
        "findings": [
            {
                "source_ref": "../etc",
                "claim": "A claim long enough",
                "evidence_excerpt": "An excerpt long enough",
                "support_level": "supported",
                "is_estimate": False,
            }
        ]
    }
    with pytest.raises(LLMOutputError):
        parse_structured_output(
            stop_reason="end_turn", text=json.dumps(bad), schema=ResearcherOutput, usage=USAGE
        )


def test_refusal_and_truncation_are_distinct_errors() -> None:
    with pytest.raises(ProviderError) as refused:
        parse_structured_output(stop_reason="refusal", text="", schema=DirectorHandoff, usage=USAGE)
    assert refused.value.code == "LLM_REFUSED" and not refused.value.retryable
    with pytest.raises(LLMOutputError) as truncated:
        parse_structured_output(
            stop_reason="max_tokens", text='{"handoff', schema=DirectorHandoff, usage=USAGE
        )
    assert truncated.value.code == "LLM_OUTPUT_TRUNCATED"


# --- Provider error classification -------------------------------------------------


def _status_error(cls: type[anthropic.APIStatusError], status: int, **headers: str) -> Any:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, headers=headers, request=request)
    return cls("error", response=response, body=None)


@pytest.mark.parametrize(
    ("exc", "code", "retryable"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "LLM_AUTH_FAILED", False),
        (_status_error(anthropic.PermissionDeniedError, 403), "LLM_AUTH_FAILED", False),
        (_status_error(anthropic.NotFoundError, 404), "LLM_MODEL_NOT_FOUND", False),
        (_status_error(anthropic.BadRequestError, 400), "LLM_BAD_REQUEST", False),
        (_status_error(anthropic.RateLimitError, 429), "LLM_RATE_LIMITED", True),
        (_status_error(anthropic.InternalServerError, 500), "LLM_UNAVAILABLE", True),
        (_status_error(anthropic.OverloadedError, 529), "LLM_UNAVAILABLE", True),
        (_status_error(anthropic.ServiceUnavailableError, 503), "LLM_UNAVAILABLE", True),
        (
            anthropic.APITimeoutError(httpx2.Request("POST", "https://api.anthropic.com/")),
            "LLM_TIMEOUT",
            True,
        ),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "https://api.anthropic.com/")
            ),
            "LLM_UNAVAILABLE",
            True,
        ),
    ],
)
def test_anthropic_errors_are_classified(exc: Any, code: str, retryable: bool) -> None:
    error = classify_anthropic_error(exc)
    assert error.code == code
    assert error.retryable is retryable


def test_retry_after_header_is_honoured() -> None:
    error = classify_anthropic_error(
        _status_error(anthropic.RateLimitError, 429, **{"retry-after": "7"})
    )
    assert error.retry_after == 7.0


def test_live_adapter_requires_configuration() -> None:
    with pytest.raises(ConfigurationError):
        AnthropicLLM(make_settings(demo_mode=False))
    with pytest.raises(ConfigurationError):
        AnthropicLLM(make_settings(demo_mode=False, anthropic_api_key=SecretStr("k")))


async def test_anthropic_adapter_uses_structured_outputs_and_records_usage() -> None:
    captured: dict[str, Any] = {}

    class FakeMessages:
        async def create(self, **params: Any) -> Any:
            captured.update(params)
            return SimpleNamespace(
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=120, output_tokens=40),
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(
                            {"handoff_summary": "ok", "confidence": "high", "caveats": []}
                        ),
                    )
                ],
            )

    fake_client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    settings = make_settings(
        demo_mode=False,
        anthropic_api_key=SecretStr("sk-ant-test-key-000000"),
        anthropic_model="claude-opus-5-5",
    )
    llm = AnthropicLLM(settings, client=fake_client)  # type: ignore[arg-type]
    result = await llm.generate(
        LLMRequest(Role.RESEARCH_DIRECTOR, "k", "system", "prompt", 1000), DirectorHandoff
    )
    assert result.usage == LLMUsage(120, 40)
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["max_tokens"] == 1000
    # Refusal fallback is opted into for supported models.
    assert captured["fallbacks"] == "default"


# --- Retry policy -----------------------------------------------------------------


async def _no_sleep(_: float) -> None:
    return None


async def test_transient_errors_are_retried_and_consume_budget() -> None:
    attempts: list[int] = []
    reserved: list[int] = []

    async def operation(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 2:
            raise ProviderError("busy", provider="x", code="LLM_UNAVAILABLE", retryable=True)
        return "ok"

    async def before(attempt: int) -> None:
        reserved.append(attempt)

    result = await run_with_retries(
        operation,
        policy=RetryPolicy(max_retries=2),
        deadline=Deadline(60),
        before_attempt=before,
        sleep=_no_sleep,
    )
    assert result == "ok"
    assert attempts == [0, 1, 2]
    assert reserved == [0, 1, 2]  # every attempt (including retries) reserved budget


async def test_permanent_errors_are_not_retried() -> None:
    attempts = 0

    async def operation(_: int) -> None:
        nonlocal attempts
        attempts += 1
        raise ProviderError("bad key", provider="x", code="LLM_AUTH_FAILED")

    async def before(_: int) -> None:
        return None

    with pytest.raises(ProviderError):
        await run_with_retries(
            operation,
            policy=RetryPolicy(max_retries=5),
            deadline=Deadline(60),
            before_attempt=before,
            sleep=_no_sleep,
        )
    assert attempts == 1


async def test_retries_stop_at_the_limit() -> None:
    attempts = 0

    async def operation(_: int) -> None:
        nonlocal attempts
        attempts += 1
        raise ProviderError("busy", provider="x", code="LLM_UNAVAILABLE", retryable=True)

    async def before(_: int) -> None:
        return None

    with pytest.raises(ProviderError):
        await run_with_retries(
            operation,
            policy=RetryPolicy(max_retries=2),
            deadline=Deadline(60),
            before_attempt=before,
            sleep=_no_sleep,
        )
    assert attempts == 3


async def test_retry_guidance_never_exceeds_the_deadline() -> None:
    slept: list[float] = []

    async def operation(_: int) -> None:
        raise ProviderError(
            "slow down", provider="x", code="LLM_RATE_LIMITED", retryable=True, retry_after=30
        )

    async def before(_: int) -> None:
        return None

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    with pytest.raises(ProviderError):
        await run_with_retries(
            operation,
            policy=RetryPolicy(max_retries=3),
            deadline=Deadline(10),  # only 10 s left: a 30 s wait would overrun
            before_attempt=before,
            sleep=record_sleep,
        )
    assert slept == []


def test_backoff_has_jitter_and_respects_cap() -> None:
    policy = RetryPolicy(base_delay=1, max_delay=4)
    assert backoff_delay(0, policy, rand=lambda: 0.0) == 0.5
    assert backoff_delay(0, policy, rand=lambda: 1.0) == 1.5
    assert backoff_delay(10, policy, rand=lambda: 1.0) == 4
    assert backoff_delay(0, policy, retry_after=3) == 3
    assert backoff_delay(0, policy, retry_after=500) == policy.max_retry_after


# --- Search adapter classification ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "SEARCH_AUTH_FAILED", False),
        (432, "SEARCH_QUOTA_EXCEEDED", False),
        (429, "SEARCH_RATE_LIMITED", True),
        (502, "SEARCH_UNAVAILABLE", True),
        (400, "SEARCH_BAD_REQUEST", False),
    ],
)
async def test_tavily_errors_are_classified(status: int, code: str, retryable: bool) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(status, json={"detail": "nope"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    search = TavilySearch(SecretStr("tvly-secret-000000"), timeout_seconds=5, client=client)
    with pytest.raises(ProviderError) as excinfo:
        await search.search("query", max_results=3)
    assert excinfo.value.code == code
    assert excinfo.value.retryable is retryable
    assert seen["auth"] == "Bearer tvly-secret-000000"
    assert "tvly-secret" not in excinfo.value.safe_message


def test_tavily_results_are_validated_defensively() -> None:
    hits = parse_tavily_results(
        {
            "results": [
                {"url": "https://ok.example/a", "title": "A", "content": "snippet", "score": 0.9},
                {"url": "javascript:alert(1)", "title": "bad"},
                "garbage",
                {"url": "https://ok.example/b", "title": None, "content": 5},
            ]
        },
        max_results=5,
    )
    assert [h.url for h in hits] == ["https://ok.example/a", "https://ok.example/b"]
    assert hits[1].snippet == ""
    with pytest.raises(ProviderError):
        parse_tavily_results({"unexpected": True}, max_results=5)


def test_app_error_defaults_are_safe() -> None:
    error = AppError()
    assert error.code == "APP_ERROR" and error.retryable is False
