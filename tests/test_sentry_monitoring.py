from __future__ import annotations

from contextlib import nullcontext

import pytest

from nexus.config import load_config
from nexus.hooks import HookEvent, HookExecutor
from nexus.integrations.fake_model import FakeModelClient
from nexus.models import Message, RuntimeResponse, ToolCall, ToolExecutionContext
from nexus.observability.sentry import SentryHookService, SentryMonitor, sentry_settings_from_config
from nexus.runtime.agent import Agent
from nexus.tools.base import ToolKind, ToolRegistry


class FakeSentryClient:
    def __init__(self) -> None:
        self.init_kwargs = {}
        self.breadcrumbs = []
        self.messages = []
        self.exceptions = []
        self.tags = {}
        self.contexts = {}
        self.flushed = False

    def init(self, **kwargs):
        self.init_kwargs = kwargs

    def capture_exception(self, error):
        self.exceptions.append(error)
        return "exception-id"

    def capture_message(self, message, level="info"):
        self.messages.append((message, level))
        return "message-id"

    def add_breadcrumb(self, **kwargs):
        self.breadcrumbs.append(kwargs)

    def set_tag(self, key, value):
        self.tags[key] = value

    def set_context(self, key, value):
        self.contexts[key] = value

    def start_transaction(self, **kwargs):
        return nullcontext()

    def start_span(self, **kwargs):
        return nullcontext()

    def update_current_span(self, **kwargs):
        self.contexts["span"] = kwargs

    def flush(self, timeout=None):
        self.flushed = True
        return True


def test_sentry_settings_read_config_and_env_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SENTRY_ENABLED", "true")
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/123")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    monkeypatch.setenv("SENTRY_RELEASE", "nexus@abc123")

    config = load_config(tmp_path, global_root=tmp_path / "global")
    settings = sentry_settings_from_config(config)

    assert settings.active is True
    assert settings.environment == "staging"
    assert settings.release == "nexus@abc123"


def test_sentry_config_rejects_invalid_sample_rate(tmp_path):
    (tmp_path / ".nexus").mkdir()
    (tmp_path / ".nexus" / "config.toml").write_text(
        'sentry_traces_sample_rate = 1.5\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sentry_traces_sample_rate"):
        load_config(tmp_path, global_root=tmp_path / "global")


@pytest.mark.asyncio
async def test_sentry_hook_service_records_redacted_prompt_and_tool_breadcrumbs(tmp_path):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={
            "sentry_enabled": True,
            "sentry_dsn": "https://public@example.ingest.sentry.io/123",
        },
    )
    client = FakeSentryClient()
    monitor = SentryMonitor(sentry_settings_from_config(config), client=client)
    monitor.initialize()
    hooks = HookExecutor()
    SentryHookService(monitor, monitor.settings).register(hooks)

    await hooks.emit(
        HookEvent.USER_PROMPT_SUBMIT,
        {
            "prompt": "secret prompt text",
            "session_id": "s1",
            "turn_id": "t1",
            "trace_id": "tr1",
            "mode": "default",
        },
    )
    await hooks.emit(
        HookEvent.POST_TOOL_USE,
        {
            "tool_name": "bash",
            "tool_source": "builtin",
            "output": "api_key=abc123",
            "is_error": True,
            "duration_ms": 12.3,
            "session_id": "s1",
            "turn_id": "t1",
            "trace_id": "tr1",
            "call_id": "call-1",
        },
    )

    prompt_breadcrumb = client.breadcrumbs[0]
    tool_breadcrumb = client.breadcrumbs[1]
    assert prompt_breadcrumb["data"]["prompt_chars"] == len("secret prompt text")
    assert "prompt_preview" not in prompt_breadcrumb["data"]
    assert tool_breadcrumb["data"]["output_chars"] == len("api_key=abc123")
    assert "output_preview" not in tool_breadcrumb["data"]
    assert client.tags["nexus.session_id"] == "s1"


@pytest.mark.asyncio
async def test_sentry_hook_service_captures_provider_error_notification(tmp_path):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={
            "sentry_enabled": True,
            "sentry_dsn": "https://public@example.ingest.sentry.io/123",
        },
    )
    client = FakeSentryClient()
    monitor = SentryMonitor(sentry_settings_from_config(config), client=client)
    monitor.initialize()
    hooks = HookExecutor()
    SentryHookService(monitor, monitor.settings).register(hooks)

    await hooks.emit(
        HookEvent.NOTIFICATION,
        {
            "event": "model_error",
            "session_id": "s1",
            "turn_id": "t1",
            "trace_id": "tr1",
            "provider": "fake",
            "model": "fake-model",
            "error": "stream failed",
        },
    )

    assert client.messages == [("Nexus provider stream error", "error")]
    assert client.contexts["nexus"]["error"] == "stream failed"


@pytest.mark.asyncio
async def test_tool_exception_emits_failed_post_tool_and_sentry_exception(tmp_path):
    class ExplodingTool:
        name = "explode"
        description = "Raise an exception."
        kind = ToolKind.READ
        input_schema = {"type": "object", "properties": {}}
        is_mutating = False

        async def execute(self, call_id, arguments, context):
            raise RuntimeError("boom")

    client = FakeSentryClient()
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={
            "sentry_enabled": True,
            "sentry_dsn": "https://public@example.ingest.sentry.io/123",
        },
    )
    monitor = SentryMonitor(sentry_settings_from_config(config), client=client)
    monitor.initialize()
    hooks = HookExecutor()
    hooks.sentry_monitor = monitor
    payloads = []

    async def record_post_tool(payload):
        payloads.append(payload)

    hooks.register(HookEvent.POST_TOOL_USE, record_post_tool)
    registry = ToolRegistry()
    registry.register(ExplodingTool())
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Calling."),
                    tool_calls=(ToolCall(call_id="call-1", tool_name="explode", arguments={}),),
                    finish_reason="tool_calls",
                )
            ]
        ),
        tool_registry=registry,
        hooks=hooks,
    )

    context = ToolExecutionContext(
        session_id="s1",
        working_directory=tmp_path,
        metadata={"turn_id": "t1", "trace_id": "tr1", "config": config},
    )
    with pytest.raises(RuntimeError, match="boom"):
        _ = [event async for event in agent.run([Message(role="user", content="go")], context)]

    assert payloads[0]["is_error"] is True
    assert payloads[0]["exception_type"] == "RuntimeError"
    assert isinstance(client.exceptions[0], RuntimeError)
