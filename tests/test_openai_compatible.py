from __future__ import annotations

import json
from urllib import error

import pytest

from nexus.app import _build_model_client
from nexus.config import load_config
from nexus.integrations.fake_model import FakeModelClient
from nexus.integrations.openai_compatible import OpenAICompatibleModelClient
from nexus.models import Message, RuntimeRequest


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


@pytest.mark.asyncio
async def test_openai_compatible_client_posts_chat_completions(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["authorization"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeHTTPResponse(
            {
                "model": "demo-model",
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = OpenAICompatibleModelClient(
        api_base_url="https://example.test/v1",
        api_key="secret",
        provider_name="openai-compatible",
    )

    response = await client.complete(
        RuntimeRequest(
            model_name="demo-model",
            system_prompt="system",
            messages=(Message(role="user", content="hello"),),
        )
    )

    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["authorization"] == "Bearer secret"
    assert captured["body"] == {
        "model": "demo-model",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ],
        "tools": [],
        "temperature": 0.0,
    }
    assert response.message.content == "done"
    assert response.usage is not None
    assert response.usage.provider == "openai-compatible"
    assert response.usage.model == "demo-model"


@pytest.mark.asyncio
async def test_mistral_client_uses_mistral_env_credentials(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["authorization"] = req.get_header("Authorization")
        return _FakeHTTPResponse(
            {
                "model": "mistral-small-latest",
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setenv("MISTRAL_API_KEY", "mistral-secret")

    client = OpenAICompatibleModelClient(
        api_base_url="",
        provider_name="mistral",
    )

    response = await client.complete(
        RuntimeRequest(
            model_name="mistral-small-latest",
            system_prompt="system",
            messages=(Message(role="user", content="hello"),),
        )
    )

    assert captured["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert captured["authorization"] == "Bearer mistral-secret"
    assert response.usage is not None
    assert response.usage.provider == "mistral"


@pytest.mark.asyncio
async def test_openai_compatible_client_retries_transient_errors(monkeypatch):
    attempts = {"count": 0}

    def _fake_urlopen(req, timeout):
        del req, timeout
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise error.URLError("temporary failure")
        return _FakeHTTPResponse(
            {
                "model": "demo-model",
                "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
        )

    async def _no_sleep(delay):
        return None

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    client = OpenAICompatibleModelClient(
        api_base_url="https://example.test/v1",
        retries=2,
        base_delay=0.0,
        jitter=0.0,
    )

    response = await client.complete(
        RuntimeRequest(
            model_name="demo-model",
            system_prompt="system",
            messages=(Message(role="user", content="hello"),),
        )
    )

    assert attempts["count"] == 2
    assert response.message.content == "recovered"


def test_build_model_client_uses_provider_config(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global", cli_overrides={"provider": "fake"})

    fake_client = _build_model_client(config)

    assert isinstance(fake_client, FakeModelClient)

    live_config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={"provider": "openai-compatible", "api_base_url": "https://example.test/v1"},
    )

    live_client = _build_model_client(live_config)

    assert isinstance(live_client, OpenAICompatibleModelClient)

    mistral_config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={"provider": "mistral"},
    )

    mistral_client = _build_model_client(mistral_config)

    assert isinstance(mistral_client, OpenAICompatibleModelClient)
    assert mistral_client.provider_name == "mistral"
    assert mistral_client.api_base_url == "https://api.mistral.ai/v1"
