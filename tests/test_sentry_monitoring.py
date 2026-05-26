from __future__ import annotations

from contextlib import nullcontext
import os
from pathlib import Path

import pytest

from nexus.config import load_config
from nexus.hooks import HookEvent, HookExecutor
from nexus.integrations.fake_model import FakeModelClient
from nexus.models import Message, RuntimeResponse, ToolCall, ToolExecutionContext
from nexus.observability.sentry import (
    SentryHookService,
    SentryMonitor,
    _SentrySDKClient,
    describe_sentry_dsn,
    sentry_settings_from_config,
)
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


class LegacySpan:
    def __init__(self, *, op=None, name=None) -> None:
        self.op = op
        self.name = name
        self.data = {}
        self.tags = {}
        self.status = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_data(self, key, value):
        self.data[key] = value

    def set_tag(self, key, value):
        self.tags[key] = value

    def set_status(self, value):
        self.status = value


class LegacySentrySDK:
    def __init__(self) -> None:
        self.current_span = None
        self.transaction_calls = []
        self.span_calls = []
        self.update_calls = []

    def start_transaction(self, *, op=None, name=None):
        self.transaction_calls.append({"op": op, "name": name})
        span = LegacySpan(op=op, name=name)
        self.current_span = span
        return span

    def start_span(self, *, op=None, name=None):
        self.span_calls.append({"op": op, "name": name})
        span = LegacySpan(op=op, name=name)
        self.current_span = span
        return span

    def update_current_span(self, op=None, name=None):
        self.update_calls.append({"op": op, "name": name})
        if self.current_span is None:
            return
        if op is not None:
            self.current_span.op = op
        if name is not None:
            self.current_span.name = name

    def get_current_span(self):
        return self.current_span


def _raise_sentry_verification_error(verification_id: str) -> None:
    raise RuntimeError(f"Intentional Sentry verification failure: {verification_id}")


def _emit_live_sentry_verification(monitor: SentryMonitor, verification_id: str) -> tuple[str | None, str | None]:
    context = {
        "session_id": "pytest-sentry-live-verify",
        "turn_id": verification_id,
        "trace_id": verification_id,
        "mode": "pytest",
        "verification_id": verification_id,
        "verification_kind": "live_sentry_test",
    }

    try:
        _raise_sentry_verification_error(verification_id)
    except RuntimeError as exc:
        exception_event_id = monitor.capture_exception(exc, context=context)
        message_event_id = monitor.capture_message(
            f"Nexus live Sentry verification error: {verification_id}",
            level="error",
            context={**context, "event_type": "verification_error_message"},
        )
        return exception_event_id, message_event_id

    return None, None


def test_sdk_client_backfills_span_updates_for_legacy_sdk():
    sdk = LegacySentrySDK()
    client = _SentrySDKClient(sdk)

    transaction = client.start_transaction(
        op="nexus.turn",
        name="nexus.turn",
        attributes={"nexus.session_id": "s1"},
    )
    span = client.start_span(
        op="gen_ai.chat",
        name="command-a",
        attributes={"gen_ai.request.model": "command-a"},
    )
    client.update_current_span(
        name="command-a-final",
        attributes={"gen_ai.usage.total_tokens": 42},
        status="completed",
    )

    assert sdk.transaction_calls == [{"op": "nexus.turn", "name": "nexus.turn"}]
    assert transaction.data["nexus.session_id"] == "s1"
    assert sdk.span_calls == [{"op": "gen_ai.chat", "name": "command-a"}]
    assert sdk.update_calls == [{"op": None, "name": "command-a-final"}]
    assert span.name == "command-a-final"
    assert span.data["gen_ai.request.model"] == "command-a"
    assert span.data["gen_ai.usage.total_tokens"] == 42
    assert span.status == "completed"


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


def test_live_sentry_verification_emits_exception_and_error_message(tmp_path):
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

    verification_id = "pytest-live-sentry-verify"
    exception_event_id, message_event_id = _emit_live_sentry_verification(monitor, verification_id)

    assert len(client.exceptions) == 1
    assert str(client.exceptions[0]) == f"Intentional Sentry verification failure: {verification_id}"
    assert client.messages == [(f"Nexus live Sentry verification error: {verification_id}", "error")]
    assert exception_event_id == "exception-id"
    assert message_event_id == "message-id"
    assert client.contexts["nexus"]["verification_id"] == verification_id
    assert client.contexts["nexus"]["event_type"] == "verification_error_message"
    assert client.tags["nexus.verification_id"] == verification_id
    assert client.tags["nexus.verification_kind"] == "live_sentry_test"
    assert client.tags["nexus.event_type"] == "verification_error_message"


def test_sentry_live_verify_with_dotenv_credentials():
    if os.getenv("NEXUS_RUN_LIVE_SENTRY_TEST") != "1":
        pytest.skip("Set NEXUS_RUN_LIVE_SENTRY_TEST=1 to send live Sentry exception and error verification events.")

    workspace_root = Path(__file__).resolve().parents[1]
    config = load_config(
        workspace_root,
        global_root=workspace_root / ".pytest-global",
        cli_overrides={"sentry_enabled": True},
    )
    settings = sentry_settings_from_config(config)

    if not settings.dsn:
        pytest.fail(
            "No live Sentry DSN was loaded. Set SENTRY_DSN or AGENT_SENTRY_DSN in the workspace .env or environment before running this verification test."
        )

    monitor = SentryMonitor(settings)
    monitor.initialize()

    assert monitor.enabled() is True

    verification_id = os.getenv("NEXUS_SENTRY_VERIFICATION_ID", "pytest-live-sentry-verify")
    try:
        exception_event_id, message_event_id = _emit_live_sentry_verification(monitor, verification_id)
        print(
            "Sentry verification sent:",
            f"target={describe_sentry_dsn(settings.dsn)}",
            f"environment={settings.environment}",
            f"verification_id={verification_id}",
            f"exception_event_id={exception_event_id}",
            f"message_event_id={message_event_id}",
        )
    finally:
        monitor.flush()


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
    assert prompt_breadcrumb["data"]["description"] == "User prompt submitted for the next Nexus turn."
    assert "prompt_preview" not in prompt_breadcrumb["data"]
    assert tool_breadcrumb["data"]["output_chars"] == len("api_key=abc123")
    assert tool_breadcrumb["data"]["description"].startswith("Tool execution completed for bash")
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
