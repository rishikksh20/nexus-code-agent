from __future__ import annotations

import json
import logging
from io import BytesIO
from urllib import error

import pytest

from nexus.app import _build_model_client
from nexus.config import load_config
from nexus.integrations.anthropic import AnthropicAdapter, AnthropicModelClient
from nexus.integrations.cohere import (
    CohereAdapter,
    CohereModelClient,
    _MAX_COHERE_TOOL_RESULT_CHARS,
    _TOOL_RESULT_TRUNCATION_MARKER,
    _chat_url as cohere_chat_url,
)
from nexus.integrations.fake_model import FakeModelClient
from nexus.integrations.gemini import GeminiAdapter, GeminiModelClient
from nexus.integrations.ollama import OllamaModelClient
from nexus.integrations.openai_compatible import OpenAICompatibleAdapter, OpenAICompatibleModelClient
from nexus.models import Message, RuntimeRequest, StreamEventType, ToolCall


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeStreamingHTTPResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(line.encode("utf-8") for line in self._lines)

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
        captured["user_agent"] = req.get_header("User-agent")
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
    assert captured["user_agent"] == "Nexus/0.1 (OpenAI-compatible client)"
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


@pytest.mark.asyncio
async def test_openai_compatible_client_waits_on_http_429(monkeypatch):
    attempts = {"count": 0}
    delays: list[float] = []

    def _fake_urlopen(req, timeout):
        del req, timeout
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise error.HTTPError(
                "https://example.test/v1/chat/completions",
                429,
                "rate limited",
                {"Retry-After": "6"},
                BytesIO(b'{"error":"slow down"}'),
            )
        return _FakeHTTPResponse(
            {
                "model": "demo-model",
                "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}],
            }
        )

    async def _record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("asyncio.sleep", _record_sleep)

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
    assert delays == [6.0]
    assert response.message.content == "recovered"


@pytest.mark.asyncio
async def test_openai_compatible_stream_accumulates_partial_tool_calls_without_usage(monkeypatch):
    lines = [
        'data: {"model":"demo-model","choices":[{"delta":{"content":"checking "}}]}\n',
        (
            'data: {"model":"demo-model","choices":[{"delta":{"tool_calls":'
            '[{"index":0,"id":"call-1","function":{"name":"read_"}}]}}]}\n'
        ),
        (
            'data: {"model":"demo-model","choices":[{"delta":{"tool_calls":'
            '[{"index":0,"function":{"name":"file","arguments":"{\\"path\\":"}}]}}]}\n'
        ),
        (
            'data: {"model":"demo-model","choices":[{"delta":{"tool_calls":'
            '[{"index":0,"function":{"arguments":"\\"README.md\\"}"}}]},'
            '"finish_reason":"tool_calls"}]}\n'
        ),
        "data: [DONE]\n",
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = OpenAICompatibleModelClient(
        api_base_url="https://example.test/v1",
        api_key="secret",
        provider_name="openai-compatible",
    )

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="demo-model",
                system_prompt="system",
                messages=(Message(role="user", content="read README"),),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in events] == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.TOOL_CALL_COMPLETE,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[0].text_delta is not None
    assert events[0].text_delta.content == "checking "
    assert events[1].tool_call == ToolCall("call-1", "read_file", {"path": "README.md"})
    assert events[2].finish_reason == "tool_calls"
    assert events[2].usage is None


@pytest.mark.asyncio
async def test_openai_compatible_stream_accepts_full_message_chunks(monkeypatch):
    lines = [
        (
            'data:{"model":"command-demo","choices":[{"message":{"role":"assistant",'
            '"content":"done"},"finish_reason":"stop"}]}\n'
        ),
        "data: [DONE]\n",
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = OpenAICompatibleModelClient(
        api_base_url="https://api.cohere.ai/compatibility/v1",
        api_key="secret",
        provider_name="openai-compatible",
    )

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(Message(role="user", content="hello"),),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in events] == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[0].text_delta is not None
    assert events[0].text_delta.content == "done"
    assert events[1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_openai_compatible_stream_accepts_full_message_tool_calls(monkeypatch):
    lines = [
        (
            'data: {"model":"command-demo","choices":[{"message":{"role":"assistant",'
            '"tool_calls":[{"id":"call-1","type":"function","function":{"name":"read_file",'
            '"arguments":"{\\"path\\":\\"README.md\\"}"}}]},"finish_reason":"tool_calls"}]}\n'
        ),
        "data: [DONE]\n",
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = OpenAICompatibleModelClient(
        api_base_url="https://api.cohere.ai/compatibility/v1",
        api_key="secret",
        provider_name="openai-compatible",
    )

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(Message(role="user", content="read README"),),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in events] == [
        StreamEventType.TOOL_CALL_COMPLETE,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[0].tool_call == ToolCall("call-1", "read_file", {"path": "README.md"})
    assert events[1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_openai_compatible_stream_accumulates_reasoning_content(monkeypatch):
    lines = [
        'data: {"model":"deepseek-v4-pro","choices":[{"delta":{"reasoning_content":"Need "}}]}\n',
        'data: {"model":"deepseek-v4-pro","choices":[{"delta":{"reasoning_content":"a file."}}]}\n',
        (
            'data: {"model":"deepseek-v4-pro","choices":[{"delta":{"tool_calls":'
            '[{"index":0,"id":"call-1","function":{"name":"read_file","arguments":"{}"}}]},'
            '"finish_reason":"tool_calls"}]}\n'
        ),
        "data: [DONE]\n",
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = OpenAICompatibleModelClient(
        api_base_url="https://api.deepseek.com/v1",
        api_key="secret",
        provider_name="openai-compatible",
    )

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="deepseek-v4-pro",
                system_prompt="system",
                messages=(Message(role="user", content="read README"),),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in events] == [
        StreamEventType.TOOL_CALL_COMPLETE,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[1].reasoning_content == "Need a file."


@pytest.mark.asyncio
async def test_openai_compatible_stream_accepts_reasoning_alias(monkeypatch):
    lines = [
        'data: {"model":"deepseek/deepseek-v4-pro","choices":[{"delta":{"reasoning":"Need "}}]}\n',
        'data: {"model":"deepseek/deepseek-v4-pro","choices":[{"delta":{"reasoning":"a file."}}]}\n',
        (
            'data: {"model":"deepseek/deepseek-v4-pro","choices":[{"delta":{"tool_calls":'
            '[{"index":0,"id":"call-1","function":{"name":"read_file","arguments":"{}"}}]},'
            '"finish_reason":"tool_calls"}]}\n'
        ),
        "data: [DONE]\n",
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = OpenAICompatibleModelClient(
        api_base_url="https://openrouter.ai/api/v1",
        api_key="secret",
        provider_name="openai-compatible",
    )

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="deepseek/deepseek-v4-pro",
                system_prompt="system",
                messages=(Message(role="user", content="read README"),),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in events] == [
        StreamEventType.TOOL_CALL_COMPLETE,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[1].reasoning_content == "Need a file."


@pytest.mark.asyncio
async def test_openai_compatible_stream_falls_back_when_provider_stream_is_empty(monkeypatch):
    requests: list[dict[str, object]] = []

    def _fake_urlopen(req, timeout):
        del timeout
        body = json.loads(req.data.decode("utf-8"))
        requests.append(body)
        if body.get("stream") is True:
            return _FakeStreamingHTTPResponse(
                [
                    'data: {"model":"command-demo","choices":[{"delta":{},"finish_reason":"stop"}]}\n',
                    "data: [DONE]\n",
                ]
            )
        return _FakeHTTPResponse(
            {
                "model": "command-demo",
                "choices": [{"message": {"content": "non-stream recovered"}, "finish_reason": "stop"}],
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = OpenAICompatibleModelClient(
        api_base_url="https://api.cohere.ai/compatibility/v1",
        api_key="secret",
        provider_name="openai-compatible",
    )

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(Message(role="user", content="hello"),),
            ),
            stream=True,
        )
    ]

    assert [request.get("stream") for request in requests] == [True, None]
    assert [event.type for event in events] == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[0].text_delta is not None
    assert events[0].text_delta.content == "non-stream recovered"
    assert events[1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_openai_compatible_stream_falls_back_from_http_500(monkeypatch):
    requests: list[dict[str, object]] = []

    def _fake_urlopen(req, timeout):
        del timeout
        body = json.loads(req.data.decode("utf-8"))
        requests.append(body)
        if body.get("stream") is True:
            raise error.HTTPError(
                req.full_url,
                500,
                "internal error",
                {},
                BytesIO(b'{"error":"stream failed"}'),
            )
        return _FakeHTTPResponse(
            {
                "model": "command-demo",
                "choices": [{"message": {"content": "non-stream recovered"}, "finish_reason": "stop"}],
            }
        )

    async def _no_sleep(delay):
        return None

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    client = OpenAICompatibleModelClient(
        api_base_url="https://api.cohere.ai/compatibility/v1",
        api_key="secret",
        provider_name="openai-compatible",
        retries=2,
        base_delay=0.0,
        jitter=0.0,
    )

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(Message(role="user", content="hello"),),
            ),
            stream=True,
        )
    ]

    assert [request.get("stream") for request in requests] == [True, True, None]
    assert events[0].text_delta is not None
    assert events[0].text_delta.content == "non-stream recovered"
    assert events[1].finish_reason == "stop"


def test_openai_compatible_cohere_base_normalizes_tools_for_strict_compatibility():
    adapter = OpenAICompatibleAdapter(
        provider_name="openai-compatible",
        cohere_compatibility=True,
    )
    payload = adapter.to_wire_request(
        RuntimeRequest(
            model_name="command-demo",
            system_prompt="system",
            messages=(
                Message(role="user", content="list files"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall("call-1", "list_dir", {"path": "."}),),
                ),
            ),
            tool_schemas=(
                {
                    "type": "function",
                    "function": {
                        "name": "list_dir",
                        "description": "List files",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "minLength": 1,
                                }
                            },
                        },
                    },
                },
            ),
        )
    )

    fn = payload["tools"][0]["function"]
    parameters = fn["parameters"]
    assert fn["strict"] is True
    assert "parallel_tool_calls" not in payload
    assert "_nexus_tool_call_reason" in parameters["required"]
    assert "minLength" not in parameters["properties"]["path"]
    assistant_args = json.loads(payload["messages"][-1]["tool_calls"][0]["function"]["arguments"])
    assert "_nexus_tool_call_reason" in assistant_args


@pytest.mark.asyncio
async def test_openai_compatible_stream_retries_socket_timeouts(monkeypatch):
    attempts = {"count": 0}
    lines = [
        'data: {"model":"demo-model","choices":[{"delta":{"content":"recovered"}}]}\n',
        "data: [DONE]\n",
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TimeoutError("The read operation timed out")
        return _FakeStreamingHTTPResponse(lines)

    async def _no_sleep(delay):
        return None

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    client = OpenAICompatibleModelClient(
        api_base_url="https://example.test/v1",
        api_key="secret",
        provider_name="openai-compatible",
        retries=2,
        base_delay=0.0,
        jitter=0.0,
    )

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="demo-model",
                system_prompt="system",
                messages=(Message(role="user", content="hello"),),
            ),
            stream=True,
        )
    ]

    assert attempts["count"] == 2
    assert events[0].type == StreamEventType.TEXT_DELTA
    assert events[0].text_delta is not None
    assert events[0].text_delta.content == "recovered"


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

    anthropic_config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={"provider": "anthropic", "api_base_url": "", "api_key": "secret"},
    )
    gemini_config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={"provider": "gemini", "api_base_url": "", "api_key": "secret"},
    )

    assert isinstance(_build_model_client(anthropic_config), AnthropicModelClient)
    assert isinstance(_build_model_client(gemini_config), GeminiModelClient)

    cohere_config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={"provider": "cohere", "api_base_url": "", "api_key": "secret"},
    )

    cohere_client = _build_model_client(cohere_config)

    assert isinstance(cohere_client, CohereModelClient)
    assert cohere_client.api_base_url == "https://api.cohere.com"


def test_openai_adapter_skips_invalid_legacy_assistant_and_tool_messages():
    adapter = OpenAICompatibleAdapter(provider_name="openai-compatible")

    payload = adapter.to_wire_request(
        RuntimeRequest(
            model_name="demo-model",
            system_prompt="system",
            messages=(
                Message(role="user", content="hello"),
                Message(role="assistant", content=""),
                Message(role="tool", content="tool output", name="write_file"),
            ),
        )
    )

    assert payload["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]


def test_openai_adapter_round_trips_reasoning_content_for_thinking_tool_calls():
    adapter = OpenAICompatibleAdapter(
        provider_name="openai-compatible",
        thinking_mode="enabled",
        reasoning_effort="max",
    )

    payload = adapter.to_wire_request(
        RuntimeRequest(
            model_name="deepseek-v4-pro",
            system_prompt="system",
            messages=(
                Message(role="user", content="read README"),
                Message(
                    role="assistant",
                    content="I need to inspect the file.",
                    reasoning_content="Need the README before answering.",
                    tool_calls=(ToolCall("call-1", "read_file", {"path": "README.md"}),),
                ),
                Message(role="tool", content="README contents", name="read_file", tool_call_id="call-1"),
            ),
        )
    )

    assistant = payload["messages"][2]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "max"
    assert assistant["reasoning_content"] == "Need the README before answering."

    response = adapter.from_wire_response(
        {
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "checking",
                        "reasoning_content": "Need a tool.",
                        "tool_calls": [
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "list_dir", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )

    assert response.message.reasoning_content == "Need a tool."
    assert response.message.tool_calls == (ToolCall("call-2", "list_dir", {}),)


def test_openai_adapter_replays_reasoning_content_key_for_auto_thinking_tool_calls():
    adapter = OpenAICompatibleAdapter(
        provider_name="openai-compatible",
        thinking_mode="auto",
    )

    payload = adapter.to_wire_request(
        RuntimeRequest(
            model_name="deepseek-v4-pro",
            system_prompt="system",
            messages=(
                Message(role="user", content="read README"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall("call-1", "read_file", {"path": "README.md"}),),
                ),
                Message(role="tool", content="README contents", name="read_file", tool_call_id="call-1"),
            ),
        )
    )

    assistant = payload["messages"][2]
    assert "thinking" not in payload
    assert assistant["reasoning_content"] == ""


def test_openai_adapter_accepts_reasoning_alias_from_wire_response():
    adapter = OpenAICompatibleAdapter(provider_name="openai-compatible")

    response = adapter.from_wire_response(
        {
            "model": "deepseek/deepseek-v4-pro",
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning": "Need a tool.",
                        "tool_calls": [
                            {
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "list_dir", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )

    assert response.message.reasoning_content == "Need a tool."
    assert response.message.tool_calls == (ToolCall("call-2", "list_dir", {}),)


def test_openai_adapter_can_disable_thinking_mode():
    adapter = OpenAICompatibleAdapter(
        provider_name="openai-compatible",
        thinking_mode="disabled",
    )

    payload = adapter.to_wire_request(
        RuntimeRequest(
            model_name="deepseek-v4-pro",
            system_prompt="system",
            messages=(
                Message(
                    role="assistant",
                    content="stored",
                    reasoning_content="provider reasoning",
                ),
            ),
        )
    )

    assert payload["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in payload
    assert "reasoning_content" not in payload["messages"][1]


def test_anthropic_adapter_converts_tools_and_messages():
    tool_schema = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }

    tools = AnthropicAdapter.tools((tool_schema,))
    messages = AnthropicAdapter.messages(
        (
            Message(role="user", content="read README"),
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall("call-1", "read_file", {"path": "README.md"}),),
            ),
            Message(role="tool", content="done", name="read_file", tool_call_id="call-1"),
        )
    )

    assert tools == [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    assert messages[1]["content"][0]["type"] == "tool_use"
    assert messages[2]["content"][0]["type"] == "tool_result"


def test_cohere_adapter_converts_tools_and_messages():
    tool_schema = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }
    adapter = CohereAdapter()

    payload = adapter.to_wire_request(
        RuntimeRequest(
            model_name="command-demo",
            system_prompt="system",
            messages=(
                Message(role="user", content="read README"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall("call-1", "read_file", {"path": "README.md"}),),
                ),
                Message(role="tool", content="plain tool output", name="read_file", tool_call_id="call-1"),
            ),
            tool_schemas=(tool_schema,),
        )
    )

    assert payload["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "read README"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"result": "plain tool output"}'},
    ]
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]
    assert payload["strict_tools"] is True


def test_cohere_adapter_strict_tools_normalizes_optional_tool_schemas_and_strips_reason_args():
    tool_schema = {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Directory path.",
                    }
                },
                "additionalProperties": False,
            },
        },
    }
    adapter = CohereAdapter()

    payload = adapter.to_wire_request(
        RuntimeRequest(
            model_name="command-demo",
            system_prompt="system",
            messages=(
                Message(role="user", content="list files"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall("call-1", "list_dir", {"path": "."}),),
                ),
            ),
            tool_schemas=(tool_schema,),
        )
    )

    parameters = payload["tools"][0]["function"]["parameters"]
    assert payload["strict_tools"] is True
    assert "_nexus_tool_call_reason" in parameters["required"]
    assert "minLength" not in parameters["properties"]["path"]
    assistant_args = json.loads(payload["messages"][-1]["tool_calls"][0]["function"]["arguments"])
    assert assistant_args["_nexus_tool_call_reason"] == "Cohere strict tool schema compatibility."

    response = adapter.from_wire_response(
        {
            "finish_reason": "TOOL_CALL",
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "list_dir",
                            "arguments": json.dumps(
                                {
                                    "path": ".",
                                    "_nexus_tool_call_reason": "Need workspace files.",
                                }
                            ),
                        },
                    }
                ],
            },
        },
        "command-demo",
    )

    assert response.tool_calls == (ToolCall("call-2", "list_dir", {"path": "."}),)


def test_cohere_adapter_bounds_large_tool_results():
    adapter = CohereAdapter()
    large_output = "x" * (_MAX_COHERE_TOOL_RESULT_CHARS + 100)

    payload = adapter.to_wire_request(
        RuntimeRequest(
            model_name="command-demo",
            system_prompt="system",
            messages=(
                Message(role="user", content="list files"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall("call-1", "list_dir", {"path": "."}),),
                ),
                Message(role="tool", content=large_output, name="list_dir", tool_call_id="call-1"),
            ),
        )
    )

    content = json.loads(payload["messages"][-1]["content"])
    assert content["truncated"] is True
    assert content["original_chars"] == len(large_output)
    assert _TOOL_RESULT_TRUNCATION_MARKER in content["result"]
    assert len(content["result"]) == _MAX_COHERE_TOOL_RESULT_CHARS


@pytest.mark.asyncio
async def test_cohere_client_posts_v2_chat_non_stream(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["authorization"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeHTTPResponse(
            {
                "id": "chat-1",
                "finish_reason": "COMPLETE",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
                "usage": {"tokens": {"input_tokens": 4, "output_tokens": 2}},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = CohereModelClient(api_key="secret")

    response = await client.complete(
        RuntimeRequest(
            model_name="command-demo",
            system_prompt="system",
            messages=(Message(role="user", content="hello"),),
        )
    )

    assert captured["url"] == "https://api.cohere.com/v2/chat"
    assert captured["authorization"] == "Bearer secret"
    assert captured["body"] == {
        "model": "command-demo",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ],
        "temperature": 0.0,
        "stream": False,
    }
    assert response.message.content == "done"
    assert response.usage is not None
    assert response.usage.provider == "cohere"
    assert response.usage.total_tokens == 6


@pytest.mark.asyncio
async def test_cohere_client_uses_longer_rate_limit_cooldown(monkeypatch):
    attempts = {"count": 0}
    delays: list[float] = []

    def _fake_urlopen(req, timeout):
        del req, timeout
        attempts["count"] += 1
        if attempts["count"] < 4:
            raise error.HTTPError(
                "https://api.cohere.com/v2/chat",
                429,
                "too many requests",
                {},
                BytesIO(b'{"message":"too many requests"}'),
            )
        return _FakeHTTPResponse(
            {
                "id": "chat-1",
                "finish_reason": "COMPLETE",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "recovered"}]},
            }
        )

    async def _record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("asyncio.sleep", _record_sleep)

    client = CohereModelClient(api_key="secret", jitter=0.0)

    response = await client.complete(
        RuntimeRequest(
            model_name="command-demo",
            system_prompt="system",
            messages=(Message(role="user", content="hello"),),
        )
    )

    assert attempts["count"] == 4
    assert delays == [10.0, 15.0, 20.0]
    assert response.message.content == "recovered"


@pytest.mark.asyncio
async def test_cohere_client_respects_longer_retry_after(monkeypatch):
    attempts = {"count": 0}
    delays: list[float] = []

    def _fake_urlopen(req, timeout):
        del req, timeout
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise error.HTTPError(
                "https://api.cohere.com/v2/chat",
                429,
                "too many requests",
                {"Retry-After": "30"},
                BytesIO(b'{"message":"too many requests"}'),
            )
        return _FakeHTTPResponse(
            {
                "id": "chat-1",
                "finish_reason": "COMPLETE",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "recovered"}]},
            }
        )

    async def _record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("asyncio.sleep", _record_sleep)

    client = CohereModelClient(api_key="secret", jitter=0.0)

    response = await client.complete(
        RuntimeRequest(
            model_name="command-demo",
            system_prompt="system",
            messages=(Message(role="user", content="hello"),),
        )
    )

    assert delays == [30.0]
    assert response.message.content == "recovered"


def test_cohere_chat_url_normalizes_v2_base_url():
    assert cohere_chat_url("https://api.cohere.com") == "https://api.cohere.com/v2/chat"
    assert cohere_chat_url("https://api.cohere.com/v2") == "https://api.cohere.com/v2/chat"
    assert cohere_chat_url("https://api.cohere.com/v2/chat") == "https://api.cohere.com/v2/chat"


@pytest.mark.asyncio
async def test_cohere_stream_message_end_error_surfaces_as_error(monkeypatch):
    lines = [
        'data: {"type":"message-end","delta":{"finish_reason":"ERROR","error":"backend failed"}}\n',
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = CohereModelClient(api_key="secret")

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(Message(role="user", content="hello"),),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in events] == [StreamEventType.ERROR]
    assert events[0].error == "backend failed"


@pytest.mark.asyncio
async def test_cohere_stream_message_end_error_logs_diagnostics(monkeypatch, caplog):
    lines = [
        'data: {"type":"message-end","delta":{"finish_reason":"ERROR","error":"backend failed"}}\n',
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    caplog.set_level(logging.WARNING, logger="nexus.integrations.cohere")

    client = CohereModelClient(api_key="secret")

    _ = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(Message(role="user", content="hello"),),
            ),
            stream=True,
        )
    ]

    assert "cohere.sse.message_end_error" in caplog.text
    assert "backend failed" in caplog.text


@pytest.mark.asyncio
async def test_cohere_stream_reads_text_from_content_start(monkeypatch):
    lines = [
        'data: {"type":"content-start","delta":{"message":{"content":{"type":"text","text":"Done."}}}}\n',
        'data: {"type":"message-end","delta":{"finish_reason":"COMPLETE"}}\n',
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = CohereModelClient(api_key="secret")

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(
                    Message(role="user", content="list files"),
                    Message(
                        role="assistant",
                        content="",
                        tool_calls=(ToolCall("call-1", "list_dir", {"path": "."}),),
                    ),
                    Message(role="tool", content="README.md", name="list_dir", tool_call_id="call-1"),
                ),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in events] == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[0].text_delta is not None
    assert events[0].text_delta.content == "Done."


@pytest.mark.asyncio
async def test_cohere_stream_logs_tool_result_payload_shape(monkeypatch, caplog):
    lines = [
        'data: {"type":"content-start","delta":{"message":{"content":{"type":"text","text":"Done."}}}}\n',
        'data: {"type":"message-end","delta":{"finish_reason":"COMPLETE"}}\n',
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    caplog.set_level(logging.DEBUG, logger="nexus.integrations.cohere")

    client = CohereModelClient(api_key="secret")

    _ = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(
                    Message(role="user", content="delegate"),
                    Message(
                        role="assistant",
                        content="",
                        tool_calls=(ToolCall("call-1", "subagent_execution", {"title": "Do", "instructions": "Do it"}),),
                    ),
                    Message(
                        role="tool",
                        content='{"status":"completed","summary":"done"}',
                        name="subagent_execution",
                        tool_call_id="call-1",
                    ),
                ),
            ),
            stream=True,
        )
    ]

    assert "cohere.chat_completion.start" in caplog.text
    assert "role_sequence=system > user > assistant:tool_calls=1 > tool:call-1" in caplog.text
    assert "tool_results=1" in caplog.text
    assert "last_tool_content_json_object=True" in caplog.text


@pytest.mark.asyncio
async def test_cohere_stream_logs_deltas_at_info_and_keeps_debug_for_lifecycle(monkeypatch, caplog):
    lines = [
        'data: {"type":"content-delta","delta":{"message":{"content":{"text":"checking "}}}}\n',
        (
            'data: {"type":"tool-call-start","index":0,"delta":{"message":{"tool_calls":'
            '{"id":"call-1","type":"function","function":{"name":"read_file"}}}}}\n'
        ),
        (
            'data: {"type":"tool-call-delta","index":0,"delta":{"message":{"tool_calls":'
            '{"function":{"arguments":"{\\"path\\":\\"README.md\\"}"}}}}}\n'
        ),
        'data: {"type":"message-end","delta":{"finish_reason":"TOOL_CALL"}}\n',
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    caplog.set_level(logging.DEBUG, logger="nexus.integrations.cohere")

    client = CohereModelClient(api_key="secret")

    _ = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(Message(role="user", content="inspect"),),
            ),
            stream=True,
        )
    ]

    records = [record for record in caplog.records if record.name == "nexus.integrations.cohere"]

    assert any(
        record.levelno == logging.INFO and "cohere.sse.text_delta type=content-delta" in record.getMessage()
        for record in records
    )
    assert any(
        record.levelno == logging.INFO and "cohere.sse.tool_call_delta" in record.getMessage()
        for record in records
    )
    assert any(
        record.levelno == logging.DEBUG and "cohere.sse.event type=tool-call-start" in record.getMessage()
        for record in records
    )
    assert any(
        record.levelno == logging.DEBUG and "cohere.sse.event type=message-end" in record.getMessage()
        for record in records
    )
    assert not any(
        record.levelno == logging.DEBUG and "cohere.sse.event type=content-delta" in record.getMessage()
        for record in records
    )
    assert not any(
        record.levelno == logging.DEBUG and "cohere.sse.tool_call_delta" in record.getMessage()
        for record in records
    )


@pytest.mark.asyncio
async def test_cohere_stream_accumulates_tool_call_deltas(monkeypatch):
    lines = [
        'data: {"type":"content-delta","delta":{"message":{"content":{"text":"checking "}}}}\n',
        (
            'data: {"type":"tool-call-start","index":0,"delta":{"message":{"tool_calls":'
            '{"id":"call-1","type":"function","function":{"name":"read_file"}}}}}\n'
        ),
        (
            'data: {"type":"tool-call-delta","index":0,"delta":{"message":{"tool_calls":'
            '{"function":{"arguments":"{\\"path\\":"}}}}}\n'
        ),
        (
            'data: {"type":"tool-call-delta","index":0,"delta":{"message":{"tool_calls":'
            '{"function":{"arguments":"\\"README.md\\"}"}}}}}\n'
        ),
        (
            'data: {"type":"message-end","delta":{"finish_reason":"TOOL_CALL",'
            '"usage":{"tokens":{"input_tokens":5,"output_tokens":1}}}}\n'
        ),
    ]

    def _fake_urlopen(req, timeout):
        del req, timeout
        return _FakeStreamingHTTPResponse(lines)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    client = CohereModelClient(api_key="secret")

    events = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="command-demo",
                system_prompt="system",
                messages=(Message(role="user", content="read README"),),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in events] == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.TOOL_CALL_COMPLETE,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[0].text_delta is not None
    assert events[0].text_delta.content == "checking "
    assert events[1].tool_call == ToolCall("call-1", "read_file", {"path": "README.md"})
    assert events[2].finish_reason == "TOOL_CALL"
    assert events[2].usage is not None
    assert events[2].usage.provider == "cohere"


class _FakeAnthropicStreamContext:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def __aiter__(self):
        return self._iter_events()

    async def _iter_events(self):
        for event in self._events:
            yield event


class _FakeAnthropicMessages:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = events

    def stream(self, **kwargs):
        del kwargs
        return _FakeAnthropicStreamContext(self._events)


class _FakeAnthropicClient:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.messages = _FakeAnthropicMessages(events)


@pytest.mark.asyncio
async def test_anthropic_stream_handles_text_partial_tool_json_and_missing_usage(monkeypatch):
    events = [
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "checking "}},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "tool-1", "name": "read_file"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
        },
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
    ]
    client = AnthropicModelClient(api_key="secret")

    monkeypatch.setattr(client, "_client", lambda: _FakeAnthropicClient(events))

    streamed = [
        event
        async for event in client.chat_completion(
            RuntimeRequest(
                model_name="claude-demo",
                system_prompt="system",
                messages=(Message(role="user", content="read"),),
            ),
            stream=True,
        )
    ]

    assert [event.type for event in streamed] == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.TOOL_CALL_COMPLETE,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert streamed[0].text_delta is not None
    assert streamed[0].text_delta.content == "checking "
    assert streamed[1].tool_call == ToolCall("tool-1", "read_file", {"_raw": '{"path":'})
    assert streamed[2].finish_reason == "tool_use"
    assert streamed[2].usage is None


def test_gemini_adapter_converts_tools_and_messages():
    tool_schema = {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search text",
            "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
        },
    }

    tools = GeminiAdapter.tools((tool_schema,))
    contents = GeminiAdapter.contents(
        (
            Message(role="user", content="search"),
            Message(role="assistant", content="", tool_calls=(ToolCall("call-1", "grep", {"pattern": "x"}),)),
            Message(role="tool", content="match", name="grep", tool_call_id="call-1"),
        )
    )

    assert tools[0]["function_declarations"][0]["name"] == "grep"
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["function_call"]["name"] == "grep"
    assert contents[2]["parts"][0]["function_response"]["response"] == {"result": "match"}


def test_gemini_adapter_strips_additional_properties_from_tool_schemas():
    tool_schema = {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "metadata": {
                        "type": "object",
                        "properties": {"author": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additional_properties": False,
                            "properties": {"name": {"type": "string"}},
                        },
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }

    tools = GeminiAdapter.tools((tool_schema,))
    parameters = tools[0]["function_declarations"][0]["parameters"]

    assert "additionalProperties" not in json.dumps(parameters)
    assert "additional_properties" not in json.dumps(parameters)
    assert "additionalProperties" in json.dumps(tool_schema)


def test_gemini_adapter_builds_current_google_genai_types():
    from google.genai import types

    tool_schema = {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search text",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    }

    tools = GeminiAdapter.typed_tools(types, (tool_schema,))
    contents = GeminiAdapter.typed_contents(
        types,
        (
            Message(role="user", content="search"),
            Message(role="assistant", content="", tool_calls=(ToolCall("call-1", "grep", {"pattern": "x"}),)),
            Message(role="tool", content="match", name="grep", tool_call_id="call-1"),
        ),
    )

    tool_payload = tools[0].model_dump(by_alias=True, exclude_none=True)
    function_declaration = tool_payload["functionDeclarations"][0]
    assert function_declaration["name"] == "grep"
    assert "parametersJsonSchema" in function_declaration
    assert "parameters" not in function_declaration
    assert "additionalProperties" not in json.dumps(function_declaration)

    content_payloads = [
        content.model_dump(by_alias=True, exclude_none=True)
        for content in contents
    ]
    assert [content["role"] for content in content_payloads] == ["user", "model", "user"]
    assert content_payloads[1]["parts"][0]["functionCall"]["name"] == "grep"
    assert content_payloads[2]["parts"][0]["functionResponse"]["response"] == {"result": "match"}


def test_gemini_client_uses_sdk_default_api_version_and_typed_config():
    from google.genai import types

    client = GeminiModelClient(api_key="secret")
    request = RuntimeRequest(
        model_name="gemini-2.5-flash",
        system_prompt="system",
        messages=(Message(role="user", content="search"),),
        max_output_tokens=128,
        tool_schemas=(
            {
                "type": "function",
                "function": {
                    "name": "grep",
                    "description": "Search text",
                    "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                },
            },
        ),
    )

    config = client._config(types, request).model_dump(by_alias=True, exclude_none=True)

    assert client._http_options(types) is None
    assert config["systemInstruction"] == "system"
    assert config["maxOutputTokens"] == 128
    assert "functionDeclarations" in config["tools"][0]


def test_gemini_client_allows_explicit_api_version_override():
    from google.genai import types

    client = GeminiModelClient(api_key="secret", api_version="v1")

    assert client._http_options(types).model_dump(by_alias=True, exclude_none=True) == {
        "apiVersion": "v1"
    }


def test_gemini_response_events_allow_mixed_text_tool_calls_without_usage():
    client = GeminiModelClient(api_key="secret")

    events = client._events_from_response(
        {
            "candidates": [
                {
                    "finish_reason": "STOP",
                    "content": {
                        "parts": [
                            {"text": "checking "},
                            {"function_call": {"name": "grep", "args": {"pattern": "needle"}}},
                        ]
                    },
                }
            ]
        },
        "gemini-demo",
        final=True,
    )

    assert [event.type for event in events] == [
        StreamEventType.TEXT_DELTA,
        StreamEventType.TOOL_CALL_COMPLETE,
        StreamEventType.MESSAGE_COMPLETE,
    ]
    assert events[0].text_delta is not None
    assert events[0].text_delta.content == "checking "
    assert events[1].tool_call == ToolCall("gemini-0001", "grep", {"pattern": "needle"})
    assert events[2].finish_reason == "stop"
    assert events[2].usage is None


def test_ollama_parse_tool_call_preserves_malformed_arguments_as_raw():
    client = OllamaModelClient(base_url="http://localhost:11434", model_name="demo")

    tool_call = client._parse_tool_call(
        {"function": {"name": "read_file", "arguments": '{"path":'}}
    )

    assert tool_call == ToolCall("ollama-0001", "read_file", {"_raw": '{"path":'})
