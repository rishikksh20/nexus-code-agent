from __future__ import annotations

import asyncio

import pytest
from rich.text import Text
from textual.geometry import Offset
from textual.selection import Selection

from nexus.config import load_config
from nexus.integrations.fake_model import FakeModelClient
from nexus.memory.store import MemoryStore
from nexus.models import Message
from nexus.models import AgentEvent, ConfirmationKind, ConfirmationRequest, ToolResult
from nexus.models import AgentEventType
from nexus.runtime.agent import Agent
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import EphemeralSessionStore, new_snapshot
from nexus.runtime.slash_commands import build_router
from nexus.tools.base import ToolRegistry
from nexus.ui import TerminalUI
from nexus.ui.textual_app import NexusTextualApp, TranscriptLog, _strip_mouse_escape_sequences


@pytest.mark.asyncio
async def test_textual_repl_enables_mouse_for_scroll_support(monkeypatch):
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

    assert calls[0]["mouse"] is True
    assert calls[1]["finalized"] is True


def test_textual_input_strips_leaked_mouse_reports():
    value = "hello\x1b[<35;12;5M world[<64;10;20M"

    assert _strip_mouse_escape_sequences(value) == "hello world"


def test_clipboard_commands_use_pbcopy_on_macos(monkeypatch):
    from nexus.ui import textual_app

    monkeypatch.setattr(textual_app.sys, "platform", "darwin")
    monkeypatch.setattr(textual_app.shutil, "which", lambda command: "/usr/bin/pbcopy" if command == "pbcopy" else None)

    assert textual_app._clipboard_commands() == [["pbcopy"]]


def test_clipboard_commands_use_linux_clipboards_in_preference_order(monkeypatch):
    from nexus.ui import textual_app

    available = {"wl-copy", "xclip", "xsel"}
    monkeypatch.setattr(textual_app.sys, "platform", "linux")
    monkeypatch.setattr(textual_app.shutil, "which", lambda command: f"/usr/bin/{command}" if command in available else None)

    assert textual_app._clipboard_commands() == [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]


def test_copy_to_system_clipboard_runs_first_available_command(monkeypatch):
    from nexus.ui import textual_app

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(textual_app, "_clipboard_commands", lambda: [["copy-tool"]])

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})

    monkeypatch.setattr(textual_app.subprocess, "run", fake_run)

    assert textual_app._copy_to_system_clipboard("hello") is True
    assert calls[0]["command"] == ["copy-tool"]
    assert calls[0]["input"] == "hello"
    assert calls[0]["text"] is True


def test_transcript_log_preserves_richlog_mouse_drag_selection_behavior():
    assert "on_mouse_up" not in TranscriptLog.__dict__


@pytest.mark.asyncio
async def test_transcript_log_exposes_offsets_for_mouse_selection(tmp_path):
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
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_plain_parts.clear()
        app.write(Text("selectable output"))

        rendered = transcript.render_line(0)
        assert any((segment.style and segment.style.meta.get("offset")) for segment in rendered)


@pytest.mark.asyncio
async def test_transcript_log_selection_copies_rendered_output(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.ui.textual_app._copy_to_system_clipboard", lambda text: True)
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
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_plain_parts.clear()
        app.write(Text("selectable output"))

        partial = transcript.get_selection(Selection.from_offsets(Offset(0, 0), Offset(10, 0)))
        assert partial is not None
        assert partial[0] == "selectable"
        transcript.text_select_all()

        await app.action_copy_or_quit()

        assert app.clipboard == "selectable output"
        assert state.should_exit is False


@pytest.mark.asyncio
async def test_textual_right_click_copies_selected_transcript_output(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.ui.textual_app._copy_to_system_clipboard", lambda text: True)
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
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_plain_parts.clear()
        app.write(Text("right click copy"))
        transcript.text_select_all()

        await pilot.click(transcript, offset=(1, 1), button=3)

        assert app.clipboard == "right click copy"
        assert state.should_exit is False


@pytest.mark.asyncio
async def test_textual_right_click_copies_transcript_when_no_selection(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.ui.textual_app._copy_to_system_clipboard", lambda text: True)
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
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_plain_parts.clear()
        app.write(Text("right click transcript fallback"))

        await pilot.click(transcript, offset=(1, 1), button=3)

        assert app.clipboard == "right click transcript fallback"
        assert state.should_exit is False


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
async def test_textual_shift_tab_cycles_focus_between_prompt_and_transcript(tmp_path):
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
        prompt = app.query_one("#prompt")
        transcript = app.query_one("#transcript")

        assert app.focused is prompt

        await pilot.press("shift+tab")
        assert app.focused is transcript

        await pilot.press("shift+tab")
        assert app.focused is prompt


@pytest.mark.asyncio
async def test_textual_tab_cycles_focus_between_panels(tmp_path):
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
        prompt = app.query_one("#prompt")
        transcript = app.query_one("#transcript")

        # Starts with input focused.
        assert app.focused is prompt

        # Tab moves focus to the transcript pane.
        await pilot.press("tab")
        assert app.focused is transcript
        assert prompt.value == ""  # no tab character inserted

        # Tab again cycles back to input.
        await pilot.press("tab")
        assert app.focused is prompt


@pytest.mark.asyncio
async def test_textual_ctrl_c_copies_selected_text_before_quitting(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.ui.textual_app._copy_to_system_clipboard", lambda text: True)
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
async def test_textual_ctrl_c_copies_selected_prompt_text_before_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.ui.textual_app._copy_to_system_clipboard", lambda text: True)
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
        app.screen.get_selected_text = lambda: "transcript selection"  # type: ignore[method-assign]
        prompt = app.query_one("#prompt")
        prompt.value = "copy this input"
        prompt.select_all()

        await app.action_copy_or_quit()

        assert app.clipboard == "copy this input"
        assert state.should_exit is False


@pytest.mark.asyncio
async def test_textual_ctrl_c_copies_focused_transcript_when_no_selection(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.ui.textual_app._copy_to_system_clipboard", lambda text: True)
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
        app.screen.get_selected_text = lambda: ""  # type: ignore[method-assign]
        app.write(Text("copy transcript output"))
        transcript = app.query_one("#transcript")
        await pilot.press("shift+tab")

        assert app.focused is transcript

        await app.action_copy_or_quit()

        assert "copy transcript output" in app.clipboard
        assert state.should_exit is False


@pytest.mark.asyncio
async def test_textual_ctrl_c_copies_transcript_when_not_focused(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.ui.textual_app._copy_to_system_clipboard", lambda text: True)
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
        app.screen.get_selected_text = lambda: ""  # type: ignore[method-assign]
        app.write(Text("copy transcript even when prompt focused"))

        assert app.focused is app.query_one("#prompt")

        await app.action_copy_or_quit()

        assert "copy transcript even when prompt focused" in app.clipboard
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


@pytest.mark.asyncio
async def test_textual_ask_echoes_input_prompt_to_transcript(tmp_path):
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
        recorded: list[str] = []
        original_write = app.write

        def capture(renderable):
            recorded.append(str(renderable))
            original_write(renderable)

        app.write = capture  # type: ignore[method-assign]
        task = asyncio.create_task(app.ask("Allow? yes once / turn / session"))
        await pilot.pause()

        assert any("Input required: Allow? yes once / turn / session" in item for item in recorded)

        prompt = app.query_one("#prompt")
        prompt.value = "y"
        await pilot.press("enter")

        assert await task == "y"


@pytest.mark.asyncio
async def test_textual_transcript_plain_text_handles_theme_styles(tmp_path):
    from rich.panel import Panel

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
        app.write(Panel(Text("styled", style="tool.write"), title=Text("Request failed", style="error")))
        assert "styled" in app._transcript_text()


@pytest.mark.asyncio
async def test_textual_write_approval_shows_diff_only_in_approval_panel(tmp_path):
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

    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file replaces the entire file.",
        payload={"approval_policy": "on-request", "actor": "subagent_execution"},
        call_id="call-1",
        arguments={"path": "calculator/calculator.py", "content": "NEW FILE CONTENT"},
        preview={
            "diff": {
                "path": "calculator/calculator.py",
                "unified_diff": "--- a/calculator/calculator.py\n+++ b/calculator/calculator.py\n-OLD\n+NEW",
            }
        },
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(call_id="call-1", tool_name="write_file", output="NEW FILE CONTENT")
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        transcript = app._transcript_text()
        assert "+++ b/calculator/calculator.py" in transcript
        assert "-OLD" in transcript
        assert "+NEW" in transcript
        assert "NEW FILE CONTENT" not in transcript


@pytest.mark.asyncio
async def test_textual_approval_callback_reprompts_until_valid_answer(tmp_path):
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

    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="Mutating tool requires confirmation.",
        payload={"approval_policy": "on-request"},
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        prompt = app.query_one("#prompt")
        prompt.value = "maybe"
        await pilot.press("enter")
        await pilot.pause()

        prompt.value = "t"
        await pilot.press("enter")

        response = await task
        assert response.approved is True
        assert response.scope == "turn"


@pytest.mark.asyncio
async def test_textual_approval_callback_handles_invalid_policy_payload(tmp_path):
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

    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="Mutating tool requires confirmation.",
        payload={"approval_policy": "not-a-policy"},
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        prompt = app.query_one("#prompt")
        prompt.value = "y"
        await pilot.press("enter")

        response = await task
        assert response.approved is True


@pytest.mark.asyncio
async def test_textual_clarification_callback_reprompts_until_non_empty(tmp_path):
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

    request = ConfirmationRequest(
        kind=ConfirmationKind.CLARIFICATION,
        tool_name="write_file",
        prompt="Need content",
        reason="Missing required field.",
        payload={"field": "content"},
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        prompt = app.query_one("#prompt")
        prompt.value = ""
        await pilot.press("enter")
        await pilot.pause()

        prompt.value = "filled"
        await pilot.press("enter")

        response = await task
        assert response.clarification == "filled"
