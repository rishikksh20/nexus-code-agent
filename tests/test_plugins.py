from __future__ import annotations

from textwrap import dedent

from rich.console import Console

from nexus.config import load_config
from nexus.extensions.plugins import PluginLoader
from nexus.memory.store import MemoryStore
from nexus.runtime.execution import ExecutionMode
from nexus.hooks import HookExecutor
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import SessionStore, new_snapshot
from nexus.runtime.slash_commands import build_router
from nexus.tools.base import ToolRegistry


def test_plugin_loader_registers_tools_with_plugin_source(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "demo_plugin.py").write_text(
        dedent(
            """
            class DemoTool:
                name = "demo_plugin_tool"
                description = "Tool from plugin"
                input_schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
                is_mutating = False

                async def execute(self, call_id, arguments, context):
                    raise NotImplementedError

            def register(registry, hooks):
                del hooks
                registry.register(DemoTool())
            """
        ),
        encoding="utf-8",
    )
    registry = ToolRegistry()

    loaded = PluginLoader(plugin_dir).load_all(registry, HookExecutor())

    assert loaded == ["demo_plugin"]
    assert registry.record("demo_plugin_tool").source == "plugin"
    assert registry.record("demo_plugin_tool").origin == "demo_plugin"


def test_tools_slash_command_shows_tool_source(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    plugin_dir = config.plugins_dir
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "demo_plugin.py").write_text(
        dedent(
            """
            class DemoTool:
                name = "demo_plugin_tool"
                description = "Tool from plugin"
                input_schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
                is_mutating = False

                async def execute(self, call_id, arguments, context):
                    raise NotImplementedError

            def register(registry, hooks):
                del hooks
                registry.register(DemoTool())
            """
        ),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    PluginLoader(plugin_dir).load_all(registry, HookExecutor())
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("plugin"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    import asyncio

    asyncio.run(router.dispatch(state, "/tools"))

    output = console.export_text()
    assert "demo_plugin_tool" in output
    assert "plugin" in output


def test_plugin_loader_respects_allow_policy(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "demo_plugin.py").write_text(
        dedent(
            """
            class DemoTool:
                name = "demo_plugin_tool"
                description = "Tool from plugin"
                input_schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
                is_mutating = False

                async def execute(self, call_id, arguments, context):
                    raise NotImplementedError

            def register(registry, hooks):
                del hooks
                registry.register(DemoTool())
            """
        ),
        encoding="utf-8",
    )
    registry = ToolRegistry()

    loaded = PluginLoader(plugin_dir).load_all(
        registry,
        HookExecutor(),
        can_register=lambda tool: tool.name == "allowed_tool",
    )

    assert loaded == ["demo_plugin"]
    try:
        registry.record("demo_plugin_tool")
    except LookupError:
        pass
    else:
        raise AssertionError("Plugin tool should have been filtered out by policy")