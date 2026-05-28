from __future__ import annotations

import logging
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.config import load_config
from nexus.hooks import HookEvent, HookExecutor, setup_hooks
from nexus.integrations.fake_model import FakeModelClient
from nexus.models import AgentEvent, AgentEventType, Message, RuntimeResponse, ToolCall, ToolExecutionContext, ToolResult, UsageSnapshot
from nexus.observability.langfuse import LangfuseHookService, LangfuseMonitor, langfuse_settings_from_config
from nexus.runtime.agent import Agent
from nexus.runtime.turn_runner import _turn_lifecycle_payload
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
async def test_langfuse_hook_service_records_turn_level_transcript_only(tmp_path):
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
                "turn_steps": [
                    {
                        "kind": "model_response",
                        "content": "Calling echo tool",
                        "finish_reason": "tool_calls",
                        "tool_calls": [
                            {
                                "call_id": "call-1",
                                "tool_name": "echo_tool",
                                "arguments": {"value": "42"},
                            }
                        ],
                        "usage": None,
                    },
                    {
                        "kind": "tool_execution",
                        "call_id": "call-1",
                        "tool_name": "echo_tool",
                        "is_error": False,
                        "input": {
                            "call_id": "call-1",
                            "tool_name": "echo_tool",
                            "arguments": {"value": "42"},
                        },
                        "output": {"content": "42", "metadata": {}},
                        "is_subagent": False,
                    },
                    {
                        "kind": "model_response",
                        "content": "42",
                        "finish_reason": "stop",
                        "tool_calls": [],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 3,
                            "total_tokens": 8,
                            "estimated_cost_usd": 0.000008,
                            "provider": "fake",
                            "model": "fake-model",
                        },
                    },
                ],
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
    assert root.kwargs["session_id"] == "session-1"
    assert root.kwargs["trace_context"]["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert root.kwargs["metadata"]["description"].startswith("Nexus turn trace for session session-1")
    assert root.ended
    assert root.ended[0]["output"]["response"] == "42"
    assert root.ended[0]["output"]["turn_steps"][0]["tool_calls"][0]["tool_name"] == "echo_tool"
    assert root.ended[0]["output"]["turn_steps"][1]["input"]["arguments"]["value"] == "42"
    assert root.ended[0]["output"]["turn_steps"][1]["output"]["content"] == "42"
    assert root.ended[0]["metadata"]["description"].startswith("Nexus turn trace for session session-1")
    assert root.children == []
    assert client.flushed is True


def test_turn_lifecycle_payload_captures_ordered_model_and_tool_steps():
    state = SimpleNamespace(
        session=SimpleNamespace(session_id="session-1"),
        mode=SimpleNamespace(value="default"),
        config=SimpleNamespace(agent_mode="basic", provider="fake", model_name="fake-model"),
    )
    events = [
        AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(
                message=Message(role="assistant", content="Calling echo tool"),
                tool_calls=(ToolCall(call_id="call-1", tool_name="echo_tool", arguments={"value": "42"}),),
                finish_reason="tool_calls",
            ),
        ),
        AgentEvent(
            kind=AgentEventType.TOOL_RESULT,
            payload=ToolResult(
                call_id="call-1",
                tool_name="echo_tool",
                output="42",
                metadata={"actor": "supervisor"},
            ),
        ),
        AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(
                message=Message(role="assistant", content="42"),
                usage=UsageSnapshot(
                    prompt_tokens=5,
                    completion_tokens=3,
                    total_tokens=8,
                    estimated_cost_usd=0.000008,
                    provider="fake",
                    model="fake-model",
                ),
                finish_reason="stop",
            ),
        ),
    ]

    payload = _turn_lifecycle_payload(
        state,
        turn_id="turn-1",
        trace_id="0123456789abcdef0123456789abcdef",
        status="completed",
        started_at=time.perf_counter(),
        events=events,
    )

    assert payload["turn_steps"][0]["kind"] == "model_response"
    assert payload["turn_steps"][0]["tool_calls"][0]["tool_name"] == "echo_tool"
    assert payload["turn_steps"][1]["kind"] == "tool_execution"
    assert payload["turn_steps"][1]["input"]["tool_name"] == "echo_tool"
    assert payload["turn_steps"][1]["output"]["content"] == "42"
    assert payload["turn_steps"][2]["usage"]["total_tokens"] == 8


def test_turn_lifecycle_payload_preserves_subagent_tool_input_and_output_context():
    state = SimpleNamespace(
        session=SimpleNamespace(session_id="session-1"),
        mode=SimpleNamespace(value="default"),
        config=SimpleNamespace(agent_mode="advanced", provider="fake", model_name="fake-model"),
    )
    subagent_output = '{"status":"completed","summary":"done","context":{"allowed_tools":["read_file"]}}'
    events = [
        AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(
                message=Message(role="assistant", content="Delegating execution"),
                tool_calls=(
                    ToolCall(
                        call_id="sub-call-1",
                        tool_name="subagent_execution",
                        arguments={"title": "Implement fix", "instructions": "Edit the file and run tests."},
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ),
        AgentEvent(
            kind=AgentEventType.TOOL_RESULT,
            payload=ToolResult(
                call_id="sub-call-1",
                tool_name="subagent_execution",
                output=subagent_output,
                metadata={
                    "task_id": "task-1",
                    "status": "completed",
                    "context_snapshot": {"allowed_tools": ["read_file"], "tool_call_count": 3},
                },
            ),
        ),
    ]

    payload = _turn_lifecycle_payload(
        state,
        turn_id="turn-1",
        trace_id="0123456789abcdef0123456789abcdef",
        status="completed",
        started_at=time.perf_counter(),
        events=events,
    )

    execution = payload["turn_steps"][1]
    assert execution["kind"] == "tool_execution"
    assert execution["is_subagent"] is True
    assert execution["input"]["tool_name"] == "subagent_execution"
    assert execution["input"]["arguments"]["title"] == "Implement fix"
    assert execution["output"]["content"] == subagent_output
    assert execution["output"]["metadata"]["context_snapshot"]["allowed_tools"] == ["read_file"]


def test_setup_hooks_attaches_langfuse_monitor_when_configured(tmp_path, monkeypatch):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={
            "langfuse_enabled": True,
            "langfuse_public_key": "pk-lf-test",
            "langfuse_secret_key": "sk-lf-test",
        },
    )

    class StubMonitor:
        def __init__(self) -> None:
            self.settings = object()

        def enabled(self) -> bool:
            return True

    class StubService:
        registered = False

        def __init__(self, monitor, settings) -> None:
            self.monitor = monitor
            self.settings = settings

        def register(self, hooks) -> None:
            StubService.registered = True

    monitor = StubMonitor()
    monkeypatch.setattr("nexus.observability.setup_langfuse_monitor", lambda cfg: monitor)
    monkeypatch.setattr("nexus.observability.LangfuseHookService", StubService)

    hooks = setup_hooks(config)

    assert hooks.langfuse_monitor is monitor
    assert StubService.registered is True


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


def test_observability_extra_includes_langfuse_dependency():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    observability = data["project"]["optional-dependencies"]["observability"]

    assert any(str(entry).startswith("langfuse") for entry in observability)