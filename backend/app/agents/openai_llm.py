"""OpenAI adapter (Responses API with strict Structured Outputs).

Implements the same ``LLMClient`` interface as the Anthropic adapter, so the
LangGraph workflow, budgets, retries and validation are unchanged. Notes:

* SDK retries are disabled (``max_retries=0``); ``RunContext.call_llm`` retries
  so every attempt is counted against the run budget and deadline.
* Responses are requested with ``store=False`` so OpenAI does not retain them
  for later retrieval through the API.
* Output is constrained with a strict JSON schema derived from our Pydantic
  models and then validated again with Pydantic (``parse_structured_output``),
  so token usage is recorded even when the output is rejected.
* Running out of prepaid credit (HTTP 429 ``insufficient_quota``) is treated
  as a permanent configuration problem, not a rate limit to retry.
"""

from __future__ import annotations

import copy
from typing import Any

import openai
from openai import AsyncOpenAI, omit
from pydantic import BaseModel

from app.agents.llm import LLMRequest, LLMResult, LLMUsage, parse_structured_output
from app.core.config import Settings
from app.core.errors import ConfigurationError, ProviderError

# String length keywords are expressed as supported regex constraints as well
# as descriptions; descriptions alone cannot prevent overlong model output.
_DESCRIBED_KEYWORDS = ("minLength", "maxLength")
_DROPPED_KEYWORDS = ("default", "title")


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic model into an OpenAI strict-mode JSON schema.

    Strict mode requires every property to be listed in ``required`` (optional
    values are expressed as a union with ``null``), ``additionalProperties:
    false`` on every object, and only supported keywords.
    """
    schema = copy.deepcopy(model.model_json_schema())
    definitions: dict[str, Any] = schema.get("$defs", {})

    def visit(node: Any) -> Any:
        if isinstance(node, list):
            return [visit(item) for item in node]
        if not isinstance(node, dict):
            return node
        # A $ref with sibling keywords is inlined (strict mode wants bare refs).
        if "$ref" in node and len(node) > 1:
            target = definitions[node["$ref"].split("/")[-1]]
            merged = {**copy.deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}
            return visit(merged)
        out: dict[str, Any] = {}
        limits: list[str] = []
        for key, value in node.items():
            if key in _DROPPED_KEYWORDS:
                continue
            if key in _DESCRIBED_KEYWORDS:
                limits.append(f"{key}: {value}")
                continue
            if key in ("properties", "$defs"):
                out[key] = {name: visit(child) for name, child in value.items()}
            else:
                out[key] = visit(value)
        if limits:
            note = "{" + ", ".join(limits) + "}"
            out["description"] = f"{out['description']}\n\n{note}" if "description" in out else note
            if out.get("type") == "string" and "pattern" not in out:
                minimum = node.get("minLength", 0)
                maximum = node.get("maxLength", "")
                # Match newlines too, including Markdown and quoted excerpts.
                out["pattern"] = rf"^[\s\S]{{{minimum},{maximum}}}$"
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out.get("properties", {}))
        return out

    result: dict[str, Any] = visit(schema)
    return result


def _retry_after(exc: openai.APIStatusError) -> float | None:
    headers = exc.response.headers
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            return float(value) * scale
        except ValueError:
            continue
    return None


def classify_openai_error(exc: openai.APIError) -> ProviderError:
    """Map SDK exceptions to retryable / permanent ``ProviderError``s."""
    p = "openai"
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderError(
            "The LLM provider rejected the API key.", provider=p, code="LLM_AUTH_FAILED"
        )
    if isinstance(exc, openai.NotFoundError):
        return ProviderError(
            "The configured model was not found.", provider=p, code="LLM_MODEL_NOT_FOUND"
        )
    if isinstance(exc, openai.RateLimitError):
        if exc.code == "insufficient_quota":
            # Prepaid credit is used up: retrying cannot help.
            return ProviderError(
                "The OpenAI account has no remaining credit or quota.",
                provider=p,
                code="LLM_QUOTA_EXCEEDED",
            )
        return ProviderError(
            "The LLM provider rate-limited the request.",
            provider=p,
            code="LLM_RATE_LIMITED",
            retryable=True,
            retry_after=_retry_after(exc),
        )
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError(
            "The LLM request timed out.", provider=p, code="LLM_TIMEOUT", retryable=True
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(
            "The LLM provider could not be reached.",
            provider=p,
            code="LLM_UNAVAILABLE",
            retryable=True,
        )
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 409 or exc.status_code >= 500:
            return ProviderError(
                "The LLM provider is temporarily unavailable.",
                provider=p,
                code="LLM_UNAVAILABLE",
                retryable=True,
                retry_after=_retry_after(exc),
            )
        return ProviderError(
            "The LLM provider rejected the request.", provider=p, code="LLM_BAD_REQUEST"
        )
    return ProviderError("The LLM call failed.", provider=p, code="LLM_ERROR")


def _stop_reason(response: Any) -> str | None:
    """Translate Responses API status/refusals to the shared stop reasons."""
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                return "refusal"
    if getattr(response, "status", None) == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None)
        if reason == "content_filter":
            return "refusal"
        return "max_tokens"
    return None


class OpenAILLM:
    """Live adapter for the OpenAI Responses API."""

    provider = "openai"

    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        key = settings.openai_api_key
        if key is None or not key.get_secret_value():
            raise ConfigurationError("OPENAI_API_KEY is required for live research.")
        if not settings.openai_model:
            raise ConfigurationError("OPENAI_MODEL is required for live research.")
        self.model = settings.openai_model
        self._effort = settings.openai_reasoning_effort
        self._client = client or AsyncOpenAI(
            api_key=key.get_secret_value(),
            max_retries=0,
            timeout=settings.llm_timeout_seconds,
        )

    async def generate[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> LLMResult[T]:
        try:
            response = await self._client.responses.create(
                model=self.model,
                instructions=request.system,
                input=request.prompt,
                # Includes reasoning tokens on reasoning models.
                max_output_tokens=request.max_tokens,
                reasoning={"effort": self._effort} if self._effort else omit,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema.__name__,
                        "schema": strict_json_schema(schema),
                        "strict": True,
                    }
                },
                store=False,
            )
        except openai.APIError as exc:
            raise classify_openai_error(exc) from exc

        usage_data = getattr(response, "usage", None)
        usage = LLMUsage(
            input_tokens=int(getattr(usage_data, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage_data, "output_tokens", 0) or 0),
        )
        output = parse_structured_output(
            stop_reason=_stop_reason(response),
            text=response.output_text or "",
            schema=schema,
            usage=usage,
        )
        return LLMResult(output=output, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()
