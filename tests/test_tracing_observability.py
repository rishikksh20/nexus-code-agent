from __future__ import annotations

import json
import logging

from nexus.config import load_config
from nexus.hooks import HookEvent, HookExecutor
from nexus.observability.tracing import OtelHookService, otel_settings_from_config, setup_otel_monitor


async def _emit_sample_turn(hooks: HookExecutor) -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    await hooks.emit(
        HookEvent.USER_PROMPT_SUBMIT,
        {
            "prompt": "sum numbers",
            "effective_prompt": "sum numbers",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "trace_id": trace_id,
            "mode": "default",
        },
    )
    await hooks.emit(
        HookEvent.TURN_START,
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "trace_id": trace_id,
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
            "trace_id": trace_id,
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
            "trace_id": trace_id,
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
            "trace_id": trace_id,
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
            "trace_id": trace_id,
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
            "trace_id": trace_id,
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
            "trace_id": trace_id,
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
            "trace_id": trace_id,
            "headless": True,
        },
    )


def test_otel_settings_read_env_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OTEL_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/api/public/otel")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer token")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "nexus-test")
    monkeypatch.setenv("AGENT_OTEL_RELEASE", "nexus@test")

    config = load_config(tmp_path, global_root=tmp_path / "global")
    settings = otel_settings_from_config(config)

    assert settings.active is True
    assert settings.endpoint == "http://collector:4318/api/public/otel/v1/traces"
    assert settings.headers == "Authorization=Bearer token"
    assert settings.service_name == "nexus-test"
    assert settings.release == "nexus@test"


def test_otel_settings_derive_langfuse_otlp_compat(tmp_path):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={
            "langfuse_enabled": True,
            "langfuse_public_key": "pk-lf-test",
            "langfuse_secret_key": "sk-lf-test",
            "langfuse_base_url": "https://us.cloud.langfuse.com",
        },
    )

    settings = otel_settings_from_config(config)

    assert settings.active is True
    assert settings.endpoint == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    assert settings.headers.startswith("Authorization=Basic ")


async def test_otel_hook_service_writes_turn_model_tool_and_log_spans(tmp_path):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={
            "otel_enabled": True,
            "otel_jsonl_enabled": True,
            "otel_service_name": "nexus-tests",
        },
    )
    monitor = setup_otel_monitor(config)
    hooks = HookExecutor()
    hooks.otel_monitor = monitor
    OtelHookService(monitor, monitor.settings).register(hooks)

    try:
        await _emit_sample_turn(hooks)
    finally:
        monitor.close()

    trace_file = config.log_dir / "traces.jsonl"
    rows = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    names = {row["name"] for row in rows}
    assert "nexus.turn" in names
    assert "nexus.model" in names
    assert "tool.echo_tool" in names
    assert "nexus.log" in names
    assert "context.compaction" in names

    root = next(row for row in rows if row["name"] == "nexus.turn")
    assert root["status"]["code"] == "UNSET"
    assert root["attributes"]["nexus.description"].startswith("Nexus turn trace for session session-1")
    assert any(event["name"] == "turn.output" for event in root["events"])
    output_event = next(event for event in root["events"] if event["name"] == "turn.output")
    assert '"response": "42"' in output_event["attributes"]["nexus.payload.json"]

    model = next(row for row in rows if row["name"] == "nexus.model")
    assert model["attributes"]["nexus.description"].startswith("LLM generation finished")
    assert model["attributes"]["nexus.usage.total_tokens"] == 8