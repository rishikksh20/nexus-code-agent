from __future__ import annotations

import asyncio
import json
import os
import tomllib
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text
from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Button, Checkbox, Input, OptionList, Select

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
from nexus.ui.textual_app import (
    FileChangePreviewScreen,
    NexusTextualApp,
    TranscriptLog,
    _bash_command_block,
    _context_pie_icon,
    _renderable_plain_text,
    _strip_mouse_escape_sequences,
    _user_prompt_block,
)


def _new_textual_app(tmp_path, *, cli_overrides: dict[str, object] | None = None) -> NexusTextualApp:
    config = load_config(tmp_path, global_root=tmp_path / "global", cli_overrides=cli_overrides)
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
    return NexusTextualApp(state, agent, build_router())


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


def test_textual_context_pie_uses_nearest_fill_level():
    assert _context_pie_icon(12.4) == "○"
    assert _context_pie_icon(12.5) == "◔"
    assert _context_pie_icon(51.0) == "◑"
    assert _context_pie_icon(74.0) == "◕"
    assert _context_pie_icon(87.5) == "●"


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
async def test_textual_setup_opens_wizard_and_saves_workspace_model(tmp_path, monkeypatch):
    from nexus.ui.model_setup import ModelSetupScreen

    for key in ("OPENAI_API_KEY", "API_KEY", "MODEL", "PROVIDER", "BASE_URL", "AGENT_MODEL_NAME", "AGENT_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    app = _new_textual_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert await app.router.dispatch(app.state, "/setup") is True
        await pilot.pause()
        assert isinstance(app.screen, ModelSetupScreen)
        await pilot.click(app.screen, offset=(1, 16))
        await pilot.pause()
        screen = app.screen
        screen._load_provider("openai")
        screen._load_model_choice("builtin:gpt-4o")
        assert screen.query_one("#setup-profile-row").display is False
        assert screen.query_one("#setup-base-url-env-row").display is False
        assert screen.query_one("#setup-api-key-env-row").display is False
        assert screen.query_one("#setup-base-url", Input).value == "https://api.openai.com/v1"
        screen.query_one("#setup-thinking-enabled", Checkbox).value = True
        screen.query_one("#setup-thinking-mode", Select).value = "reasoning_effort"
        screen.query_one("#setup-api-key", Input).value = "sk-test"

        screen._save_setup()
        await pilot.pause()

    local = tomllib.loads(app.state.config.local_config_file.read_text(encoding="utf-8"))
    profile = local["models"]["openai-gpt-4o"]
    assert local["active_model_profile"] == "openai-gpt-4o"
    assert local["providers"]["openai"]["api_key_env"] == "OPENAI_API_KEY"
    assert profile["provider"] == "openai"
    assert profile["model_name"] == "gpt-4o"
    assert profile["thinking"]["enabled"] is True
    assert profile["thinking"]["mode"] == "reasoning_effort"
    assert profile["thinking"]["reasoning_effort"] == "high"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "BASE_URL=https://api.openai.com/v1\nOPENAI_API_KEY=sk-test\n"
    assert app.state.config.provider == "openai"
    assert app.state.config.model_name == "gpt-4o"
    for key in ("OPENAI_API_KEY", "BASE_URL"):
        os.environ.pop(key, None)


@pytest.mark.asyncio
async def test_textual_setup_big_pickle_prefills_openai_compatible_budget_model(tmp_path, monkeypatch):
    from nexus.ui.model_setup import ModelSetupScreen

    for key in ("API_KEY", "BASE_URL", "MODEL", "PROVIDER", "AGENT_MODEL_NAME", "AGENT_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    app = _new_textual_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert await app.router.dispatch(app.state, "/setup") is True
        await pilot.pause()
        assert isinstance(app.screen, ModelSetupScreen)
        screen = app.screen
        screen._load_provider("openai-compatible")
        screen._load_model_choice("builtin:big-pickle")

        assert screen.query_one("#setup-profile-name", Input).value == "openai-compatible-big-pickle"
        assert screen.query_one("#setup-model-name", Input).value == "big-pickle"
        assert screen.query_one("#setup-context", Input).value == "200000"
        assert screen.query_one("#setup-max-output", Input).value == "32000"
        assert screen.query_one("#setup-reserved-output", Input).value == "32000"
        assert screen.query_one("#setup-api-key-env", Input).value == "API_KEY"
        assert screen.query_one("#setup-base-url-env", Input).value == "BASE_URL"
        assert screen.query_one("#setup-thinking-mode", Select).value == "budget_tokens"
        assert screen.query_one("#setup-thinking-budget", Input).value == "4096"


@pytest.mark.asyncio
async def test_textual_setup_inputs_support_copy_cut_and_paste(tmp_path, monkeypatch):
    from textual import events

    from nexus.ui.model_setup import ModelSetupScreen

    monkeypatch.setattr("nexus.ui.textual_app._copy_to_system_clipboard", lambda text: True)
    app = _new_textual_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert await app.router.dispatch(app.state, "/setup") is True
        await pilot.pause()
        assert isinstance(app.screen, ModelSetupScreen)
        screen = app.screen
        field = screen.query_one("#setup-model-name", Input)
        field.value = ""
        field.focus()
        await pilot.pause()

        app.copy_to_clipboard("keyboard-paste")
        await pilot.press("ctrl+v")
        assert field.value == "keyboard-paste"

        field.value = ""
        screen.on_paste(events.Paste("pasted-model\nignored"))
        assert field.value == "pasted-model"

        field.select_all()
        screen.action_copy_focused_input()
        assert app.clipboard == "pasted-model"

        app.copy_to_clipboard("replacement")
        screen.action_paste_focused_input()
        assert field.value == "replacement"

        field.select_all()
        screen.action_cut_focused_input()
        assert app.clipboard == "replacement"
        assert field.value == ""


@pytest.mark.asyncio
async def test_textual_setup_custom_model_auto_names_profile_and_writes_env(tmp_path, monkeypatch):
    from nexus.ui.model_setup import ModelSetupScreen

    for key in ("API_KEY", "BASE_URL", "MODEL", "PROVIDER", "AGENT_MODEL_NAME", "AGENT_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    app = _new_textual_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert await app.router.dispatch(app.state, "/setup") is True
        await pilot.pause()
        assert isinstance(app.screen, ModelSetupScreen)
        screen = app.screen
        screen._load_provider("openai-compatible")
        screen._load_model_choice("custom:")
        assert screen.query_one("#setup-extra-row").display is False
        await screen.on_button_pressed(SimpleNamespace(button=SimpleNamespace(id="setup-add-field")))  # type: ignore[arg-type]
        await pilot.pause()
        assert screen.query_one("#setup-extra-row").display is True
        screen.query_one("#setup-profile-name", Input).value = ""
        screen.query_one("#setup-model-name", Input).value = "my custom/model"
        screen.query_one("#setup-base-url", Input).value = "https://llm.example.test/v1"
        screen.query_one("#setup-api-key", Input).value = "custom-key"
        screen.query_one("#setup-extra-key", Input).value = "CUSTOM_HEADER"
        screen.query_one("#setup-extra-value", Input).value = "enabled"

        screen._save_setup()
        await pilot.pause()

    local = tomllib.loads(app.state.config.local_config_file.read_text(encoding="utf-8"))
    profile = local["models"]["openai-compatible-my-custom-model"]
    assert local["active_model_profile"] == "openai-compatible-my-custom-model"
    assert profile["provider"] == "openai-compatible"
    assert profile["model_name"] == "my custom/model"
    assert (
        tmp_path / ".env"
    ).read_text(encoding="utf-8") == "BASE_URL=https://llm.example.test/v1\nAPI_KEY=custom-key\nCUSTOM_HEADER=enabled\n"
    assert app.state.config.provider == "openai-compatible"
    assert app.state.config.model_name == "my custom/model"
    for key in ("API_KEY", "BASE_URL", "CUSTOM_HEADER"):
        os.environ.pop(key, None)


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
async def test_textual_slash_command_dropdown_filters_commands_with_descriptions(tmp_path):
    app = _new_textual_app(tmp_path)

    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt")
        suggestions = app.query_one("#slash-suggestions", OptionList)

        assert suggestions.display is False

        prompt.value = "/"
        await pilot.pause()

        assert suggestions.display is True
        assert suggestions.option_count > 5
        assert suggestions.region.height == 12
        assert suggestions.region.y + suggestions.region.height <= prompt.region.y
        all_text = "\n".join(
            _renderable_plain_text(suggestions.get_option_at_index(index).prompt, width=160)
            for index in range(suggestions.option_count)
        )
        assert all_text.index("/abort") < all_text.index("/agent")
        assert "/provider" in all_text
        assert "Show or update model provider and session parameters." in all_text

        prompt.value = "/pro"
        await pilot.pause()

        filtered_text = "\n".join(
            _renderable_plain_text(suggestions.get_option_at_index(index).prompt, width=160)
            for index in range(suggestions.option_count)
        )
        assert "/provider" in filtered_text
        assert "/tools" not in filtered_text

        await pilot.press("enter")
        assert prompt.value == "/provider"
        assert suggestions.display is False

        prompt.value = "/"
        await pilot.pause()
        await pilot.press("down")
        assert suggestions.highlighted == 1
        await pilot.press("enter")
        assert prompt.value == "/agent"

        prompt.value = "/tools"
        await pilot.pause()
        assert suggestions.option_count == 1
        assert suggestions.region.height == 3
        assert suggestions.region.y + suggestions.region.height <= prompt.region.y
        await pilot.click(suggestions, offset=(2, 1))
        await pilot.pause()
        assert prompt.value == "/tools"
        assert suggestions.display is False

        prompt.value = "/zzzz"
        await pilot.pause()

        assert suggestions.display is False

        prompt.value = "hello"
        await pilot.pause()

        assert suggestions.display is False


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
        await pilot.pause()

        assert isinstance(app.screen, FileChangePreviewScreen)
        expanded = _renderable_plain_text(app.screen.preview_renderable, width=160)
        assert app._transcript_entries[-1]["expanded_state"] is False
        assert "Before | app.py" in expanded
        assert "After | app.py" in expanded
        assert "new 19" in expanded


@pytest.mark.asyncio
async def test_textual_approval_tile_collapses_after_keyboard_turn_approval(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file creates a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-keyboard-turn-preview",
        arguments={"path": "turn_approved.py", "content": "secret = True\n"},
        preview={"diff": {"path": "turn_approved.py", "old_content": "", "new_content": "secret = True\n"}},
    )

    async with app.run_test(size=(140, 40)) as pilot:
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
        approval_entry = app._transcript_entries[-1]
        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        prompt = app.query_one("#prompt")
        prompt.value = "t"
        await pilot.press("enter")

        response = await asyncio.wait_for(task, timeout=1)
        assert response.approved is True
        assert response.scope == "turn"
        assert approval_entry["expanded_state"] is False
        assert approval_entry["preview"] is None
        assert approval_entry["approval_resolved"] is True

        collapsed = app._transcript_text()
        assert "[+] ✓ Approval Request Write file turn_approved.py · approved for turn" in collapsed
        assert "secret = True" not in collapsed

        app.toggle_collapsible(approval_entry["id"])
        await pilot.pause()

        assert isinstance(app.screen, FileChangePreviewScreen)
        assert app.screen.query_one("#file-preview-accept", Button).disabled is True
        assert app.screen.query_one("#file-preview-reject", Button).disabled is True


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

        assert entry["expanded_state"] is False
        assert isinstance(app.screen, FileChangePreviewScreen)
        preview = _renderable_plain_text(app.screen.preview_renderable, width=160)
        assert "Before | algorithms/binary_search.py" in preview
        assert "After | algorithms/binary_search.py" in preview
        assert "20 | +line 20" in preview


@pytest.mark.asyncio
async def test_textual_write_file_create_preview_opens_read_only_before_after_screen(tmp_path):
    app = _new_textual_app(tmp_path)
    content = "alpha\nbeta\ngamma"
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file creates a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-create-preview",
        arguments={"path": "new_module.py", "content": content},
        preview={
            "diff": {
                "path": "new_module.py",
                "is_new_file": True,
                "old_content": "",
                "new_content": content,
            }
        },
    )

    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        await pilot.pause()

        assert isinstance(app.screen, FileChangePreviewScreen)
        assert not list(app.screen.query(Input))
        preview = _renderable_plain_text(app.screen.preview_renderable, width=160)
        assert "Before | new_module.py" in preview
        assert "After | new_module.py" in preview
        assert "1 | +alpha" in preview
        assert "3 | +gamma" in preview


@pytest.mark.asyncio
async def test_textual_write_file_overwrite_preview_shows_before_and_after(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file overwrites a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-overwrite-preview",
        arguments={"path": "app.py", "content": "value = 2\n"},
        preview={
            "diff": {
                "path": "app.py",
                "old_content": "value = 1\n",
                "new_content": "value = 2\n",
            }
        },
    )

    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        await pilot.pause()

        assert isinstance(app.screen, FileChangePreviewScreen)
        preview = _renderable_plain_text(app.screen.preview_renderable, width=160)
        assert "Before | app.py" in preview
        assert "After | app.py" in preview
        assert "1 | -value = 1" in preview
        assert "1 | +value = 2" in preview


@pytest.mark.asyncio
async def test_textual_edit_preview_shows_before_and_after(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="edit",
        prompt="Allow edit?",
        reason="edit modifies a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-edit-preview",
        arguments={"path": "app.py", "old_string": "return old", "new_string": "return new"},
        preview={
            "diff": {
                "path": "app.py",
                "old_content": "def run():\n    return old\n",
                "new_content": "def run():\n    return new\n",
            }
        },
    )

    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        await pilot.pause()

        assert isinstance(app.screen, FileChangePreviewScreen)
        preview = _renderable_plain_text(app.screen.preview_renderable, width=160)
        assert "Before | app.py" in preview
        assert "After | app.py" in preview
        assert "2 | -    return old" in preview
        assert "2 | +    return new" in preview


@pytest.mark.asyncio
async def test_textual_file_preview_accept_resolves_once_only_approval(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file creates a file.",
        payload={"approval_policy": "approve-session"},
        call_id="call-accept-preview",
        arguments={"path": "accepted.py", "content": "accepted = True\n"},
        preview={"diff": {"path": "accepted.py", "old_content": "", "new_content": "accepted = True\n"}},
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )
        approval_entry = app._transcript_entries[-1]
        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        app.toggle_collapsible(approval_entry["id"])
        await pilot.pause()
        await pilot.click(app.screen.query_one("#file-preview-accept", Button))

        response = await asyncio.wait_for(task, timeout=1)
        assert response.approved is True
        assert response.scope == "once"
        assert app._active_file_preview_screen is None
        assert approval_entry["approval_resolved"] is True

        app.toggle_collapsible(approval_entry["id"])
        await pilot.pause()

        assert app.screen.query_one("#file-preview-accept", Button).disabled is True
        assert app.screen.query_one("#file-preview-reject", Button).disabled is True


@pytest.mark.asyncio
async def test_textual_file_preview_reject_denies_approval(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="edit",
        prompt="Allow edit?",
        reason="edit modifies a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-reject-preview",
        arguments={"path": "app.py", "old_string": "old", "new_string": "new"},
        preview={"diff": {"path": "app.py", "old_content": "old\n", "new_content": "new\n"}},
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )
        approval_entry = app._transcript_entries[-1]
        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        app.toggle_collapsible(approval_entry["id"])
        await pilot.pause()
        await pilot.click(app.screen.query_one("#file-preview-reject", Button))

        response = await asyncio.wait_for(task, timeout=1)
        assert response.denied is True
        assert app._active_file_preview_screen is None
        assert approval_entry["approval_resolved"] is True

        app.toggle_collapsible(approval_entry["id"])
        await pilot.pause()

        assert app.screen.query_one("#file-preview-accept", Button).disabled is True
        assert app.screen.query_one("#file-preview-reject", Button).disabled is True


@pytest.mark.asyncio
async def test_textual_completed_file_change_opens_read_only_preview(tmp_path):
    app = _new_textual_app(tmp_path)
    args = {"path": "auto_edit.py", "old_string": "value = 1", "new_string": "value = 2"}
    preview = {
        "diff": {
            "path": "auto_edit.py",
            "old_content": "value = 1\n",
            "new_content": "value = 2\n",
        }
    }

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start("call-auto-edit", "edit", args, preview=preview),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="call-auto-edit",
                    tool_name="edit",
                    output="Patched 1 region.",
                    metadata={"path": "auto_edit.py"},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        complete_entry = app._transcript_entries[-1]
        assert complete_entry["clickable_path"] == "auto_edit.py"
        assert "[+] ✓ Edited file auto_edit.py" in app._transcript_text()

        app.toggle_collapsible(complete_entry["id"])
        await pilot.pause()

        assert isinstance(app.screen, FileChangePreviewScreen)
        assert app.screen.query_one("#file-preview-accept", Button).disabled is True
        assert app.screen.query_one("#file-preview-reject", Button).disabled is True
        preview_text = _renderable_plain_text(app.screen.preview_renderable, width=140)
        assert "Before | auto_edit.py" in preview_text
        assert "After | auto_edit.py" in preview_text
        assert "1 | -value = 1" in preview_text
        assert "1 | +value = 2" in preview_text


@pytest.mark.asyncio
async def test_textual_subagent_file_change_absorbs_resolved_approval_into_tool_row(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file creates a file.",
        payload={"approval_policy": "on-request", "actor": "subagent_execution"},
        call_id="inner-write",
        arguments={"path": "algorithms/go/lru.go", "content": "package main\n"},
        preview={
            "diff": {
                "path": "algorithms/go/lru.go",
                "old_content": "",
                "new_content": "package main\n",
            }
        },
    )

    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start(
                "call-sub",
                "subagent_execution",
                {"title": "Add in-depth comments to LRU cache implementation"},
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )
        approval_task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        prompt = app.query_one("#prompt")
        prompt.value = "y"
        await pilot.press("enter")
        response = await asyncio.wait_for(approval_task, timeout=1)
        assert response.approved is True
        assert "Approval Request Write file algorithms/go/lru.go" in app._transcript_text()

        app.ui.render_event(
            AgentEvent.tool_call_start(
                "inner-write",
                "write_file",
                {"path": "algorithms/go/lru.go", "content": "package main\n"},
                actor="subagent_execution",
                preview={
                    "diff": {
                        "path": "algorithms/go/lru.go",
                        "old_content": "",
                        "new_content": "package main\n",
                    }
                },
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="inner-write",
                    tool_name="write_file",
                    output="Created algorithms/go/lru.go",
                    metadata={"actor": "subagent_execution", "path": "algorithms/go/lru.go", "duration_ms": 1},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        transcript_text = app._transcript_text()
        assert "Approval Request Write file algorithms/go/lru.go" not in transcript_text
        assert "Input required: Allow?" not in transcript_text
        assert "Approval response: y" not in transcript_text
        assert "   [+] ✓ Wrote file algorithms/go/lru.go" in transcript_text
        assert app.has_file_preview("inner-write") is True
        row = app._subagent_entries_by_actor["subagent_execution"]["subagent_tool_rows"]["inner-write"]
        assert any(str(span.style) == "bold tool.write" for span in row.spans)

        app.open_file_change_preview_for_call("inner-write")
        await pilot.pause()

        assert isinstance(app.screen, FileChangePreviewScreen)
        assert app.screen.query_one("#file-preview-accept", Button).disabled is True
        assert app.screen.query_one("#file-preview-reject", Button).disabled is True
        preview_text = _renderable_plain_text(app.screen.preview_renderable, width=150)
        assert "Before | algorithms/go/lru.go" in preview_text
        assert "After | algorithms/go/lru.go" in preview_text
        assert "1 | +package main" in preview_text


@pytest.mark.asyncio
async def test_textual_subagent_bash_approval_blends_into_collapsed_command_row(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="bash",
        prompt="Allow bash?",
        reason="High-risk bash command requires confirmation.",
        payload={"approval_policy": "on-request", "actor": "subagent_execution", "risk_level": "dangerous"},
        call_id="inner-bash",
        arguments={"command": "rm -rf build", "cwd": "algorithms/go"},
        preview={"command": "rm -rf build"},
    )

    async with app.run_test(size=(150, 50)) as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start(
                "call-sub",
                "subagent_execution",
                {"title": "Inspect Go algorithms"},
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )

        approval_wait = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        pending_text = app._transcript_text()
        assert "Approval Request bash" not in pending_text
        assert "   [+] ? Bash Run rm -rf build" in pending_text
        assert "approval required" in pending_text

        prompt = app.query_one("#prompt")
        prompt.value = "y"
        await pilot.press("enter")
        response = await asyncio.wait_for(approval_wait, timeout=1)
        assert response.approved is True

        app.ui.render_event(
            AgentEvent.tool_call_start(
                "inner-bash",
                "bash",
                {"command": "rm -rf build", "cwd": "algorithms/go"},
                actor="subagent_execution",
                preview={"command": "rm -rf build"},
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.append_tool_output("inner-bash", "stdout", "live line\n")
        live_collapsed = app._transcript_text()
        assert "bash live output" not in live_collapsed
        assert "live line" not in live_collapsed

        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="inner-bash",
                    tool_name="bash",
                    output="removed build",
                    metadata={"actor": "subagent_execution", "duration_ms": 5},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "Input required: Allow?" not in collapsed
        assert "Approval response: y" not in collapsed
        assert "   [+] ✓ Bash Run rm -rf build" in collapsed
        assert "Console output" not in collapsed
        assert "removed build" not in collapsed

        app.toggle_subagent_command_detail("inner-bash")
        await pilot.pause()

        expanded = app._transcript_text()
        assert "   [-] ✓ Bash Run rm -rf build" in expanded
        assert "Command" in expanded
        assert "Console output" in expanded
        assert "removed build" in expanded


@pytest.mark.asyncio
async def test_textual_file_preview_close_leaves_keyboard_approval_active(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file creates a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-close-preview",
        arguments={"path": "close_preview.py", "content": "value = 1\n"},
        preview={"diff": {"path": "close_preview.py", "old_content": "", "new_content": "value = 1\n"}},
    )

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=True,
            show_tool_calls=True,
        )
        approval_entry = app._transcript_entries[-1]
        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        app.toggle_collapsible(approval_entry["id"])
        await pilot.pause()
        await pilot.click(app.screen.query_one("#file-preview-close", Button))
        await pilot.pause()

        assert not task.done()
        prompt = app.query_one("#prompt")
        prompt.value = "n"
        await pilot.press("enter")

        response = await asyncio.wait_for(task, timeout=1)
        assert response.denied is True


@pytest.mark.asyncio
async def test_textual_keyboard_approval_still_works_without_preview_action(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="write_file creates a file.",
        payload={"approval_policy": "on-request"},
        call_id="call-keyboard-approval",
        arguments={"path": "keyboard.py", "content": "ok = True\n"},
        preview={"diff": {"path": "keyboard.py", "old_content": "", "new_content": "ok = True\n"}},
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()

        prompt = app.query_one("#prompt")
        prompt.value = "y"
        await pilot.press("enter")

        response = await asyncio.wait_for(task, timeout=1)
        assert response.approved is True
        assert response.scope == "once"


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
async def test_textual_bash_approval_absorbs_into_completed_bash_row(tmp_path):
    app = _new_textual_app(tmp_path)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="bash",
        prompt="Allow bash?",
        reason="Medium-risk bash command requires confirmation: `go run lfu.go 2>&1`",
        payload={"approval_policy": "on-request"},
        call_id="call-bash-approval",
        arguments={"command": "go run lfu.go 2>&1", "cwd": "algorithms/go"},
        preview={"command": "go run lfu.go 2>&1"},
    )

    async with app.run_test(size=(150, 50)) as pilot:
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
        approval_task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()
        prompt = app.query_one("#prompt")
        prompt.value = "y"
        await pilot.press("enter")
        response = await asyncio.wait_for(approval_task, timeout=1)
        assert response.approved is True

        app.ui.render_event(
            AgentEvent.tool_call_start(
                "call-bash-approval",
                "bash",
                {"command": "go run lfu.go 2>&1", "cwd": "algorithms/go"},
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="call-bash-approval",
                    tool_name="bash",
                    output="/bin/bash: go: command not found\nExit code: 127",
                    is_error=True,
                    metadata={"duration_ms": 24},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "Approval Request bash" not in collapsed
        assert "Input required: Allow?" not in collapsed
        assert "Approval response: y" not in collapsed
        assert "Run Command : #call-ba" not in collapsed
        assert "[+] ✗ Bash Run algorithms/go · failed:" in collapsed
        assert "Command" not in collapsed
        assert "Console output" not in collapsed

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        expanded = app._transcript_text()
        assert "Command" in expanded
        assert "go run lfu.go 2>&1" in expanded
        assert "Console output" in expanded
        assert "/bin/bash: go: command not found" in expanded


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
        assert "Run Command : #call-1" not in transcript_text
        assert "Command" not in transcript_text
        assert "bash output" not in transcript_text
        assert "[+] ✓ Bash Run uv run pytest" in transcript_text
        assert "Console output" not in transcript_text
        assert "line 14" not in transcript_text

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        await pilot.pause()

        expanded = app._transcript_text()
        assert "[-] ✓ Bash Run uv run pytest" in expanded
        assert "Command" in expanded
        assert "Console output" in expanded
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
        assert "[+] ✓ Bash Run printf hello" in app._transcript_text()
        assert "Console output" not in app._transcript_text()

        app.toggle_collapsible(entry["id"])
        await pilot.pause()

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
        assert "[+] · sandbox_exec Run pwd" in transcript_text
        assert "> sandbox_exec command" not in transcript_text
        assert "Command" not in transcript_text
        assert app._supervisor_entry is None
        assert _bash_command_block("pwd").label_style == "bold tool.shell"


@pytest.mark.asyncio
async def test_textual_git_tool_uses_collapsible_command_tile(tmp_path):
    app = _new_textual_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start("call-git", "git_diff", {"target": "head", "path": "nexus/ui/textual_app.py"}),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(call_id="call-git", tool_name="git_diff", output="diff --git a/file b/file")
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        transcript_text = app._transcript_text()
        assert app._supervisor_entry is None
        assert "[+] ✓ Git Diff nexus/ui/textual_app.py · done" in transcript_text
        assert "git diff HEAD -- nexus/ui/textual_app.py" not in transcript_text
        assert "Console output" not in transcript_text
        assert "diff --git" not in transcript_text

        app.toggle_collapsible(app._transcript_entries[-1]["id"])
        await pilot.pause()

        expanded = app._transcript_text()
        assert "git diff HEAD -- nexus/ui/textual_app.py" in expanded
        assert "Console output" in expanded
        assert "diff --git" in expanded


@pytest.mark.asyncio
async def test_textual_failed_tool_json_output_uses_readable_reason(tmp_path):
    app = _new_textual_app(tmp_path)
    payload = {
        "tool": "run_tests",
        "command": ["uv", "run", "pytest"],
        "exit_code": 1,
        "stderr_tail": "FAILED tests/test_demo.py::test_demo",
    }

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(
            AgentEvent.tool_call_start("call-tests", "run_tests", {}),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="call-tests",
                    tool_name="run_tests",
                    output=json.dumps(payload, indent=2),
                    is_error=True,
                    metadata=payload,
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        transcript_text = app._transcript_text()
        assert "[+] ✗ Test Run uv run pytest · failed: exit 1: FAILED tests/test_demo.py::test_demo" in transcript_text
        assert '"stderr_tail"' not in transcript_text


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
        assert "   ✗ grep query=needle · failed: not found" in transcript_text
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
        "agent": "subagent_planning_analysis",
        "role": "planning_analysis",
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
                "subagent_planning_analysis",
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
                actor="subagent_planning_analysis",
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
                    metadata={"actor": "subagent_planning_analysis", "duration_ms": 4},
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
                actor="subagent_planning_analysis",
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
                    metadata={"actor": "subagent_planning_analysis", "duration_ms": 8},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )
        app.ui.render_event(
            AgentEvent.tool_call_complete(
                ToolResult(
                    call_id="call-sub",
                    tool_name="subagent_planning_analysis",
                    output=json.dumps(result_payload, indent=2),
                    metadata={"duration_ms": 21000, "title": "Summarize the algorithms directory"},
                )
            ),
            stream_output=True,
            show_tool_calls=True,
        )

        collapsed = app._transcript_text()
        assert "[+] ● Planning Task - Summarize the algorithms directory" in collapsed
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
        assert "   [+] ✓ Result: Output JSON · done · 21.0s" in expanded
        assert '"schema_version"' not in expanded

        app.toggle_subagent_result_json("call-sub")
        expanded = app._transcript_text()
        assert "   [-] ✓ Result: Output JSON · done · 21.0s" in expanded
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
        app.ui.render_event(AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="stop"), stream_output=True, show_tool_calls=True)
        app.ui.render_event(AgentEvent.agent_stop("done"), stream_output=True, show_tool_calls=True)

        transcript_text = app._transcript_text()
        assert "Done" in transcript_text
        assert "2 tools" in transcript_text
        assert "1 edit" in transcript_text
        assert "1 failed" in transcript_text
        assert "1 recovered" in transcript_text


@pytest.mark.asyncio
async def test_textual_turn_footer_waits_for_turn_completed_before_agent_stop(tmp_path):
    app = _new_textual_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.ui.render_event(AgentEvent.agent_start("needs approval"), stream_output=True, show_tool_calls=True)
        app.record_tool_completion(ToolResult(call_id="a", tool_name="read_file", output="read"))
        app.ui.render_event(AgentEvent.agent_stop(None), stream_output=True, show_tool_calls=True)

        assert "Done" not in app._transcript_text()

        app.ui.render_event(AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="stop"), stream_output=True, show_tool_calls=True)
        app.ui.render_event(AgentEvent.agent_stop("done"), stream_output=True, show_tool_calls=True)

        transcript_text = app._transcript_text()
        assert "Done" in transcript_text
        assert "1 tool" in transcript_text


@pytest.mark.asyncio
async def test_textual_turn_footer_accumulates_across_same_user_turn_restarts(tmp_path, monkeypatch):
    from nexus.ui import textual_app

    app = _new_textual_app(tmp_path)
    now = 10.0
    monkeypatch.setattr(textual_app.time, "perf_counter", lambda: now)

    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", TranscriptLog)
        transcript.clear()
        app._transcript_entries.clear()
        app._transcript_plain_parts.clear()

        app.state.current_turn_id = "turn-stats"
        app.ui.render_event(AgentEvent.agent_start("do work"), stream_output=True, show_tool_calls=True)
        app.record_tool_completion(ToolResult(call_id="a", tool_name="read_file", output="read"))
        app.record_tool_completion(ToolResult(call_id="b", tool_name="bash", output="ran"))

        now = 12.0
        app.ui.render_event(AgentEvent.agent_start("continue work"), stream_output=True, show_tool_calls=True)
        app.record_tool_completion(ToolResult(call_id="c", tool_name="edit", output="patched"))

        now = 16.0
        app.ui.render_event(AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="stop"), stream_output=True, show_tool_calls=True)
        app.ui.render_event(AgentEvent.agent_stop("done"), stream_output=True, show_tool_calls=True)

        transcript_text = app._transcript_text()
        assert "Done" in transcript_text
        assert "3 tools" in transcript_text
        assert "1 edit" in transcript_text
        assert "6.0s" in transcript_text


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


@pytest.mark.asyncio
async def test_textual_ask_user_request_renders_options_and_accepts_default(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("textual-ask-user"),
        session_store=EphemeralSessionStore(),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=TerminalUI(color=False),
    )
    app = NexusTextualApp(state, Agent(model_client=FakeModelClient(), tool_registry=registry), build_router())
    request = ConfirmationRequest(
        kind=ConfirmationKind.CLARIFICATION,
        tool_name="ask_user",
        prompt="Where should provider config live?",
        reason="This changes override behavior.",
        call_id="ask12345",
        payload={
            "interaction": "ask_user",
            "answer_type": "choice",
            "options": [
                {"id": "global", "label": "Global config"},
                {"id": "project", "label": "Project config"},
            ],
            "default_option_id": "project",
        },
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.ui.render_event(
            AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
            stream_output=False,
            show_tool_calls=True,
        )
        assert "Nexus needs clarification" in app._transcript_text()
        assert "2. Project config (project) [default]" in app._transcript_text()

        task = asyncio.create_task(app._approval_callback()(request))
        await pilot.pause()
        await pilot.press("enter")

        response = await task
        assert response.selected_option_id == "project"
