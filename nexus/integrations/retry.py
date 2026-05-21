from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")


async def call_with_backoff(
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    jitter: float = 0.2,
    rate_limit_min_delay: float = 5.0,
    rate_limit_max_delay: float = 10.0,
    retryable: Callable[[Exception], bool] | None = None,
) -> T:
    for attempt in range(retries):
        try:
            return await operation()
        except Exception as exc:
            if retryable is not None and not retryable(exc):
                raise
            if attempt == retries - 1:
                raise
            delay = retry_delay(
                exc,
                attempt=attempt,
                base_delay=base_delay,
                jitter=jitter,
                rate_limit_min_delay=rate_limit_min_delay,
                rate_limit_max_delay=rate_limit_max_delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError("Retry loop exhausted unexpectedly.")


def retry_delay(
    exc: Exception,
    *,
    attempt: int,
    base_delay: float = 0.5,
    jitter: float = 0.2,
    rate_limit_min_delay: float = 5.0,
    rate_limit_max_delay: float = 10.0,
) -> float:
    """Return the delay before retrying a provider call.

    Ordinary transient failures use short exponential backoff. Rate limits use
    a longer floor so retries do not immediately collide with provider quotas.
    If the provider exposes a Retry-After value, respect it.
    """
    exponential = base_delay * (2 ** attempt) + random.uniform(0, jitter)
    if not is_rate_limit_error(exc):
        return exponential

    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return max(float(retry_after), exponential)

    lower = max(rate_limit_min_delay, 0.0)
    upper = max(rate_limit_max_delay, lower)
    return max(random.uniform(lower, upper), exponential)


def is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    message = str(exc).lower()
    return "429" in message or "rate limit" in message or "rate_limit" in message
