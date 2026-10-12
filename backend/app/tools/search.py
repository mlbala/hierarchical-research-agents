"""Web search adapters.

``TavilySearch`` is a deliberately small adapter over Tavily's REST API so it
can be swapped for another provider by implementing ``SearchProvider``.
The API key is sent only in the Authorization header and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from app.core.errors import ConfigurationError, ProviderError

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


@dataclass(frozen=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    score: float | None
    published_date: str | None


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, max_results: int) -> list[SearchHit]: ...

    async def aclose(self) -> None: ...


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_tavily_results(payload: Any, max_results: int) -> list[SearchHit]:
    """Validate the provider response defensively; ignore malformed entries."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ProviderError(
            "The search provider returned an unexpected response.",
            provider="tavily",
            code="SEARCH_BAD_RESPONSE",
        )
    hits: list[SearchHit] = []
    for item in payload["results"]:
        if not isinstance(item, dict):
            continue
        url, title = item.get("url"), item.get("title")
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            continue
        content = item.get("content")
        snippet = content if isinstance(content, str) else ""
        score = item.get("score") if isinstance(item.get("score"), int | float) else None
        published = item.get("published_date")
        hits.append(
            SearchHit(
                url=url[:2048],
                title=(title if isinstance(title, str) else url)[:500],
                snippet=snippet[:1500],
                score=float(score) if score is not None else None,
                published_date=published if isinstance(published, str) else None,
            )
        )
        if len(hits) >= max_results:
            break
    return hits


class TavilySearch:
    name = "tavily"

    def __init__(
        self,
        api_key: SecretStr | None,
        *,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if api_key is None or not api_key.get_secret_value():
            raise ConfigurationError("TAVILY_API_KEY is required for live research.")
        self._api_key = api_key
        # trust_env=False: no ambient proxies or netrc credentials.
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, trust_env=False, follow_redirects=False
        )

    async def search(self, query: str, *, max_results: int) -> list[SearchHit]:
        body = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_published_date": True,
        }
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        try:
            response = await self._client.post(TAVILY_SEARCH_URL, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "The search provider timed out.", provider="tavily",
                code="SEARCH_TIMEOUT", retryable=True,
            ) from exc  # fmt: skip
        except httpx.TransportError as exc:
            raise ProviderError(
                "The search provider could not be reached.", provider="tavily",
                code="SEARCH_UNAVAILABLE", retryable=True,
            ) from exc  # fmt: skip

        status = response.status_code
        if status == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderError(
                    "The search provider returned invalid JSON.",
                    provider="tavily",
                    code="SEARCH_BAD_RESPONSE",
                ) from exc
            return parse_tavily_results(payload, max_results)
        if status in (401, 403):
            raise ProviderError(
                "The search provider rejected the API key.",
                provider="tavily",
                code="SEARCH_AUTH_FAILED",
            )
        if status in (432, 433):
            raise ProviderError(
                "The search provider usage limit was reached.",
                provider="tavily",
                code="SEARCH_QUOTA_EXCEEDED",
            )
        if status == 429:
            raise ProviderError(
                "The search provider rate-limited the request.",
                provider="tavily",
                code="SEARCH_RATE_LIMITED",
                retryable=True,
                retry_after=_retry_after(response),
            )
        if status >= 500:
            raise ProviderError(
                "The search provider had a server error.",
                provider="tavily",
                code="SEARCH_UNAVAILABLE",
                retryable=True,
            )
        raise ProviderError(
            "The search provider rejected the request.",
            provider="tavily",
            code="SEARCH_BAD_REQUEST",
        )

    async def aclose(self) -> None:
        await self._client.aclose()
