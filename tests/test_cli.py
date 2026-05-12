from __future__ import annotations

import builtins
import json

import pytest
from rich.console import Console

from nexus.app import main
from nexus.cli.args import args_to_config_overrides
from nexus.cli.headless import EXIT_NEEDS_CONFIRM, EXIT_OK, run_headless
from nexus.config import load_config
from nexus.integrations.fake_model import FakeModelClient
from nexus.memory.store import MemoryStore
from nexus.models import Message, RuntimeResponse, ToolCall, UsageSnapshot
from nexus.runtime.agent import Agent
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import SessionStore, new_snapshot
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, WriteNoteTool


def test_args_to_config_overrides_maps_flags():
    overrides = args_to_config_overrides(mode="plan", model="demo", no_stream=True)

    assert overrides["default_mode"] == "plan"
    assert overrides["model_name"] == "demo"
    assert overrides["stream_output"] is False


def test_args_to_config_overrides_stream_flag_enables_streaming():
    overrides = args_to_config_overrides(stream=True)

    assert overrides["stream_output"] is True


def test_args_to_config_overrides_maps_allow_hidden_paths_flag():
    overrides = args_to_config_overrides(allow_hidden_paths=True)

    assert overrides["allow_hidden_paths"] is True


def test_cli_rejects_stream_and_no_stream_together():
    exit_code = main(["--stream", "--no-stream", "--prompt", "hello"])

    assert exit_code == 2


@pytest.mark.asyncio
async def test_headless_runner_returns_final_response(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(message=Message(role="assistant", content="done")),
            ]
        ),
        tool_registry=registry,
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("headless"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True),
    )

    result = await run_headless(
        state,
        agent,
        "say done",
        auto_confirm=False,
        output_path=None,
        output_format="text",
        quiet=True,
    )

    assert result.exit_code == EXIT_OK
    assert result.response == "done"


@pytest.mark.asyncio
async def test_headless_runner_accumulates_usage_metadata(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="done"),
                    usage=UsageSnapshot(
                        prompt_tokens=10,
                        completion_tokens=4,
                        total_tokens=14,
                        estimated_cost_usd=0.0014,
                    ),
                ),
            ]
        ),
        tool_registry=registry,
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("usage"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True),
    )

    result = await run_headless(
        state,
        agent,
        "say done",
        auto_confirm=False,
        output_path=None,
        output_format="text",
        quiet=True,
    )

    assert result.exit_code == EXIT_OK
    assert state.session.metadata["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "estimated_cost_usd": 0.0014,
    }


@pytest.mark.asyncio
async def test_headless_runner_exits_when_clarification_is_required(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(WriteNoteTool())
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Need a destination path."),
                    tool_calls=(
                        ToolCall(
                            call_id="clarify-1",
                            tool_name="write_note",
                            arguments={"content": "hello"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            ]
        ),
        tool_registry=registry,
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("clarify"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True),
    )

    result = await run_headless(
        state,
        agent,
        "write a note",
        auto_confirm=True,
        output_path=None,
        output_format="text",
        quiet=True,
    )

    assert result.exit_code == EXIT_NEEDS_CONFIRM
    assert "Provide a value for 'path'" in result.error


@pytest.mark.asyncio
async def test_headless_runner_accepts_tty_confirmation_input(tmp_path, monkeypatch):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(WriteNoteTool())
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Writing note."),
                    tool_calls=(
                        ToolCall(
                            call_id="confirm-1",
                            tool_name="write_note",
                            arguments={"path": "notes/out.txt", "content": "hello"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                RuntimeResponse(
                    message=Message(role="assistant", content="Writing note."),
                    tool_calls=(
                        ToolCall(
                            call_id="confirm-1b",
                            tool_name="write_note",
                            arguments={"path": "notes/out.txt", "content": "hello"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                RuntimeResponse(message=Message(role="assistant", content="done")),
            ]
        ),
        tool_registry=registry,
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("headless-confirm"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True),
    )

    monkeypatch.setattr("nexus.cli.headless._can_prompt_for_confirmation", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")

    result = await run_headless(
        state,
        agent,
        "write a note",
        auto_confirm=False,
        output_path=None,
        output_format="text",
        quiet=True,
    )

    assert result.exit_code == EXIT_OK
    assert result.response == "done"
    assert (tmp_path / "notes" / "out.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_headless_runner_exits_when_confirmation_is_required_without_tty(tmp_path, monkeypatch):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(WriteNoteTool())
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Writing note."),
                    tool_calls=(
                        ToolCall(
                            call_id="confirm-2",
                            tool_name="write_note",
                            arguments={"path": "notes/out.txt", "content": "hello"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            ]
        ),
        tool_registry=registry,
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("headless-no-tty"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True),
    )

    monkeypatch.setattr("nexus.cli.headless._can_prompt_for_confirmation", lambda: False)

    result = await run_headless(
        state,
        agent,
        "write a note",
        auto_confirm=False,
        output_path=None,
        output_format="text",
        quiet=True,
    )

    assert result.exit_code == EXIT_NEEDS_CONFIRM
    assert result.error == "Allow tool 'write_note'?"


@pytest.mark.asyncio
async def test_headless_runner_writes_structured_json_to_stdout(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    console = Console(record=True, no_color=True, width=200)
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(message=Message(role="assistant", content="done")),
            ]
        ),
        tool_registry=registry,
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("json-stdout"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    result = await run_headless(
        state,
        agent,
        "say done",
        auto_confirm=False,
        output_path=None,
        output_format="json",
        quiet=True,
    )

    assert result.exit_code == EXIT_OK
    assert '"response": "done"' in console.export_text()


def test_main_no_session_does_not_persist_session(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Use the fake provider so the test does not make real network calls.
    monkeypatch.setenv("AGENT_PROVIDER", "fake")

    exit_code = main(["--prompt", "hello", "--quiet", "--no-session"])

    assert exit_code == EXIT_OK
    assert list((workspace / ".nexus" / "sessions").glob("*.json")) == []


def test_main_doctor_outputs_json_report(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("HOME", str(tmp_path))

    exit_code = main(["doctor", "--output-format", "json"])

    captured = capsys.readouterr().out
    payload = json.loads(captured)
    assert exit_code == 0
    assert payload["overall_status"] in {"pass", "warn"}
    assert any(gate["name"] == "Runtime Integrity" for gate in payload["gates"])
