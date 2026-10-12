"""Bounded retries with exponential backoff, jitter and a run deadline.

Retries are deliberately explicit (the provider SDKs' built-in retries are
disabled) so that every attempt is counted against the run's shared budgets
and no retry can push a run past its overall deadline.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.errors import AppError, ProviderError


class Deadline:
    """Monotonic wall-clock deadline for a run."""

    def __init__(self, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._expires_at = clock() + seconds

    def remaining(self) -> float:
        return max(0.0, self._expires_at - self._clock())

    def expired(self) -> bool:
        return self.remaining() <= 0


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 8.0
    # Upper bound on provider-supplied Retry-After guidance we will honour.
    max_retry_after: float = 60.0


def backoff_delay(
    attempt: int,
    policy: RetryPolicy,
    retry_after: float | None = None,
    *,
    rand: Callable[[], float] = random.random,
) -> float:
    """Delay before retry number ``attempt + 1``.

    Provider guidance (``Retry-After``) wins when present; otherwise use
    exponential backoff with +/-50% jitter so parallel tasks do not retry in
    lock-step.
    """
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, policy.max_retry_after)
    exponential = policy.base_delay * (2**attempt)
    return min(policy.max_delay, exponential * (0.5 + rand()))


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, AppError) and exc.retryable


async def run_with_retries[T](
    operation: Callable[[int], Awaitable[T]],
    *,
    policy: RetryPolicy,
    deadline: Deadline,
    before_attempt: Callable[[int], Awaitable[None]],
    on_retry: Callable[[int, AppError, float], Awaitable[None]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``operation`` with bounded retries.

    ``before_attempt`` runs before *every* attempt (including the first) and is
    where callers check cancellation and reserve budget, so retries consume the
    same budgets as first attempts. Non-retryable errors propagate immediately.
    If the backoff would exceed the remaining run deadline, the last error is
    raised instead of sleeping past the deadline.
    """
    attempt = 0
    while True:
        await before_attempt(attempt)
        try:
            return await operation(attempt)
        except AppError as exc:
            if not exc.retryable or attempt >= policy.max_retries:
                raise
            retry_after = exc.retry_after if isinstance(exc, ProviderError) else None
            delay = backoff_delay(attempt, policy, retry_after)
            if delay >= deadline.remaining():
                raise
            if on_retry is not None:
                await on_retry(attempt + 1, exc, delay)
            await sleep(delay)
            attempt += 1
