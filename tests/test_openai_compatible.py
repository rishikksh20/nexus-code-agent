from __future__ import annotations

import json
from urllib import error

import pytest

from nexus.app import _build_model_client
from nexus.config import load_config
from nexus.integrations.anthropic import AnthropicAdapter, AnthropicModelClient
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
