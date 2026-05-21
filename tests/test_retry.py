from __future__ import annotations

import pytest

from nexus.integrations.retry import call_with_backoff


@pytest.mark.asyncio
async def test_call_with_backoff_retries_until_success(monkeypatch):
    calls = {"count": 0}

    async def _operation():
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("retry")
        return "ok"

    async def _no_sleep(delay):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    result = await call_with_backoff(_operation, retries=3, base_delay=0.0, jitter=0.0)

    assert result == "ok"
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_call_with_backoff_does_not_retry_non_retryable_errors(monkeypatch):
    calls = {"count": 0}

    async def _operation():
        calls["count"] += 1
        raise ValueError("do not retry")

    async def _no_sleep(delay):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    with pytest.raises(ValueError, match="do not retry"):
        await call_with_backoff(
            _operation,
            retries=3,
            base_delay=0.0,
            jitter=0.0,
            retryable=lambda exc: isinstance(exc, RuntimeError),
        )

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_call_with_backoff_uses_rate_limit_delay_floor(monkeypatch):
    calls = {"count": 0}
    delays: list[float] = []

    class RateLimitError(RuntimeError):
        status_code = 429

    async def _operation():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RateLimitError("HTTP 429 rate limit")
        return "ok"

    async def _record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("asyncio.sleep", _record_sleep)
    monkeypatch.setattr("random.uniform", lambda lower, upper: lower)

    result = await call_with_backoff(
        _operation,
        retries=2,
        base_delay=0.0,
        jitter=0.0,
        retryable=lambda exc: isinstance(exc, RateLimitError),
    )

    assert result == "ok"
    assert delays == [5.0]


@pytest.mark.asyncio
async def test_call_with_backoff_respects_retry_after_for_rate_limits(monkeypatch):
    delays: list[float] = []

    class RateLimitError(RuntimeError):
        status_code = 429
        retry_after = 7.0

    async def _operation():
        if not delays:
            raise RateLimitError("HTTP 429 rate limit")
        return "ok"

    async def _record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("asyncio.sleep", _record_sleep)

    result = await call_with_backoff(
        _operation,
        retries=2,
        base_delay=0.0,
        jitter=0.0,
        retryable=lambda exc: isinstance(exc, RateLimitError),
    )

    assert result == "ok"
    assert delays == [7.0]
