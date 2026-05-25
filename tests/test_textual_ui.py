from __future__ import annotations

import pytest

from nexus.config import load_config
from nexus.integrations.fake_model import FakeModelClient
from nexus.memory.store import MemoryStore
from nexus.models import Message
from nexus.models import AgentEvent, ToolResult
from nexus.runtime.agent import Agent
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import EphemeralSessionStore, new_snapshot
from nexus.runtime.slash_commands import build_router
from nexus.tools.base import ToolRegistry
from nexus.ui import TerminalUI
from nexus.ui.textual_app import NexusTextualApp, _strip_mouse_escape_sequences


@pytest.mark.asyncio
async def test_textual_repl_leaves_mouse_available_for_terminal_selection(monkeypatch):
    from nexus.ui import textual_app

    calls: list[dict[str, object]] = []

    class FakeApp:
        def __init__(self, *args, **kwargs):
            pass

        async def run_async(self, **kwargs):
            calls.append(kwargs)

        async def finalize_session(self):
            calls.append({"finalized": True})

    monkeypatch.setattr(textual_app, "NexusTextualApp", FakeApp)

    await textual_app.run_textual_repl(None, None, None)  # type: ignore[arg-type]

    assert calls[0]["mouse"] is False
    assert calls[1]["finalized"] is True


def test_textual_input_strips_leaked_mouse_reports():
    value = "hello\x1b[<35;12;5M world[<64;10;20M"

    assert _strip_mouse_escape_sequences(value) == "hello world"


@pytest.mark.asyncio
async def test_textual_app_mounts_and_restores_console(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    original_console = TerminalUI(color=False)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("textual"),
        session_store=EphemeralSessionStore(),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=original_console,
    )
    agent = Agent(model_client=FakeModelClient(), tool_registry=registry)
    app = NexusTextualApp(
        state,
        agent,
        build_router(),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#prompt") is not None
        assert state.console is app.ui
        assert app.query_one("#transcript").max_lines == config.textual_transcript_max_lines

    assert state.console is original_console


@pytest.mark.asyncio
async def test_textual_prompt_history_uses_up_and_down_arrows(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global", cli_overrides={"prompt_history_max_entries": 2})
    registry = ToolRegistry()
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("textual"),
        session_store=EphemeralSessionStore(),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=TerminalUI(color=False),
        history=[
            Message(role="user", content="first instruction"),
            Message(role="assistant", content="done"),
            Message(role="user", content="second instruction"),
            Message(role="user", content="/history 5"),
        ],
    )
    agent = Agent(model_client=FakeModelClient(), tool_registry=registry)
    app = NexusTextualApp(
        state,
        agent,
        build_router(),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.value = "draft"

        await pilot.press("up")
        assert prompt.value == "/history 5"

        await pilot.press("up")
        assert prompt.value == "second instruction"

        await pilot.press("down")
        assert prompt.value == "/history 5"

        await pilot.press("down")
        assert prompt.value == "draft"


@pytest.mark.asyncio
async def test_textual_ctrl_c_copies_selected_text_before_quitting(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("textual"),
        session_store=EphemeralSessionStore(),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=TerminalUI(color=False),
    )
    agent = Agent(model_client=FakeModelClient(), tool_registry=registry)
    app = NexusTextualApp(state, agent, build_router())

    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.get_selected_text = lambda: "copy me"  # type: ignore[method-assign]

        await app.action_copy_or_quit()

        assert app.clipboard == "copy me"
        assert state.should_exit is False


@pytest.mark.asyncio
async def test_textual_streamed_tool_output_is_capped_and_cleaned_up(tmp_path):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={"tool_output_max_chars": 5},
    )
    registry = ToolRegistry()
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("textual"),
        session_store=EphemeralSessionStore(),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=TerminalUI(color=False),
    )
    agent = Agent(model_client=FakeModelClient(), tool_registry=registry)
    app = NexusTextualApp(state, agent, build_router())

    async with app.run_test() as pilot:
        await pilot.pause()
        app.append_tool_output("call-1", "stdout", "abcdefghij")
        assert app._streaming_tool_outputs == {"call-1"}
        assert app._streaming_tool_output_chars["call-1"] == 5
        app.ui.render_event(
            AgentEvent.tool_call_complete(ToolResult(call_id="call-1", tool_name="bash", output="done")),
            stream_output=True,
            show_tool_calls=True,
        )
        assert "call-1" not in app._streaming_tool_outputs
        assert "call-1" not in app._streaming_tool_output_chars
