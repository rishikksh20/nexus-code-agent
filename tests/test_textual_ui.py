from __future__ import annotations

import asyncio
import json

import pytest
from rich.console import Console
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
from nexus.ui.textual_app import NexusTextualApp, TranscriptLog, _strip_mouse_escape_sequences, _user_prompt_block


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


def test_textual_user_prompt_block_is_inline_label():
    block = _user_prompt_block("adjust the TUI")
    console = Console(record=True, no_color=True)
    console.print(block)
    assert "You: adjust the TUI" in console.export_text()


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
async def test_textual_provider_manage_opens_settings_screen(tmp_path):
    from nexus.ui.provider_settings import ProviderSettingsScreen

    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("textual-provider-settings"),
        session_store=EphemeralSessionStore(),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=TerminalUI(color=False),
    )
    app = NexusTextualApp(state, Agent(model_client=FakeModelClient(), tool_registry=registry), build_router())

    async with app.run_test() as pilot:
        await pilot.pause()
        assert state.provider_settings_opener is not None
        state.provider_settings_opener()
        await pilot.pause()
        assert isinstance(app.screen, ProviderSettingsScreen)


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
        assert app._transcript_entries[-1]["type"] == "collapsible"
        assert app._transcript_entries[-1]["expanded_state"] is False
        assert "[+] > bash live output  #call-1" in app._transcript_text()
        assert "abcde" in app._transcript_text()
        assert "[live output capped at 5 chars]" in app._transcript_text()
        app.ui.render_event(
            AgentEvent.tool_call_complete(ToolResult(call_id="call-1", tool_name="bash", output="done")),
            stream_output=True,
            show_tool_calls=True,
        )
        assert "call-1" not in app._streaming_tool_outputs
        assert "call-1" not in app._streaming_tool_output_chars
        assert "call-1" not in app._streaming_tool_output_text
        assert "call-1" not in app._streaming_tool_output_entries
        assert "bash live output" not in app._transcript_text()


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
        assert "Approval response: y" in app._transcript_text()


@pytest.mark.asyncio
async def test_textual_long_error_alert_collapses_to_short_preview(tmp_path):
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
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.print_error("failure detail " * 20)

        collapsed = app._transcript_text()
        assert "[+] Request failed:" in collapsed
        assert "failure detail failure detail" in collapsed
        assert len(collapsed) < 260

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        expanded = app._transcript_text()
        assert "[-] Request failed:" in expanded
        assert "failure detail failure detail failure detail" in expanded


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
async def test_textual_write_completion_collapses_diff_without_echoing_file_content(tmp_path):
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
        assert "[+] ✓ Wrote file calculator/calculator.py" in transcript
        assert "-OLD" in transcript
        assert "+NEW" in transcript
        assert "NEW FILE CONTENT" not in transcript

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        transcript = app._transcript_text()
        assert "Before" in transcript
        assert "After" in transcript
        assert "OLD" in transcript
        assert "NEW" in transcript
        assert "NEW FILE CONTENT" not in transcript


@pytest.mark.asyncio
async def test_textual_approval_request_shows_collapsible_file_diff_preview(tmp_path):
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
    diff = "\n".join(
        [
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -1,20 +1,20 @@",
            *(f"-old {index}\n+new {index}" for index in range(20)),
        ]
    )
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file replaces the entire file.",
        payload={"approval_policy": "on-request"},
        call_id="call-preview",
        arguments={"path": "app.py", "content": "hidden full content"},
        preview={"diff": {"path": "app.py", "unified_diff": diff}},
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "[+] ? Approval required Write file app.py" in collapsed
        assert "Before" in collapsed
        assert "After" in collapsed
        assert "old 0" in collapsed
        assert "new 6" in collapsed
        assert "old 7" in collapsed
        assert "new 19" not in collapsed
        assert "hidden full content" not in collapsed

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        expanded = app._transcript_text()
        assert "[-] ? Approval required Write file app.py" in expanded
        assert "Before" in expanded
        assert "After" in expanded
        assert "new 19" in expanded


@pytest.mark.asyncio
async def test_textual_diff_preview_includes_context_and_line_numbers(tmp_path):
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
    diff = "\n".join(
        [
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -37,7 +37,7 @@",
            " keep 37",
            " keep 38",
            " keep 39",
            "-old 40",
            "+new 40",
            " keep 41",
            " keep 42",
            " keep 43",
        ]
    )
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="edit",
        prompt="Allow edit?",
        reason="edit modifies app.py.",
        payload={"approval_policy": "on-request"},
        call_id="call-context",
        arguments={"path": "app.py", "old_string": "old 40", "new_string": "new 40"},
        preview={"diff": {"path": "app.py", "unified_diff": diff}},
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "37 |  keep 37" in collapsed
        assert "40 | -old 40" in collapsed
        assert "40 | +new 40" in collapsed
        assert "43 |  keep 43" in collapsed


@pytest.mark.asyncio
async def test_textual_write_file_new_file_preview_uses_single_added_view(tmp_path):
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
    content = "\n".join(
        [
            '"""Cosine operation for the calculator."""',
            "",
            "import math",
            "",
            "",
            "def cos(value: float) -> float:",
            "    return math.cos(value)",
        ]
    )
    diff = "\n".join(
        [
            "--- /dev/null",
            "+++ b/calculator/operations/cos.py",
            "@@ -0,0 +1,7 @@",
            *[f"+{line}" for line in content.splitlines()],
        ]
    )
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file creates a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-new-file",
        arguments={"path": "calculator/operations/cos.py", "content": content},
        preview={
            "diff": {
                "path": "calculator/operations/cos.py",
                "is_new_file": True,
                "old_content": "",
                "new_content": content,
                "unified_diff": diff,
            }
        },
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "New file" in collapsed
        assert "Before" not in collapsed
        assert "After" not in collapsed
        assert '1 | +"""Cosine operation for the calculator."""' in collapsed
        assert "6 | +def cos(value: float) -> float:" in collapsed


@pytest.mark.asyncio
async def test_textual_clicking_inline_diff_expand_hint_opens_approval_preview(tmp_path):
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
    content = "\n".join(f"line {index}" for index in range(1, 21))
    diff = "\n".join(
        [
            "--- /dev/null",
            "+++ b/algorithms/binary_search.py",
            "@@ -0,0 +1,20 @@",
            *[f"+{line}" for line in content.splitlines()],
        ]
    )
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file creates a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-inline-expand",
        arguments={"path": "algorithms/binary_search.py", "content": content},
        preview={
            "diff": {
                "path": "algorithms/binary_search.py",
                "is_new_file": True,
                "old_content": "",
                "new_content": content,
                "unified_diff": diff,
            }
        },
    )

    async with app.run_test(size=(160, 60)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        entry = app._transcript_entries[-1]
        assert entry["expanded_state"] is False
        hint_y, hint_line = next(
            (index, line.text)
            for index, line in enumerate(transcript.lines)
            if "click [+] to expand" in line.text
        )
        content_offset_x = transcript.content_region.x - transcript.region.x
        content_offset_y = transcript.content_region.y - transcript.region.y

        await pilot.click(
            transcript,
            offset=(hint_line.index("click") + 1 + content_offset_x, hint_y + content_offset_y),
        )
        await pilot.pause()

        assert entry["expanded_state"] is True
        assert "20 | +line 20" in app._transcript_text()


@pytest.mark.asyncio
async def test_textual_bash_approval_uses_command_block_and_structured_details(tmp_path):
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
        tool_name="bash",
        prompt="Allow bash?",
        reason="Medium-risk bash command requires confirmation.",
        payload={"approval_policy": "on-request"},
        call_id="call-bash",
        arguments={"command": "printf 'hello\\n'", "timeout": 10, "cwd": "calculator"},
        preview={"command": "printf 'hello\\n'"},
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "Command" in collapsed
        assert "printf 'hello\\n'" in collapsed
        assert "params" in collapsed
        assert "timeout=10" in collapsed
        assert "cwd=calculator" in collapsed
        assert "reason" in collapsed
        assert "approval" in collapsed


@pytest.mark.asyncio
async def test_textual_diff_preview_reflows_when_terminal_width_changes(tmp_path):
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
    diff = "\n".join(
        [
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -40,1 +40,1 @@",
            "-old 40",
            "+new 40",
        ]
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.write(app.ui._render_diff_editor_preview(diff, path="app.py"))

        wide = app._transcript_text()
        assert "Before | app.py" in wide
        assert "After | app.py" in wide
        assert wide.index("After | app.py") < wide.index("40 | -old 40")

        await pilot.resize_terminal(80, 40)
        await pilot.pause()

        narrow = app._transcript_text()
        assert narrow.index("After | app.py") > narrow.index("40 | -old 40")
        assert "40 | +new 40" in narrow


def test_textual_wide_diff_preview_uses_full_width_and_keeps_wrapped_rows_aligned():
    from nexus.ui.textual_app import _DiffRow, _ResponsiveDiff, _renderable_plain_text

    renderable = _ResponsiveDiff(
        rows=(
            _DiffRow(
                1,
                1,
                "short",
                "this replacement line is intentionally much longer than the available split panel width so it wraps",
                "change",
            ),
            _DiffRow(2, 2, "next()", "next()", "context"),
        ),
        path="src/example.py",
        language="python",
    )

    rendered = _renderable_plain_text(renderable, width=110)
    lines = rendered.splitlines()

    assert len(lines[0]) == 110
    aligned_context_line = next(line for line in lines if "2 |  next()" in line)
    assert aligned_context_line.count("2 |  next()") == 2


@pytest.mark.asyncio
async def test_textual_collapsible_entries_toggle_by_id(tmp_path):
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
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.write_collapsible(Text("< Ran command"), Text("full output"), summary="1 line")
        collapsed = app._transcript_text()
        assert "[+] < Ran command" in collapsed
        assert "full output" not in collapsed

        entry_id = app._transcript_entries[-1]["id"]
        app.toggle_collapsible(entry_id)

        expanded = app._transcript_text()
        assert "[-] < Ran command" in expanded
        assert "full output" in expanded


@pytest.mark.asyncio
async def test_textual_long_assistant_response_is_expanded_by_default(tmp_path):
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
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.text_complete("\n".join(f"assistant line {i}" for i in range(30))),
            stream_output=True,
            show_tool_calls=True,
        )

        transcript_text = app._transcript_text()
        assert "Assistant:" in transcript_text
        assert "[-] · (30 lines)" in transcript_text
        assert "assistant line 29" in transcript_text
        assert app._transcript_entries[-1]["expanded_state"] is True


@pytest.mark.asyncio
async def test_textual_assistant_header_is_green_label_with_colon(tmp_path):
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
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.text_complete("Found the algorithms/ directory."),
            stream_output=True,
            show_tool_calls=True,
        )

        transcript_text = app._transcript_text()
        assert "Assistant:" in transcript_text
        assert "Found the algorithms/ directory." in transcript_text


@pytest.mark.asyncio
async def test_textual_bash_output_uses_inline_blocks_and_collapses_long_output(tmp_path):
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

    async with app.run_test(size=(120, 60)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start("call-1", "bash", {"command": "uv run pytest"}),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(call_id="call-1", tool_name="bash", output="\n".join(f"line {i}" for i in range(30)))
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        transcript_text = app._transcript_text()
        assert "Command" in transcript_text
        assert "uv run pytest" in transcript_text
        assert "bash output" not in transcript_text
        assert "[+] ✓ Ran uv run pytest" in transcript_text
        assert "Console output" in transcript_text
        assert "line 14" in transcript_text
        assert "line 16" not in transcript_text

        hint_y, hint_line = next(
            (index, line.text)
            for index, line in enumerate(transcript.lines)
            if "click [+] to expand" in line.text
        )
        content_offset_x = transcript.content_region.x - transcript.region.x
        content_offset_y = transcript.content_region.y - transcript.region.y
        await pilot.click(
            transcript,
            offset=(hint_line.index("click") + 1 + content_offset_x, hint_y + content_offset_y),
        )
        await pilot.pause()

        expanded = app._transcript_text()
        assert "[-] ✓ Ran uv run pytest" in expanded
        assert "line 29" in expanded


@pytest.mark.asyncio
async def test_textual_bash_short_output_still_uses_collapsible_console_preview(tmp_path):
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
    app = NexusTextualApp(state, Agent(model_client=FakeModelClient(), tool_registry=registry), build_router())

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start("call-short", "bash", {"command": "printf hello"}),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(ToolResult(call_id="call-short", tool_name="bash", output="hello")),
            stream_output=True,
            show_tool_calls=True,
        )

        entry = app._transcript_entries[-1]
        assert entry["type"] == "collapsible"
        assert entry["expanded_state"] is False
        assert "[+] ✓ Ran printf hello" in app._transcript_text()
        assert "Console output" in app._transcript_text()
        assert "hello" in app._transcript_text()


@pytest.mark.asyncio
async def test_textual_command_argument_tool_start_uses_fenced_command_preview(tmp_path):
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
    app = NexusTextualApp(state, Agent(model_client=FakeModelClient(), tool_registry=registry), build_router())

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start("call-command", "sandbox_exec", {"command": "pwd"}),
            stream_output=True,
            show_tool_calls=True,
        )

        transcript_text = app._transcript_text()
        assert "> sandbox_exec command #call-com" in transcript_text
        assert "Command" in transcript_text
        assert "pwd" in transcript_text
        assert app._supervisor_entry is None


def test_textual_bash_command_preview_builds_markdown_fence():
    from nexus.ui.textual_app import _markdown_code_fence

    assert _markdown_code_fence("printf hello", language="bash") == "```bash\nprintf hello\n```"


def test_textual_bash_console_preview_bounds_single_long_line():
    from nexus.ui.textual_app import _bash_output_block, _renderable_plain_text

    output = ("a" * 1800) + "TAIL"

    collapsed = _renderable_plain_text(_bash_output_block(output, collapsed=True))
    expanded = _renderable_plain_text(_bash_output_block(output))

    assert "TAIL" not in collapsed
    assert "click [+] to expand" in collapsed
    assert "TAIL" in expanded


@pytest.mark.asyncio
async def test_textual_supervisor_tools_use_colored_header_and_indented_status_symbols(tmp_path):
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
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start("call-list", "list_dir", {"path": "algorithms"}),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(call_id="call-list", tool_name="list_dir", output="heap.py", metadata={"duration_ms": 4})
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_start("call-grep", "grep", {"query": "needle"}),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(call_id="call-grep", tool_name="grep", output="not found", is_error=True)
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        header = app._supervisor_entry["header"]
        assert isinstance(header, Text)
        assert header.plain == "● Supervisor Agent"
        assert header.style == "bold cyan"
        transcript_text = app._transcript_text()
        assert "   ✓ Listed algorithms" in transcript_text
        assert "   ✗ Failed query=needle" in transcript_text
        assert "|->" not in transcript_text


@pytest.mark.asyncio
async def test_textual_subagent_tools_render_inside_collapsible_task_block(tmp_path):
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
    result_payload = {
        "schema_version": 1,
        "status": "completed",
        "agent": "subagent_explorer",
        "role": "explorer",
        "task_id": "call-sub",
        "title": "Summarize the algorithms directory",
        "summary": "Algorithms contains heap and table examples.",
        "raw_result": "{\"status\":\"completed\"}",
        "context": {"tool_call_count": 2},
        "recommended_next_action": "continue",
    }

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start(
                "call-sub",
                "subagent_explorer",
                {"title": "Summarize the algorithms directory", "instructions": "Summarize algorithms."},
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_start(
                "inner-1",
                "list_dir",
                {"path": "algorithms"},
                actor="subagent_explorer",
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="inner-1",
                    tool_name="list_dir",
                    output="heap.py\nheap_table.py",
                    metadata={"actor": "subagent_explorer", "duration_ms": 4},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_start(
                "inner-2",
                "read_file",
                {"path": "algorithms/README.md"},
                actor="subagent_explorer",
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="inner-2",
                    tool_name="read_file",
                    output="# Algorithms",
                    metadata={"actor": "subagent_explorer", "duration_ms": 8},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="call-sub",
                    tool_name="subagent_explorer",
                    output=json.dumps(result_payload, indent=2),
                    metadata={"duration_ms": 21000, "title": "Summarize the algorithms directory"},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "[+] ● Explore Task - Summarize the algorithms directory" in collapsed
        assert "completed · 21.0s · 2 tools" in collapsed
        assert "Algorithms contains heap and table examples." in collapsed
        assert "> Delegate" not in collapsed
        assert "< Delegated" not in collapsed
        assert '"schema_version"' not in collapsed
        assert "   ✓ Listed algorithms" not in collapsed

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        expanded = app._transcript_text()
        assert "   ✓ Listed algorithms" in expanded
        assert "   ✓ Read algorithms/README.md" in expanded
        assert "|-->" not in expanded
        assert "4ms" in expanded
        assert "8ms" in expanded
        assert '"schema_version"' in expanded
        assert '"summary": "Algorithms contains heap and table examples."' in expanded


@pytest.mark.asyncio
async def test_textual_file_change_completion_collapses_side_by_side_diff(tmp_path):
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
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start("call-1", "edit", {"path": "calculator.py", "old_string": "old", "new_string": "new"}),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui._tool_preview_by_call_id["call-1"] = {
            "diff": {
                "unified_diff": "--- a/calculator.py\n+++ b/calculator.py\n@@ -1 +1 @@\n-print('old')\n+print('new')\n"
            }
        }
        app.ui.render_event(
            AgentEvent.tool_call_complete(ToolResult(call_id="call-1", tool_name="edit", output="Patched 1 region")),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "[+] ✓ Edited file calculator.py" in collapsed
        assert "-print('old')" in collapsed
        assert "+print('new')" in collapsed
        assert "Before" in collapsed
        assert "After" in collapsed

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        expanded = app._transcript_text()
        assert "Before" in expanded
        assert "After" in expanded
        assert "print('old')" in expanded
        assert "print('new')" in expanded


@pytest.mark.asyncio
async def test_textual_turn_footer_summarizes_tools_edits_and_recovery(tmp_path):
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
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(AgentEvent.agent_start("do work"), stream_output=True, show_tool_calls=True)
        app.record_tool_completion(ToolResult(call_id="a", tool_name="bash", output="failed", is_error=True))
        app.record_tool_completion(ToolResult(call_id="b", tool_name="edit", output="patched"))
        app.ui.render_event(AgentEvent.agent_stop("done"), stream_output=True, show_tool_calls=True)

        transcript_text = app._transcript_text()
        assert "Done" in transcript_text
        assert "2 tools" in transcript_text
        assert "1 edit" in transcript_text
        assert "1 failed" in transcript_text
        assert "1 recovered" in transcript_text


@pytest.mark.asyncio
async def test_textual_footer_shows_context_and_runtime_metadata(tmp_path):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={"agent_mode": "advanced", "model_name": "mistral-medium-latest"},
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
        history=[Message(role="user", content="hello nexus")],
    )
    agent = Agent(model_client=FakeModelClient(), tool_registry=registry)
    app = NexusTextualApp(state, agent, build_router())

    async with app.run_test() as pilot:
        await pilot.pause()
        footer = app.query_one("#footer")
        footer_text = str(footer.render())

        assert "ctx" in footer_text
        assert "%" in footer_text
        assert "mode default" in footer_text
        assert "agent advanced" in footer_text
        assert "thinking True" in footer_text
        assert "budget high" in footer_text
        assert "model mistral-medium-latest" in footer_text
        assert "workspace" in footer_text


@pytest.mark.asyncio
async def test_textual_status_uses_braille_thinking_animation(tmp_path):
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
        app.set_status("Thinking")

        status = app.query_one("#status")
        rendered = str(status.render())
        assert rendered[0] in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        assert "Thinking" in rendered


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
