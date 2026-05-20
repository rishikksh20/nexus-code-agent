from __future__ import annotations

import json
import os

import pytest
from rich.console import Console

from nexus.cli.init import init_workspace
from nexus.config import load_config
from nexus.tools.mcp import MCPServerConfig, MCPServerRuntime, MCPToolSpec
from nexus.memory.store import MemoryStore
from nexus.models import Message, ToolCall
from nexus.runtime.context_state import (
    TaskContext,
    append_artifact_record,
    append_context_packet,
    make_artifact_record,
    make_context_packet,
    upsert_task_context,
)
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.agent_scope import subagent_skill_names, subagent_tool_names, supervisor_skill_names, supervisor_tool_names
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import SessionStore, new_snapshot
from nexus.runtime.slash_commands import build_router
from nexus.sandbox.agent_tool import SubAgentTool, SubagentDefinition
from nexus.skills import get_skill_roots, load_skill_registry
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, ReadFileTool


@pytest.mark.asyncio
async def test_mode_slash_command_switches_state(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True),
    )

    router = build_router()
    handled = await router.dispatch(state, "/mode plan")

    assert handled is True
    assert state.mode is ExecutionMode.PLAN


@pytest.mark.asyncio
async def test_slash_command_invalid_quoting_does_not_crash(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    console = Console(record=True, no_color=True)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-invalid"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    handled = await router.dispatch(state, '/memory save note "unterminated')

    assert handled is True
    assert "Invalid command syntax" in console.export_text()


class _FakeMCPClient:
    def __init__(self, server: MCPServerConfig | None = None) -> None:
        self.server = server or MCPServerConfig(
            name="filesystem",
            command=("uvx", "mcp-server-filesystem", "."),
            prefix="fs_",
        )
        self._tools = [
            MCPToolSpec(
                name="read_file",
                description="Read files.",
                input_schema={"type": "object", "properties": {}, "required": []},
            )
        ]

    async def list_tools(self) -> list[MCPToolSpec]:
        return list(self._tools)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_mcp_help_shows_without_configured_servers(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-help"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/mcp help")

    assert handled is True
    output = console.export_text()
    assert "/mcp" in output
    assert "reload" in output


@pytest.mark.asyncio
async def test_mcp_default_shows_usage_without_loaded_servers(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-empty"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/mcp")

    assert handled is True
    output = console.export_text()
    assert "No MCP servers loaded" in output
    assert "/mcp reload" in output


@pytest.mark.asyncio
async def test_mcp_status_slash_command_shows_server_state(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    runtime = MCPServerRuntime(
        server=MCPServerConfig(name="filesystem", command=("uvx", "mcp-server-filesystem", "."), prefix="fs_"),
        client=_FakeMCPClient(),
        connected=True,
        registered_tools=("fs_read_file",),
        discovered_tools=("fs_read_file",),
        discovered_specs=(
            MCPToolSpec(
                name="read_file",
                description="Read files.",
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
        ),
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        mcp_servers=[runtime],
    )

    router = build_router()
    handled = await router.dispatch(state, "/mcp status")

    assert handled is True
    output = console.export_text()
    assert "filesystem" in output
    assert "connected" in output
    assert "1" in output


@pytest.mark.asyncio
async def test_multi_agent_slash_command_is_not_registered(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    session = new_snapshot("slash-multi-agent")
    session.metadata["multi_agent"] = {"mode": "advanced", "complexity": "large"}
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=session,
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    status_handled = await router.dispatch(state, "/multi-agent status")

    assert status_handled is False
    output = console.export_text()
    assert "Multi-Agent Supervisor" not in output


@pytest.mark.asyncio
async def test_agent_attach_tool_persists_and_updates_supervisor_scope(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_workspace(workspace, global_root=tmp_path / "global", project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    local_config.write_text(
        'agent_mode = "advanced"\n'
        'allowed_tools = ["get_time", "read_file", "subagent_execution"]\n',
        encoding="utf-8",
    )
    config = load_config(workspace, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    registry.register(ReadFileTool(), source="core", origin="builtin")
    registry.register(
        SubAgentTool(
            SubagentDefinition(
                name="execution",
                description="Execute focused work.",
                goal_prompt="Do the task.",
                allowed_tools=["get_time"],
            ),
            base_tool_registry=registry,
            config=config,
        ),
        source="agent",
        origin="execution",
    )
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("agent-scope"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/agent attach tool read_file")

    assert handled is True
    assert state.config.agent_attached_tools == ["read_file"]
    assert "read_file" in supervisor_tool_names(state.config, state.tool_registry)
    assert "get_time" not in supervisor_tool_names(state.config, state.tool_registry)
    content = local_config.read_text(encoding="utf-8")
    assert "[agents]" in content
    assert 'attached_tools = ["read_file"]' in content
    assert "Attached tool for supervisor" in console.export_text()


@pytest.mark.asyncio
async def test_subagent_commands_show_and_persist_resource_scope(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.agent_mode = "advanced"
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    registry.register(ReadFileTool(), source="core", origin="builtin")
    registry.register(
        SubAgentTool(
            SubagentDefinition(
                name="execution",
                description="Execute focused work.",
                goal_prompt="Do the task.",
                allowed_tools=["get_time"],
            ),
            base_tool_registry=registry,
            config=config,
        ),
        source="agent",
        origin="execution",
    )
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("subagent-scope"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    assert await router.dispatch(state, "/sub-agent list") is True
    assert await router.dispatch(state, "/sub-agent attach execution tool read_file") is True
    assert await router.dispatch(state, "/sub-agent tools execution") is True

    assert state.config.subagent_profiles[0]["name"] == "execution"
    assert state.config.subagent_profiles[0]["attached_tools"] == ["read_file"]
    assert "[[sub-agents]]" in config.local_config_file.read_text(encoding="utf-8")
    output = console.export_text()
    assert "Sub-Agents" in output
    assert "Attached tool for sub-agent execution" in output
    assert "read_file" in output


@pytest.mark.asyncio
async def test_agent_allowed_config_restricts_tools_and_skills(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.agent_mode = "advanced"
    config.agent_allowed_tools = ["read_file"]
    config.agent_allowed_skills = ["review"]
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    registry.register(ReadFileTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("agent-allowed-config"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        active_skills=["review", "notes"],
    )

    await build_router().dispatch(state, "/agent status")

    assert supervisor_tool_names(config, registry) == {"read_file"}
    assert supervisor_skill_names(config, state.active_skills) == ["review"]
    assert "Configured allowed tools" in console.export_text()


@pytest.mark.asyncio
async def test_agent_all_scope_uses_workspace_resources(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.agent_mode = "advanced"
    config.agent_allowed_tools = ["all"]
    config.agent_allowed_mcp_servers = ["all"]
    config.agent_allowed_skills = ["all"]
    registry = ToolRegistry()
    registry.register(ReadFileTool(), source="core", origin="builtin")
    registry.register(GetTimeTool(), source="mcp", origin="filesystem")

    assert supervisor_tool_names(config, registry) == {"read_file", "get_time"}
    assert supervisor_skill_names(config, ["review", "notes"]) == ["review", "notes"]


@pytest.mark.asyncio
async def test_subagent_profile_allowed_config_overrides_default_tools(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.agent_mode = "advanced"
    config.subagent_profiles = [
        {
            "name": "execution",
            "allowed_tools": ["read_file"],
            "attached_tools": [],
            "detached_tools": [],
            "allowed_skills": [],
            "attached_skills": [],
            "detached_skills": [],
            "allowed_mcp_servers": [],
            "attached_mcp_servers": [],
            "detached_mcp_servers": [],
        }
    ]
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    registry.register(ReadFileTool(), source="core", origin="builtin")
    definition = SubagentDefinition(
        name="execution",
        description="Execute focused work.",
        goal_prompt="Do the task.",
        allowed_tools=["get_time", "read_file"],
    )
    registry.register(
        SubAgentTool(definition, base_tool_registry=registry, config=config),
        source="agent",
        origin="execution",
    )
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("subagent-allowed-config"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    await build_router().dispatch(state, "/sub-agent tools execution")

    assert subagent_tool_names(
        config,
        registry,
        "execution",
        base_allowed_tools=definition.allowed_tools,
    ) == {"read_file"}
    output = console.export_text()
    assert "allowed" in output


@pytest.mark.asyncio
async def test_subagent_all_scope_uses_workspace_resources(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.agent_mode = "advanced"
    config.subagent_profiles = [
        {
            "name": "execution",
            "allowed_tools": ["all"],
            "allowed_skills": ["all"],
            "allowed_mcp_servers": ["all"],
        }
    ]
    registry = ToolRegistry()
    registry.register(ReadFileTool(), source="core", origin="builtin")
    registry.register(GetTimeTool(), source="mcp", origin="filesystem")

    assert subagent_tool_names(
        config,
        registry,
        "execution",
        base_allowed_tools=["read_file"],
    ) == {"read_file", "get_time"}
    assert subagent_skill_names(config, "execution", ["review", "notes"]) == ["review", "notes"]


@pytest.mark.asyncio
async def test_mcp_refresh_slash_command_updates_discovered_tools(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    runtime = MCPServerRuntime(
        server=MCPServerConfig(name="filesystem", command=("uvx", "mcp-server-filesystem", "."), prefix="fs_"),
        client=_FakeMCPClient(),
        connected=True,
        registered_tools=("fs_read_file",),
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-refresh"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        mcp_servers=[runtime],
    )

    router = build_router()
    handled = await router.dispatch(state, "/mcp refresh filesystem")

    assert handled is True
    assert runtime.discovered_tools == ("fs_read_file",)
    output = console.export_text()
    assert "MCP Refresh" in output


@pytest.mark.asyncio
async def test_mcp_tools_slash_command_shows_tool_details(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    console = Console(record=True, no_color=True, width=200)
    runtime = MCPServerRuntime(
        server=MCPServerConfig(name="filesystem", command=("uvx", "mcp-server-filesystem", "."), prefix="fs_"),
        client=_FakeMCPClient(),
        connected=True,
        registered_tools=("fs_read_file",),
        discovered_tools=("fs_read_file",),
        discovered_specs=(
            MCPToolSpec(
                name="read_file",
                description="Read files.",
                input_schema={"type": "object", "properties": {}, "required": []},
            ),
        ),
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-tools"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        mcp_servers=[runtime],
    )

    handled = await build_router().dispatch(state, "/mcp tools")

    assert handled is True
    output = console.export_text()
    assert "fs_read_file" in output
    assert "read_file" in output
    assert "enabled" in output


@pytest.mark.asyncio
async def test_mcp_available_slash_command_shows_global_catalog(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_workspace(workspace, global_root=tmp_path / "global", project_name="workspace")
    (tmp_path / "global" / "config.toml").write_text(
        'mcp_servers = [{ name = "filesystem", transport = "stdio", command = ["mcp-server-filesystem", "."], prefix = "fs_" }]\n',
        encoding="utf-8",
    )
    config = load_config(workspace, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-available"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/mcp available")

    assert handled is True
    output = console.export_text()
    assert "filesystem" in output
    assert "global" in output
    assert "available" in output


@pytest.mark.asyncio
async def test_mcp_activate_slash_command_enables_global_server_locally(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_workspace(workspace, global_root=tmp_path / "global", project_name="workspace")
    (tmp_path / "global" / "config.toml").write_text(
        'mcp_servers = [{ name = "broken", transport = "stdio", command = ["definitely-missing-mcp-server"], prefix = "mcp_broken_" }]\n',
        encoding="utf-8",
    )
    config = load_config(workspace, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-activate"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/mcp activate broken")

    assert handled is True
    assert "broken" in state.config.enabled_mcp_servers
    assert state.config.mcp_servers[0]["name"] == "broken"
    assert len(state.mcp_servers) == 1
    output = console.export_text()
    assert "Activated MCP server" in output


@pytest.mark.asyncio
async def test_mcp_activate_refreshes_cached_system_prompt(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_workspace(workspace, global_root=tmp_path / "global", project_name="workspace")
    (tmp_path / "global" / "config.toml").write_text(
        'mcp_servers = [{ name = "filesystem", transport = "stdio", command = ["fake-mcp"], prefix = "fs_" }]\n',
        encoding="utf-8",
    )
    config = load_config(workspace, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-activate-prompt"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )
    state.build_system_prompt("inspect files")
    assert "MCP Tool Contract" not in state.current_system_prompt

    async def fake_refresh(self):
        self.client = _FakeMCPClient(self.server)
        self.connected = True
        self.last_error = None
        self.discovered_specs = tuple(await self.client.list_tools())
        self.discovered_tools = tuple(self.display_name(spec.name) for spec in self.discovered_specs)
        return self.discovered_tools

    monkeypatch.setattr(MCPServerRuntime, "refresh", fake_refresh)

    handled = await build_router().dispatch(state, "/mcp activate filesystem")

    assert handled is True
    assert "MCP Tool Contract" in state.current_system_prompt
    assert "`fs_read_file` from `filesystem` remote `read_file`" in state.current_system_prompt


@pytest.mark.asyncio
async def test_mcp_deactivate_refreshes_cached_system_prompt(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_workspace(workspace, global_root=tmp_path / "global", project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'mcp_servers = [{ name = "filesystem", transport = "stdio", command = ["fake-mcp"], prefix = "fs_" }]\n',
        encoding="utf-8",
    )
    config = load_config(workspace, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-deactivate-prompt"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    async def fake_refresh(self):
        self.client = _FakeMCPClient(self.server)
        self.connected = True
        self.last_error = None
        self.discovered_specs = tuple(await self.client.list_tools())
        self.discovered_tools = tuple(self.display_name(spec.name) for spec in self.discovered_specs)
        return self.discovered_tools

    monkeypatch.setattr(MCPServerRuntime, "refresh", fake_refresh)
    await build_router().dispatch(state, "/mcp reload")
    assert "MCP Tool Contract" in state.current_system_prompt

    handled = await build_router().dispatch(state, "/mcp deactivate filesystem")

    assert handled is True
    assert "MCP Tool Contract" not in state.current_system_prompt
    assert "fs_read_file" not in state.current_system_prompt


@pytest.mark.asyncio
async def test_mcp_refresh_unknown_server_reports_not_found(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-refresh-missing"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        mcp_servers=[
            MCPServerRuntime(
                server=MCPServerConfig(name="filesystem", command=("uvx", "mcp-server-filesystem", ".")),
            )
        ],
    )

    handled = await build_router().dispatch(state, "/mcp refresh missing")

    assert handled is True
    assert "MCP server not found: missing" in console.export_text()


@pytest.mark.asyncio
async def test_mcp_reload_loads_configured_servers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_workspace(workspace, global_root=tmp_path / "global", project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'mcp_servers = [{ name = "broken", transport = "stdio", command = ["definitely-missing-mcp-server"], prefix = "mcp_broken_" }]\n',
        encoding="utf-8",
    )
    config = load_config(workspace, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("slash-mcp-reload"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/mcp reload")

    assert handled is True
    assert len(state.mcp_servers) == 1
    assert state.mcp_servers[0].server.name == "broken"
    output = console.export_text()
    assert "MCP Reload" in output
    assert "broken" in output


@pytest.mark.asyncio
async def test_skills_slash_command_activates_skill(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    skill_root = config.skills_dir / "review"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review skill\n---\n\n# Review skill\n\nAlways review carefully.",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("skills-slash"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        skill_registry=load_skill_registry(config.skills_dir, config.local_root / "skills"),
    )

    router = build_router()
    handled = await router.dispatch(state, "/skills add review")

    assert handled is True
    assert state.active_skills == ["review"]
    assert state.config.enabled_skills == ["review"]


@pytest.mark.asyncio
async def test_skills_deactivate_refreshes_prompt_and_config(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    skill_root = config.skills_dir / "review"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review skill\n---\n\nAlways review carefully.",
        encoding="utf-8",
    )
    config.enabled_skills = ["review"]
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("skills-deactivate"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        skill_registry=load_skill_registry(config.skills_dir),
        active_skills=["review"],
    )
    state.build_system_prompt("review this")
    assert "name=review" in state.current_system_prompt
    assert "active=yes" in state.current_system_prompt
    assert "Always review carefully." not in state.current_system_prompt

    handled = await build_router().dispatch(state, "/skills deactivate review")

    assert handled is True
    assert state.active_skills == []
    assert state.config.disabled_skills == ["review"]
    assert "active=no" in state.current_system_prompt
    assert "Always review carefully." not in state.current_system_prompt


@pytest.mark.asyncio
async def test_skills_deactivate_removes_run_only_skill(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    skill_root = config.skills_dir / "review"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review skill\n---\n\nAlways review carefully.",
        encoding="utf-8",
    )
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("skills-run-only-deactivate"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True, width=200),
        skill_registry=load_skill_registry(config.skills_dir),
        active_skills=["review"],
        run_skills=["review"],
    )

    handled = await build_router().dispatch(state, "/skills deactivate review")

    assert handled is True
    assert state.active_skills == []
    assert state.run_skills == []


@pytest.mark.asyncio
async def test_skills_create_and_remove_local_skill(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("skills-local"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        skill_registry=load_skill_registry(*get_skill_roots(config)),
    )
    router = build_router()

    assert await router.dispatch(state, "/skills create-local code-review") is True
    skill_file = config.local_root / "skills" / "code-review" / "SKILL.md"
    assert skill_file.exists()
    assert state.skill_registry.get("code-review") is not None

    assert await router.dispatch(state, "/skills remove-local code-review") is True
    assert not skill_file.exists()
    assert state.skill_registry.get("code-review") is None


@pytest.mark.asyncio
async def test_skills_remove_local_refuses_builtin(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("skills-refuse-builtin"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        skill_registry=load_skill_registry(*get_skill_roots(config)),
    )

    handled = await build_router().dispatch(state, "/skills remove-local nexus-agent")

    assert handled is True
    assert "Refusing to remove non-local skill" in console.export_text()


@pytest.mark.asyncio
async def test_skills_help_mentions_subagent_skill_convention(tmp_path):
    state, console = _make_state(tmp_path)
    router = build_router()

    handled = await router.dispatch(state, "/skills help")

    assert handled is True
    output = console.export_text()
    assert "subagent-*" in output


@pytest.mark.asyncio
async def test_skills_list_shows_subagent_type(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    skill_root = config.local_root / "skills" / "subagent-review"
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: subagent-review\ndescription: Review skill\n---\n\n# Review skill\n\nAlways review carefully.",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("skills-list"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        skill_registry=load_skill_registry(config.skills_dir, config.local_root / "skills"),
    )

    router = build_router()
    handled = await router.dispatch(state, "/skills")

    assert handled is True
    output = console.export_text()
    assert "Type" in output
    assert "subagent-review" in output
    assert "subagent" in output


@pytest.mark.asyncio
async def test_provider_slash_command_shows_current_status(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("provider-status"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    handled = await router.dispatch(state, "/provider")

    assert handled is True
    output = console.export_text()
    assert "provider" in output
    assert "mistral" in output
    assert "model_name" in output
    assert "temperature" in output


@pytest.mark.asyncio
async def test_provider_list_slash_command_shows_all_providers(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("provider-list"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    handled = await router.dispatch(state, "/provider list")

    assert handled is True
    output = console.export_text()
    assert "fake" in output
    assert "openai" in output
    assert "openai-compatible" in output
    assert "mistral" in output
    # The active provider (mistral by default) should be marked yes
    assert "yes" in output


@pytest.mark.asyncio
async def test_provider_set_slash_command_updates_model_name(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    # Ensure the local config file directory exists so the TOML write succeeds
    config.local_root.mkdir(parents=True, exist_ok=True)
    config.local_config_file.parent.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("provider-set-model"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    handled = await router.dispatch(state, "/provider set model_name gpt-4o")

    assert handled is True
    assert state.config.model_name == "gpt-4o"
    output = console.export_text()
    assert "model_name" in output
    assert "gpt-4o" in output


@pytest.mark.asyncio
async def test_provider_set_slash_command_rejects_restricted_key(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("provider-set-restricted"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    # sandbox_image is not in PROVIDER_SETTABLE_PARAMS
    handled = await router.dispatch(state, "/provider set sandbox_image evil-image:latest")

    assert handled is True
    output = console.export_text()
    assert "restricted" in output or "Unknown" in output
    # Config must not have changed
    assert state.config.sandbox_image == config.sandbox_image


@pytest.mark.asyncio
async def test_config_upgrade_reloads_config_and_workspace_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("AGENT_MODEL_NAME", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    config = load_config(workspace, global_root=global_root)
    assert config.model_name != "env-after-upgrade"

    (workspace / ".env").write_text("MODEL=env-after-upgrade\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("config-upgrade-reload"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/config upgrade local")

    assert handled is True
    assert state.config.model_name == "env-after-upgrade"
    assert "reloaded" in console.export_text()
    os.environ.pop("MODEL", None)


@pytest.mark.asyncio
async def test_config_upgrade_updates_allowed_tools_and_live_registry(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    local_config.write_text(
        'project_name = "workspace"\n'
        'config_version = 2\n'
        'agent_mode = "advanced"\n'
        'allowed_tools = ["get_time"]\n',
        encoding="utf-8",
    )
    config = load_config(workspace, global_root=global_root)
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("config-upgrade-tools"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/config upgrade local")

    assert handled is True
    content = local_config.read_text(encoding="utf-8")
    assert '"write_file"' in content
    assert "write_file" in state.config.allowed_tools
    assert state.tool_registry.record("write_file").source == "core"
    assert state.tool_registry.record("subagent_planning_analysis").source == "agent"
    assert "allowed_tools: write_file" in console.export_text()


@pytest.mark.asyncio
async def test_config_upgrade_removes_deprecated_multi_agent_mode(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    local_config.write_text(
        'project_name = "workspace"\nmulti_agent_mode = "always"\n',
        encoding="utf-8",
    )
    config = load_config(workspace, global_root=global_root)
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("config-upgrade-legacy"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    handled = await build_router().dispatch(state, "/config upgrade local")

    assert handled is True
    content = local_config.read_text(encoding="utf-8")
    assert "multi_agent_mode" not in content
    assert "config_version = 2" in content
    assert "removed deprecated multi_agent_mode" in console.export_text()


@pytest.mark.asyncio
async def test_session_export_slash_command_writes_json(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("session-export"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        history=[__import__("nexus.models", fromlist=["Message"]).Message(role="user", content="hello")],
    )

    export_path = tmp_path / "session.json"
    router = build_router()
    handled = await router.dispatch(state, f"/session export {export_path}")

    assert handled is True
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_session_export_slash_command_preserves_tool_call_metadata(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("session-export-tool-meta"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        history=[
            Message(
                role="assistant",
                content="Checking time.",
                tool_calls=(ToolCall(call_id="call-1", tool_name="get_time", arguments={}),),
            ),
            Message(
                role="tool",
                content="2026-05-12T00:00:00Z",
                name="get_time",
                tool_call_id="call-1",
            ),
        ],
    )

    export_path = tmp_path / "session-with-tools.json"
    router = build_router()
    handled = await router.dispatch(state, f"/session export {export_path}")

    assert handled is True
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload[0]["tool_calls"][0]["call_id"] == "call-1"
    assert payload[1]["tool_call_id"] == "call-1"


# ── /context usage ────────────────────────────────────────────────────────────

def _make_state(tmp_path, *, extra_history=None):
    from nexus.models import Message
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("ctx-test"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        history=extra_history or [],
    )
    return state, console


@pytest.mark.asyncio
async def test_skills_reload_registers_skill_backed_subagent_tools(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.agent_mode = "advanced"
    skill_dir = config.local_root / "skills" / "subagent-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: subagent-review\n"
        "description: Review code changes.\n"
        "---\n\n"
        "# Review Skill\n\nInspect the selected code and report issues.\n",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("skills-reload"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        skill_registry=load_skill_registry(*get_skill_roots(config)),
    )

    router = build_router()
    handled = await router.dispatch(state, "/skills reload")

    assert handled is True
    assert state.tool_registry.record("subagent_review").source == "agent-skill"


@pytest.mark.asyncio
async def test_sub_agent_agents_reload_registers_and_updates_yaml_tools(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = load_config(
        workspace,
        global_root=tmp_path / "global",
        cli_overrides={"agent_mode": "advanced"},
    )
    agents_dir = workspace / ".nexus" / "agents"
    agents_dir.mkdir(parents=True)
    yaml_path = agents_dir / "summarizer.yml"
    yaml_path.write_text(
        "name: summarizer\n"
        "description: First summary agent.\n"
        "goal_prompt: Summarize briefly.\n",
        encoding="utf-8",
    )
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    console = Console(record=True, no_color=True, width=200)
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("yaml-agent-reload"),
        session_store=SessionStore(config.session_dir),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
    )

    router = build_router()
    handled = await router.dispatch(state, "/sub-agent agents reload")

    assert handled is True
    assert state.tool_registry.record("subagent_summarizer").source == "agent-yaml"
    assert state.tool_registry.record("subagent_summarizer").tool.description == "First summary agent."

    yaml_path.write_text(
        "name: summarizer\n"
        "description: Updated summary agent.\n"
        "goal_prompt: Summarize carefully.\n"
        "max_turns: 9\n",
        encoding="utf-8",
    )
    handled = await router.dispatch(state, "/sub-agent agents reload")

    assert handled is True
    record = state.tool_registry.record("subagent_summarizer")
    assert record.source == "agent-yaml"
    assert record.tool.description == "Updated summary agent."
    assert record.tool._definition.max_turns == 9


@pytest.mark.asyncio
async def test_context_usage_command_shows_table(tmp_path):
    state, console = _make_state(tmp_path)
    router = build_router()
    handled = await router.dispatch(state, "/context usage")

    assert handled is True
    output = console.export_text()
    assert "Context Usage" in output
    assert "Context window" in output
    assert "Tool schemas" in output
    assert "Sub-agent schemas" in output
    assert "MCP schemas" in output
    assert "Active skills prompt" in output
    assert "tokens" in output


@pytest.mark.asyncio
async def test_context_usage_includes_provider_and_model(tmp_path):
    state, console = _make_state(tmp_path)
    router = build_router()
    await router.dispatch(state, "/context usage")

    output = console.export_text()
    assert state.config.provider in output
    assert state.config.model_name in output


@pytest.mark.asyncio
async def test_context_show_is_default_subcommand(tmp_path):
    """'/context' without args should show the system prompt, not the usage table."""
    state, console = _make_state(tmp_path)
    state.current_system_prompt = "You are Nexus."
    router = build_router()
    await router.dispatch(state, "/context")

    output = console.export_text()
    assert "You are Nexus." in output
    assert "Context window" not in output


@pytest.mark.asyncio
async def test_context_agents_and_agent_usage_show_multi_agent_records(tmp_path):
    state, console = _make_state(tmp_path)
    state.session.metadata["multi_agent_context"] = {
        "agents": {
            "supervisor": {
                "agent_id": "supervisor",
                "role": "supervisor",
                "scope": "shared",
                "summary": "Supervising cognitive sub-agent context.",
                "token_estimate": 42,
                "message_count": 3,
                "tool_call_count": 0,
                "allowed_tools": [],
            }
        },
        "packets": [],
    }
    router = build_router()

    await router.dispatch(state, "/context agents")
    await router.dispatch(state, "/context agent supervisor")
    await router.dispatch(state, "/context usage supervisor")

    output = console.export_text()
    assert "Agent Contexts" in output
    assert "Supervising cognitive sub-agent context" in output
    assert "Context Usage: supervisor" in output
    assert "42" in output


@pytest.mark.asyncio
async def test_multi_agent_typed_state_is_not_exposed_by_slash_command(tmp_path):
    state, console = _make_state(tmp_path)
    upsert_task_context(
        state.session.metadata,
        TaskContext(
            task_id="verify",
            role="test",
            objective="Run focused checks.",
            status="failed",
        ),
    )
    packet = make_context_packet(
        metadata=state.session.metadata,
        source_agent="test",
        target_agent="execution",
        packet_type="test_failure",
        task_id="verify",
        summary="Verification follow-up needed.",
        failure_summary="Typecheck failed.",
    )
    append_context_packet(state.session.metadata, packet)
    artifact = make_artifact_record(
        metadata=state.session.metadata,
        artifact_type="typecheck_output",
        task_id="verify",
        producer_agent="test",
        summary="Typecheck failed.",
        content="full typecheck output",
    )
    append_artifact_record(state.session.metadata, artifact)
    router = build_router()

    handled = await router.dispatch(state, "/multi-agent tasks")
    await router.dispatch(state, "/context task verify")
    await router.dispatch(state, "/context summary")

    assert handled is False
    output = console.export_text()
    assert "Multi-Agent Tasks" not in output
    assert "Verification follow-up needed" in output


# ── /help subcommand ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "/context help",
    "/mode help",
    "/provider help",
    "/skills help",
    "/config help",
    "/session help",
    "/memory help",
    "/tools help",
    "/history help",
])
@pytest.mark.asyncio
async def test_help_subcommand_shows_table(tmp_path, command):
    state, console = _make_state(tmp_path)
    router = build_router()
    handled = await router.dispatch(state, command)

    assert handled is True
    output = console.export_text()
    # Every help table must include the literal word "help" and show at least one example
    assert "help" in output.lower()
    assert "/" in output  # at least one example contains a slash command


@pytest.mark.asyncio
async def test_context_help_lists_all_subcommands(tmp_path):
    state, console = _make_state(tmp_path)
    router = build_router()
    await router.dispatch(state, "/context help")

    output = console.export_text()
    assert "show" in output
    assert "usage" in output
    assert "help" in output


@pytest.mark.asyncio
async def test_mode_help_lists_all_modes(tmp_path):
    state, console = _make_state(tmp_path)
    router = build_router()
    await router.dispatch(state, "/mode help")

    output = console.export_text()
    for mode in ("plan", "default", "auto"):
        assert mode in output


# ── Unknown slash command forwarding ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_slash_command_returns_false(tmp_path):
    """An unrecognised /command should return False so the REPL forwards it to the agent."""
    state, _ = _make_state(tmp_path)
    router = build_router()
    handled = await router.dispatch(state, "/notacommand do something")

    assert handled is False


# ── Model context limits ──────────────────────────────────────────────────────

def test_get_model_context_limit_exact_match():
    from nexus.config.model_limits import get_model_context_limit
    assert get_model_context_limit("mistral-medium-latest") == 32_768


def test_get_model_context_limit_prefix_match():
    from nexus.config.model_limits import get_model_context_limit
    # "mistral-large-2407" is in the table but "mistral-large-9999" is not;
    # it should prefix-match "mistral-large" (131_072 tokens).
    assert get_model_context_limit("mistral-large-9999") == 131_072


def test_get_model_context_limit_unknown_model_uses_default():
    from nexus.config.model_limits import get_model_context_limit, _DEFAULT_CONTEXT_LIMIT
    assert get_model_context_limit("some-unknown-model-xyz") == _DEFAULT_CONTEXT_LIMIT


def test_apply_model_context_limits_overrides_defaults(tmp_path):
    from nexus.app import _apply_model_context_limits
    config = load_config(tmp_path, global_root=tmp_path / "global",
                         cli_overrides={"model_name": "mistral-large-latest"})
    # Defaults are 10_000 / 14_000
    assert config.compaction_soft_limit == 10_000
    assert config.compaction_hard_limit == 14_000

    _apply_model_context_limits(config)

    # mistral-large-latest context = 131_072 → 65% / 85%
    assert config.compaction_soft_limit == int(131_072 * 0.65)
    assert config.compaction_hard_limit == int(131_072 * 0.85)


def test_apply_model_context_limits_respects_user_overrides(tmp_path):
    from nexus.app import _apply_model_context_limits
    config = load_config(tmp_path, global_root=tmp_path / "global",
                         cli_overrides={"compaction_soft_limit": 5000,
                                        "compaction_hard_limit": 8000,
                                        "model_name": "mistral-large-latest"})
    _apply_model_context_limits(config)
    # User explicitly set both; function should not override either
    assert config.compaction_soft_limit == 5000
    assert config.compaction_hard_limit == 8000
