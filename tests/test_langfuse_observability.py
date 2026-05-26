from __future__ import annotations

import logging

import pytest

from nexus.config import load_config
from nexus.hooks import HookEvent, HookExecutor
from nexus.integrations.fake_model import FakeModelClient
from nexus.models import Message, ToolExecutionContext
from nexus.observability.langfuse import LangfuseHookService, LangfuseMonitor, langfuse_settings_from_config
from nexus.runtime.agent import Agent
from nexus.tools.base import ToolRegistry


class FakeLangfuseObservation:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.children = []
        self.updated = []
        self.ended = []
        self.trace_id = kwargs.get("trace_context", {}).get("trace_id")
        self.id = kwargs.get("name", "observation")

    def update(self, **kwargs):
        self.updated.append(kwargs)

    def end(self, **kwargs):
        self.ended.append(kwargs)

    def start_observation(self, **kwargs):
        child = FakeLangfuseObservation(kwargs)
        self.children.append(child)
        return child


class FakeLangfuseClient:
    def __init__(self) -> None:
        self.roots = []
        self.flushed = False

    def start_observation(self, **kwargs):
        root = FakeLangfuseObservation(kwargs)
        self.roots.append(root)
        return root

    def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_agent_emits_model_start_and_end_notifications(tmp_path):
    hooks = HookExecutor()
    notifications = []

    async def record_notification(payload):
        notifications.append(payload)

    hooks.register(HookEvent.NOTIFICATION, record_notification)
    agent = Agent(model_client=FakeModelClient(), tool_registry=ToolRegistry(), hooks=hooks)
    config = load_config(tmp_path, global_root=tmp_path / "global", strict=False)
    context = ToolExecutionContext(
        session_id="session-1",
        working_directory=tmp_path,
        metadata={
            "turn_id": "turn-1",
            "trace_id": "0123456789abcdef0123456789abcdef",
            "config": config,
        },
    )

    _ = [event async for event in agent.run([Message(role="user", content="hello")], context)]

    event_names = [payload["event"] for payload in notifications]
    assert "model_start" in event_names
    assert "model_end" in event_names
    model_start = next(payload for payload in notifications if payload["event"] == "model_start")
    model_end = next(payload for payload in notifications if payload["event"] == "model_end")
    assert model_start["system_prompt"]
    assert model_end["output"] == "Echo: hello"


@pytest.mark.asyncio
async def test_langfuse_hook_service_records_turn_model_tool_and_warning_events(tmp_path):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={
            "langfuse_enabled": True,
            "langfuse_public_key": "pk-lf-test",
            "langfuse_secret_key": "sk-lf-test",
        },
    )
    client = FakeLangfuseClient()
    monitor = LangfuseMonitor(langfuse_settings_from_config(config), client=client)
    monitor.initialize()
    hooks = HookExecutor()
    hooks.langfuse_monitor = monitor
    LangfuseHookService(monitor, monitor.settings).register(hooks)

    try:
        await hooks.emit(
            HookEvent.USER_PROMPT_SUBMIT,
            {
                "prompt": "sum numbers",
                "effective_prompt": "sum numbers",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "mode": "default",
            },
        )
        await hooks.emit(
            HookEvent.TURN_START,
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "provider": "fake",
                "model": "fake-model",
                "mode": "default",
                "agent_mode": "basic",
                "status": "started",
            },
        )
        await hooks.emit(
            HookEvent.NOTIFICATION,
            {
                "event": "model_start",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "model_call_id": "model-1",
                "provider": "fake",
                "model": "fake-model",
                "system_prompt": "You are Nexus",
                "messages": [{"role": "user", "content": "sum numbers"}],
                "message_count": 1,
                "tool_schema_count": 1,
                "turn_index": 1,
                "actor": "supervisor",
                "prompt_name": "nexus-system-prompt",
                "prompt_version": "",
                "system_prompt_hash": "abc123",
                "system_prompt_chars": 13,
            },
        )
        await hooks.emit(
            HookEvent.PRE_TOOL_USE,
            {
                "tool_name": "echo_tool",
                "tool_source": "builtin",
                "tool_origin": "default",
                "arguments": {"value": "42"},
                "call_id": "call-1",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "is_mutating": False,
            },
        )
        await hooks.emit(
            HookEvent.POST_TOOL_USE,
            {
                "tool_name": "echo_tool",
                "tool_source": "builtin",
                "tool_origin": "default",
                "arguments": {"value": "42"},
                "call_id": "call-1",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "is_mutating": False,
                "is_error": False,
                "duration_ms": 4.2,
                "output": "42",
            },
        )
        logging.getLogger("nexus.runtime.agent").warning("warning inside turn")
        await hooks.emit(
            HookEvent.NOTIFICATION,
            {
                "event": "model_end",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "model_call_id": "model-1",
                "provider": "fake",
                "model": "fake-model",
                "finish_reason": "stop",
                "tool_call_count": 1,
                "output": "42",
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                    "total_tokens": 8,
                    "estimated_cost_usd": 0.000008,
                },
                "status": "completed",
            },
        )
        await hooks.emit(
            HookEvent.CONTEXT_COMPACTION,
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "messages_before_prune": 5,
                "messages_before_compaction": 5,
                "messages_after": 4,
                "pruned_tool_results": 1,
                "compacted": False,
                "carry_over_entries": 0,
            },
        )
        await hooks.emit(
            HookEvent.TURN_END,
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "provider": "fake",
                "model": "fake-model",
                "mode": "default",
                "agent_mode": "basic",
                "status": "completed",
                "duration_ms": 12.3,
                "tool_calls": 1,
                "response": "42",
            },
        )
        await hooks.emit(
            HookEvent.STOP,
            {
                "session_id": "session-1",
                "turn_id": "turn-1",
                "trace_id": "0123456789abcdef0123456789abcdef",
                "headless": True,
            },
        )
    finally:
        monitor.close()

    assert client.roots
    root = client.roots[0]
    assert root.kwargs["name"] == "nexus.turn"
    assert root.kwargs["trace_context"]["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert root.ended
    assert root.ended[0]["output"]["response"] == "42"
    child_names = [child.kwargs["name"] for child in root.children]
    assert "nexus.model" in child_names
    assert "tool.echo_tool" in child_names
    assert "nexus.log" in child_names
    assert "context.compaction" in child_names
    assert client.flushed is True


def test_langfuse_settings_read_config_and_env_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-env")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com")
    monkeypatch.setenv("LANGFUSE_ENVIRONMENT", "staging")
    monkeypatch.setenv("LANGFUSE_RELEASE", "nexus@test")

    config = load_config(tmp_path, global_root=tmp_path / "global")
    settings = langfuse_settings_from_config(config)

    assert settings.active is True
    assert settings.base_url == "https://us.cloud.langfuse.com"
    assert settings.environment == "staging"
    assert settings.release == "nexus@test"