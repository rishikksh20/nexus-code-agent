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