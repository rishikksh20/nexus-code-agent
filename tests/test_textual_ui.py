from __future__ import annotations

import pytest

from nexus.config import load_config
from nexus.integrations.fake_model import FakeModelClient
from nexus.memory.store import MemoryStore
from nexus.runtime.agent import Agent
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import EphemeralSessionStore, new_snapshot
from nexus.runtime.slash_commands import build_router
from nexus.tools.base import ToolRegistry
from nexus.ui import TerminalUI
from nexus.ui.textual_app import NexusTextualApp, _strip_mouse_escape_sequences


@pytest.mark.asyncio
async def test_textual_repl_keeps_mouse_enabled_for_scrolling(monkeypatch):
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

    assert state.console is original_console
