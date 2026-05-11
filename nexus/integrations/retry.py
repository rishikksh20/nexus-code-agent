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
            delay = base_delay * (2 ** attempt) + random.uniform(0, jitter)
            await asyncio.sleep(delay)
    raise RuntimeError("Retry loop exhausted unexpectedly.")