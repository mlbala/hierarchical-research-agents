"""Small server-side LLM adapter.

The rest of the application depends only on ``LLMClient``; ``AnthropicLLM`` is
the live implementation and ``DemoLLM`` (agents/demo_llm.py) the offline one.

Design notes:

* SDK-level retries are disabled (``max_retries=0``). Retries happen in
  ``RunContext.call_llm`` so each attempt is counted against the run budget
  and the run deadline.
* Responses use structured outputs (``output_config.format``) and are then
  validated against our Pydantic model. Validation happens here rather than
  via ``messages.parse`` so token usage is still recorded when the output is
  malformed.
* Only short, safe messages leave this module. Provider error bodies, prompts
  and responses are never logged or surfaced to users.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

import anthropic
from anthropic import AsyncAnthropic, transform_schema
from pydantic import BaseModel, ValidationError

from app.agents.roles import Role
from app.core.config import Settings
from app.core.errors import ConfigurationError, ProviderError

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMRequest:
    role: Role
    task_key: str
    system: str
    prompt: str
    max_tokens: int


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    output: T
    usage: LLMUsage


class LLMOutputError(ProviderError):
    """The model answered, but the answer is unusable (malformed/truncated).

    Not retried as a transient failure; ``RunContext.call_llm`` makes at most
    one corrective re-request, which consumes budget like any other call.
    """

    def __init__(self, message: str, *, code: str, usage: LLMUsage, problems: list[str]) -> None:
        super().__init__(message, provider="llm", code=code, retryable=False, usage=usage)
        self.problems = problems


class LLMClient(Protocol):
    provider: str
    model: str

    async def generate[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> LLMResult[T]: ...

    async def aclose(self) -> None: ...


def validation_problems(exc: ValidationError, limit: int = 8) -> list[str]:
    """Field locations and messages only; never the offending model text."""
    problems = []
    for err in exc.errors()[:limit]:
        location = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        problems.append(f"{location}: {err.get('msg', 'invalid')}")
    return problems


def parse_structured_output[T: BaseModel](
    *, stop_reason: str | None, text: str, schema: type[T], usage: LLMUsage
) -> T:
    """Validate a model response against ``schema`` or raise a typed error."""
    if stop_reason == "refusal":
        raise ProviderError(
            "The model declined this request.",
            provider="llm",
            code="LLM_REFUSED",
            usage=usage,
        )
    if stop_reason == "max_tokens":
        raise LLMOutputError(
            "The model response was cut off before completion.",
            code="LLM_OUTPUT_TRUNCATED",
            usage=usage,
            problems=["response reached max_tokens"],
        )
    if not text.strip():
        raise LLMOutputError(
            "The model returned an empty response.",
            code="LLM_INVALID_OUTPUT",
            usage=usage,
            problems=["empty response"],
        )
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise LLMOutputError(
            "The model response did not match the required structure.",
            code="LLM_INVALID_OUTPUT",
            usage=usage,
            problems=validation_problems(exc),
        ) from exc


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def classify_anthropic_error(exc: anthropic.APIError) -> ProviderError:
    """Map SDK exceptions to retryable / permanent ``ProviderError``s.

    Most specific classes first, per the SDK's exception hierarchy.
    """
    p = "anthropic"
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderError(
            "The LLM provider rejected the API key.", provider=p, code="LLM_AUTH_FAILED"
        )
    if isinstance(exc, anthropic.NotFoundError):
        return ProviderError(
            "The configured model was not found.", provider=p, code="LLM_MODEL_NOT_FOUND"
        )
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderError(
            "The LLM provider rate-limited the request.",
            provider=p,
            code="LLM_RATE_LIMITED",
            retryable=True,
            retry_after=_retry_after(exc),
        )
    if isinstance(
        exc,
        anthropic.OverloadedError
        | anthropic.ServiceUnavailableError
        | anthropic.InternalServerError
        | anthropic.DeadlineExceededError,
    ):
        return ProviderError(
            "The LLM provider is temporarily unavailable.",
            provider=p,
            code="LLM_UNAVAILABLE",
            retryable=True,
            retry_after=_retry_after(exc),
        )
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError(
            "The LLM request timed out.", provider=p, code="LLM_TIMEOUT", retryable=True
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(
            "The LLM provider could not be reached.",
            provider=p,
            code="LLM_UNAVAILABLE",
            retryable=True,
        )
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code == 409:
            return ProviderError(
                "The LLM provider reported a conflict.",
                provider=p,
                code="LLM_UNAVAILABLE",
                retryable=True,
            )
        if exc.status_code >= 500:
            return ProviderError(
                "The LLM provider had a server error.",
                provider=p,
                code="LLM_UNAVAILABLE",
                retryable=True,
            )
        return ProviderError(
            "The LLM provider rejected the request.", provider=p, code="LLM_BAD_REQUEST"
        )
    return ProviderError("The LLM call failed.", provider=p, code="LLM_ERROR")


class AnthropicLLM:
    """Live adapter for the Anthropic Messages API."""

    provider = "anthropic"

    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None:
        key = settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise ConfigurationError("ANTHROPIC_API_KEY is required for live research.")
        if not settings.anthropic_model:
            raise ConfigurationError("ANTHROPIC_MODEL is required for live research.")
        self.model = settings.anthropic_model
        self._effort = settings.anthropic_effort
        self._fallback = settings.refusal_fallback_enabled
        self._client = client or AsyncAnthropic(
            api_key=key.get_secret_value(),
            max_retries=0,
            timeout=settings.llm_timeout_seconds,
        )

    async def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": transform_schema(schema)}
        }
        if self._effort:
            output_config["effort"] = self._effort
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
            "output_config": output_config,
        }
        if self._fallback:
            # Server-side refusal fallback for models that support it: a
            # declined request is retried on Anthropic's recommended model.
            params["betas"] = [REFUSAL_FALLBACK_BETA]
            params["fallbacks"] = "default"
        try:
            response = await self._client.beta.messages.create(**params)
        except anthropic.APIError as exc:
            raise classify_anthropic_error(exc) from exc

        usage = LLMUsage(
            input_tokens=int(response.usage.input_tokens or 0),
            output_tokens=int(response.usage.output_tokens or 0),
        )
        text = "".join(
            cast(str, getattr(block, "text", ""))
            for block in response.content
            if block.type == "text"
        )
        output = parse_structured_output(
            stop_reason=response.stop_reason, text=text, schema=schema, usage=usage
        )
        return LLMResult(output=output, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()
