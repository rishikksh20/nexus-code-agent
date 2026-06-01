from __future__ import annotations

import json
import logging

import pytest
from rich.console import Console

from nexus.cli.headless import run_headless
from nexus.config import load_config
from nexus.integrations.fake_model import FakeModelClient
from nexus.models import ConfirmationKind, Message, RuntimeResponse, ToolCall, UsageSnapshot
from nexus.observability import JsonlAuditTrail, JsonlRuntimeLogger, RuntimeMetricsCollector, configure_root_text_logging, redact_payload, register_audit_hooks, register_default_runtime_hooks
from nexus.runtime.agent import Agent
from nexus.runtime.execution import ExecutionMode
from nexus.hooks import HookEvent, HookExecutor
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import SessionStore, new_snapshot
from nexus.memory.store import MemoryStore
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, ReadFileTool, WriteFileTool


@pytest.mark.asyncio
async def test_agent_emits_tool_hooks(tool_context):
    payloads: dict[str, list[dict]] = {"pre": [], "post": [], "notify": []}
    hooks = HookExecutor()

    async def _record_pre(payload):
        payloads["pre"].append(payload)

    async def _record_post(payload):
        payloads["post"].append(payload)

    async def _record_notify(payload):
        payloads["notify"].append(payload)

    hooks.register(HookEvent.PRE_TOOL_USE, _record_pre)
    hooks.register(HookEvent.POST_TOOL_USE, _record_post)
    hooks.register(HookEvent.NOTIFICATION, _record_notify)

    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Checking time."),
                tool_calls=(ToolCall(call_id="call-1", tool_name="get_time", arguments={}),),
                usage=UsageSnapshot(
                    prompt_tokens=10,
                    completion_tokens=4,
                    total_tokens=14,
                    estimated_cost_usd=0.0014,
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(
                message=Message(role="assistant", content="Done."),
                usage=UsageSnapshot(
                    prompt_tokens=6,
                    completion_tokens=2,
                    total_tokens=8,
                    estimated_cost_usd=0.0008,
                ),
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    agent = Agent(model_client=model, tool_registry=registry, hooks=hooks)

    events = [
        event
        async for event in agent.run([Message(role="user", content="what time is it")], tool_context)
    ]

    assert any(event.kind == "tool_result" for event in events)
    assert payloads["pre"][0]["tool_name"] == "get_time"
    assert payloads["post"][0]["tool_name"] == "get_time"
    assert payloads["pre"][0]["tool_call_id"] == "call-1"
    assert payloads["post"][0]["duration_ms"] >= 0
    assert any(payload["event"] == "model_usage" for payload in payloads["notify"])


@pytest.mark.asyncio
async def test_headless_logging_records_prompt_tool_and_stop(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global", cli_overrides={"log_format": "json"})
    hooks = HookExecutor()
    log_path = config.log_dir / "runtime.jsonl"
    register_default_runtime_hooks(hooks, JsonlRuntimeLogger(log_path))

    registry = ToolRegistry()
    registry.register(GetTimeTool())
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Checking time."),
                    tool_calls=(ToolCall(call_id="call-1", tool_name="get_time", arguments={}),),
                    usage=UsageSnapshot(
                        prompt_tokens=12,
                        completion_tokens=5,
                        total_tokens=17,
                        estimated_cost_usd=0.0017,
                    ),
                    finish_reason="tool_calls",
                ),
                RuntimeResponse(
                    message=Message(role="assistant", content="Done."),
                    usage=UsageSnapshot(
                        prompt_tokens=8,
                        completion_tokens=2,
                        total_tokens=10,
                        estimated_cost_usd=0.001,
                    ),
                ),
            ]
        ),
        tool_registry=registry,
        hooks=hooks,
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("hooked"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True),
        hooks=hooks,
    )

    result = await run_headless(
        state,
        agent,
        "what time is it",
        auto_confirm=False,
        output_path=None,
        output_format="text",
        quiet=True,
    )

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    event_names = [record["event"] for record in records]
    notifications = [record for record in records if record["event"] == "notification"]

    assert result.response == "Done."
    assert event_names[0] == "user_prompt_submit"
    assert "turn_start" in event_names
    assert "pre_tool_use" in event_names
    assert "post_tool_use" in event_names
    assert "turn_end" in event_names
    assert event_names[-1] == "stop"
    assert [record["payload"]["event"] for record in notifications] == [
        "model_start",
        "model_end",
        "model_usage",
        "model_start",
        "model_end",
        "model_usage",
    ]
    assert notifications[2]["payload"]["total_tokens"] == 17
    assert notifications[5]["payload"]["total_tokens"] == 10


@pytest.mark.asyncio
async def test_agent_emits_clarification_notification(tool_context):
    payloads: list[dict] = []
    hooks = HookExecutor()

    async def _record_notify(payload):
        payloads.append(payload)

    hooks.register(HookEvent.NOTIFICATION, _record_notify)

    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Need a path."),
                tool_calls=(ToolCall(call_id="clarify-1", tool_name="read_file", arguments={}),),
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    agent = Agent(model_client=model, tool_registry=registry, hooks=hooks)

    events = [
        event
        async for event in agent.run([Message(role="user", content="write a note")], tool_context)
    ]

    clarification = next(event for event in events if event.kind == "confirmation_requested")
    assert clarification.payload.kind is ConfirmationKind.CLARIFICATION
    assert any(payload["event"] == "clarification_requested" for payload in payloads)


@pytest.mark.asyncio
async def test_hook_executor_isolates_handler_failures():
    hooks = HookExecutor()
    payloads: list[dict] = []

    async def _broken(payload):
        raise OSError("disk full")

    async def _healthy(payload):
        payloads.append(payload)

    hooks.register(HookEvent.NOTIFICATION, _broken)
    hooks.register(HookEvent.NOTIFICATION, _healthy)

    await hooks.emit(HookEvent.NOTIFICATION, {"event": "model_usage"})

    assert payloads == [{"event": "model_usage"}]


@pytest.mark.asyncio
async def test_jsonl_logger_ignores_file_write_failures(tmp_path, monkeypatch):
    logger = JsonlRuntimeLogger(tmp_path / "runtime.jsonl")

    def _fail_open(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(type(logger.path), "open", _fail_open)

    await logger.log("notification", {"event": "model_usage"})


def test_configure_root_text_logging_writes_warning_and_error_to_text_file(tmp_path):
    log_path = configure_root_text_logging(level=logging.WARNING, log_dir=tmp_path / "logs")
    test_logger = logging.getLogger("nexus.test.textlog")
    try:
        test_logger.warning("warning message for text log")
        test_logger.error("error message for text log")
        for handler in logging.getLogger().handlers:
            handler.flush()
    finally:
        for handler in list(logging.getLogger().handlers):
            handler.close()
            logging.getLogger().removeHandler(handler)

    contents = log_path.read_text(encoding="utf-8")

    assert "warning message for text log" in contents
    assert "error message for text log" in contents
    assert "nexus.test.textlog" in contents


@pytest.mark.asyncio
async def test_runtime_metrics_collector_records_usage_and_tool_counts(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global", cli_overrides={"log_format": "json"})
    hooks = HookExecutor()
    log_path = config.log_dir / "runtime.jsonl"
    metrics_path = config.log_dir / "metrics.json"
    register_default_runtime_hooks(
        hooks,
        JsonlRuntimeLogger(log_path),
        metrics_collector=RuntimeMetricsCollector(metrics_path),
    )

    registry = ToolRegistry()
    registry.register(GetTimeTool())
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Checking time."),
                    tool_calls=(ToolCall(call_id="call-1", tool_name="get_time", arguments={}),),
                    usage=UsageSnapshot(
                        prompt_tokens=12,
                        completion_tokens=5,
                        total_tokens=17,
                        estimated_cost_usd=0.0017,
                    ),
                    finish_reason="tool_calls",
                ),
                RuntimeResponse(
                    message=Message(role="assistant", content="Done."),
                    usage=UsageSnapshot(
                        prompt_tokens=8,
                        completion_tokens=2,
                        total_tokens=10,
                        estimated_cost_usd=0.001,
                    ),
                ),
            ]
        ),
        tool_registry=registry,
        hooks=hooks,
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("metrics"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True),
        hooks=hooks,
    )

    result = await run_headless(
        state,
        agent,
        "what time is it",
        auto_confirm=False,
        output_path=None,
        output_format="text",
        quiet=True,
    )

    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert result.response == "Done."
    assert metrics["totals"]["prompt_submissions"] == 1
    assert metrics["totals"]["tool_calls_completed"] == 1
    assert metrics["totals"]["total_tokens"] == 27
    assert metrics["tools"]["get_time"]["completed"] == 1
    assert metrics["sessions"]["metrics"]["estimated_cost_usd"] == 0.0027


def test_redact_payload_replaces_sensitive_keys():
    payload = {"api_key": "secret", "nested": {"token": "abc"}, "safe": "ok"}

    redacted = redact_payload(payload)

    assert redacted == {"api_key": "[REDACTED]", "nested": {"token": "[REDACTED]"}, "safe": "ok"}


def test_redact_payload_scrubs_secret_values_in_text():
    payload = {"output_preview": "API_KEY=sk-1234567890abcdefghijklmnop and safe text"}

    redacted = redact_payload(payload)

    assert "sk-1234567890" not in redacted["output_preview"]
    assert "[REDACTED]" in redacted["output_preview"]


@pytest.mark.asyncio
async def test_audit_trail_records_mutating_actions(tmp_path):
    hooks = HookExecutor()
    trail = JsonlAuditTrail(tmp_path / "audit-trail.jsonl")
    register_audit_hooks(hooks, trail)

    await hooks.emit(
        HookEvent.NOTIFICATION,
        {
            "event": "confirmation_requested",
            "tool_name": "write_file",
            "reason": "Mutating tool requires confirmation.",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "trace_id": "trace-1",
            "call_id": "call-1",
            "arguments": {"path": "notes/out.txt"},
        },
    )
    await hooks.emit(
        HookEvent.POST_TOOL_USE,
        {
            "tool_name": "write_file",
            "arguments": {"path": "notes/out.txt"},
            "call_id": "call-1",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "trace_id": "trace-1",
            "is_mutating": True,
            "is_error": False,
            "output": "Wrote notes/out.txt",
        },
    )

    records = [json.loads(line) for line in (tmp_path / "audit-trail.jsonl").read_text(encoding="utf-8").splitlines()]
    assert records[0]["state"] == "requested"
    assert records[1]["state"] == "executed"
    assert records[1]["rollback"]["supported"] is True
