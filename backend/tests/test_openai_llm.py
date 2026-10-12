"""OpenAI adapter: strict schemas, request shape, refusals, error mapping.

Uses an in-process fake client; no request reaches OpenAI.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import BaseModel, SecretStr

from app.agents.llm import LLMOutputError, LLMRequest, LLMUsage
from app.agents.openai_llm import OpenAILLM, classify_openai_error, strict_json_schema
from app.agents.roles import Role
from app.core.errors import ConfigurationError, ProviderError
from app.schemas import agents as agent_schemas
from app.schemas.agents import AgentOutput, DirectorHandoff, ResearchPlan
from tests.conftest import make_settings

OUTPUT_MODELS = [
    cls
    for cls in vars(agent_schemas).values()
    if isinstance(cls, type) and issubclass(cls, AgentOutput) and cls is not AgentOutput
]


def _walk(node: Any) -> Any:
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for child in value.values():
                    yield from _walk(child)
            else:
                yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


@pytest.mark.parametrize("model", OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_every_agent_schema_meets_openai_strict_rules(model: type[BaseModel]) -> None:
    schema = strict_json_schema(model)
    assert schema["type"] == "object" and "anyOf" not in schema
    for node in _walk(schema):
        assert not {"minLength", "maxLength", "default", "allOf"} & set(node)
        if node.get("type") == "object" or "properties" in node:
            assert node["additionalProperties"] is False
            assert node["required"] == list(node.get("properties", {}))
        if "$ref" in node:
            assert set(node) == {"$ref"}  # no sibling keywords next to a $ref


def test_length_limits_move_into_descriptions_and_optionals_become_nullable() -> None:
    schema = strict_json_schema(ResearchPlan)
    objective = schema["properties"]["objective"]
    assert "maxLength: 600" in objective["description"]
    context = schema["properties"]["context_focus"]
    assert {"type": "null"} in context["anyOf"]
    assert "context_focus" in schema["required"]  # optional value, required key
    # Property names that collide with dropped keywords are kept.
    assert "title" in strict_json_schema(agent_schemas.ReportOutline)["properties"]


def live_settings(**overrides: Any) -> Any:
    values = {
        "demo_mode": False,
        "llm_provider": "openai",
        "openai_api_key": SecretStr("sk-test-0000000000000000"),
        "openai_model": "gpt-6-luna",
    }
    values.update(overrides)
    return make_settings(**values)


class FakeResponses:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.params: dict[str, Any] = {}

    async def create(self, **params: Any) -> Any:
        self.params = params
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def fake_client(response: Any) -> Any:
    return SimpleNamespace(responses=FakeResponses(response))


def response_with(
    text: str | None = None,
    *,
    refusal: str | None = None,
    status: str = "completed",
    reason: str | None = None,
) -> Any:
    content = []
    if text is not None:
        content.append(SimpleNamespace(type="output_text", text=text))
    if refusal is not None:
        content.append(SimpleNamespace(type="refusal", refusal=refusal))
    return SimpleNamespace(
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output=[SimpleNamespace(type="message", content=content)],
        output_text=text or "",
        usage=SimpleNamespace(input_tokens=321, output_tokens=54),
    )


REQUEST = LLMRequest(Role.RESEARCH_DIRECTOR, "research_director.handoff", "sys", "prompt", 2000)
HANDOFF = json.dumps({"handoff_summary": "Done.", "confidence": "medium", "caveats": []})


async def test_request_uses_strict_structured_outputs_and_records_usage() -> None:
    client = fake_client(response_with(HANDOFF))
    llm = OpenAILLM(live_settings(openai_reasoning_effort="low"), client=client)
    result = await llm.generate(REQUEST, DirectorHandoff)

    params = client.responses.params
    assert params["model"] == "gpt-6-luna"
    assert params["instructions"] == "sys" and params["input"] == "prompt"
    assert params["max_output_tokens"] == 2000
    assert params["reasoning"] == {"effort": "low"}
    assert params["store"] is False
    fmt = params["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert fmt["name"] == "DirectorHandoff"
    assert result.output.confidence == "medium"
    assert result.usage == LLMUsage(321, 54)


async def test_refusals_and_truncation_are_reported() -> None:
    refused = OpenAILLM(live_settings(), client=fake_client(response_with(refusal="no")))
    with pytest.raises(ProviderError) as excinfo:
        await refused.generate(REQUEST, DirectorHandoff)
    assert excinfo.value.code == "LLM_REFUSED"
    assert excinfo.value.usage == LLMUsage(321, 54)

    cut = response_with('{"handoff_summary": "Do', status="incomplete", reason="max_output_tokens")
    truncated = OpenAILLM(live_settings(), client=fake_client(cut))
    with pytest.raises(LLMOutputError) as truncation:
        await truncated.generate(REQUEST, DirectorHandoff)
    assert truncation.value.code == "LLM_OUTPUT_TRUNCATED"

    malformed = OpenAILLM(live_settings(), client=fake_client(response_with('{"nope": 1}')))
    with pytest.raises(LLMOutputError) as invalid:
        await malformed.generate(REQUEST, DirectorHandoff)
    assert invalid.value.code == "LLM_INVALID_OUTPUT"


def test_live_openai_adapter_requires_configuration() -> None:
    with pytest.raises(ConfigurationError):
        OpenAILLM(live_settings(openai_api_key=None))
    with pytest.raises(ConfigurationError):
        OpenAILLM(live_settings(openai_model=None))


def _status_error(
    cls: type[openai.APIStatusError], status: int, body: Any = None, **headers: str
) -> Any:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx2.Response(status, headers=headers, request=request)
    return cls("error", response=response, body=body)


@pytest.mark.parametrize(
    ("exc", "code", "retryable"),
    [
        (_status_error(openai.AuthenticationError, 401), "LLM_AUTH_FAILED", False),
        (_status_error(openai.PermissionDeniedError, 403), "LLM_AUTH_FAILED", False),
        (_status_error(openai.NotFoundError, 404), "LLM_MODEL_NOT_FOUND", False),
        (_status_error(openai.BadRequestError, 400), "LLM_BAD_REQUEST", False),
        (_status_error(openai.RateLimitError, 429), "LLM_RATE_LIMITED", True),
        (
            _status_error(
                openai.RateLimitError,
                429,
                {"code": "insufficient_quota", "message": "You exceeded your current quota"},
            ),
            "LLM_QUOTA_EXCEEDED",
            False,
        ),
        (_status_error(openai.InternalServerError, 500), "LLM_UNAVAILABLE", True),
        (_status_error(openai.InternalServerError, 503), "LLM_UNAVAILABLE", True),
        (
            openai.APITimeoutError(httpx2.Request("POST", "https://api.openai.com/")),
            "LLM_TIMEOUT",
            True,
        ),
        (
            openai.APIConnectionError(request=httpx2.Request("POST", "https://api.openai.com/")),
            "LLM_UNAVAILABLE",
            True,
        ),
    ],
)
def test_openai_errors_are_classified(exc: Any, code: str, retryable: bool) -> None:
    error = classify_openai_error(exc)
    assert error.code == code
    assert error.retryable is retryable
    assert "sk-" not in error.safe_message


def test_rate_limit_retry_guidance_is_parsed() -> None:
    error = classify_openai_error(
        _status_error(openai.RateLimitError, 429, **{"retry-after-ms": "1500"})
    )
    assert error.retry_after == 1.5


async def test_sdk_errors_raised_by_the_client_are_mapped() -> None:
    quota = _status_error(openai.RateLimitError, 429, {"code": "insufficient_quota"})
    llm = OpenAILLM(live_settings(), client=fake_client(quota))
    with pytest.raises(ProviderError) as excinfo:
        await llm.generate(REQUEST, DirectorHandoff)
    assert excinfo.value.code == "LLM_QUOTA_EXCEEDED"
