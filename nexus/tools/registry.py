"""Registry helpers for the Nexus coding-agent tool package.

This module centralizes first-party tool construction so runtime bootstrap and
interactive tool reload share the same registration flow.
"""
from __future__ import annotations

from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import (
    ApplyPatchTool,
    BashTool,
    CodeIndexTool,
    EditTool,
    FindReferencesTool,
    GitDiffTool,
    GitStatusTool,
    GetTimeTool,
    GlobTool,
    GrepTool,
    InsertEditIntoFileTool,
    LsTool,
    PythonLspTool,
    MemoryTool,
    ReadFileTool,
    RunFormatterTool,
    RunLinterTool,
    RunTestsTool,
    RunTypecheckTool,
    SemanticSearchTool,
    TodoTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)


def tool_enabled(config, tool_name: str) -> bool:
    """Return True when *tool_name* is permitted by the active config."""
    allowed_tools = getattr(config, "allowed_tools", [])
    denied_tools = getattr(config, "denied_tools", [])
    allowed = [allowed_tools] if isinstance(allowed_tools, str) else list(allowed_tools or [])
    if any(str(item).strip().lower() == "all" for item in allowed):
        return tool_name not in denied_tools
    if allowed and tool_name not in allowed:
        return False
    return tool_name not in denied_tools


def get_core_tools(config) -> list:
    """Return pre-constructed first-party coding-agent tool instances."""
    return [
        GetTimeTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditTool(),
        InsertEditIntoFileTool(),
        ApplyPatchTool(),
        GlobTool(),
        GrepTool(),
        LsTool(),
        PythonLspTool(),
        FindReferencesTool(),
        CodeIndexTool(),
        SemanticSearchTool(),
        GitStatusTool(),
        GitDiffTool(),
        RunTestsTool(),
        RunLinterTool(),
        RunTypecheckTool(),
        RunFormatterTool(),
        BashTool(),
        MemoryTool(memory_dir=config.memory_dir),
        TodoTool(),
        WebFetchTool(),
        WebSearchTool(),
    ]


def register_core_tools(registry: ToolRegistry, config) -> ToolRegistry:
    """Register all enabled first-party tools into *registry*."""
    for tool in get_core_tools(config):
        if tool_enabled(config, tool.name):
            registry.register(tool, source="core", origin="builtin")
    return registry


def create_tool_registry(config) -> ToolRegistry:
    """Create a registry pre-populated with enabled first-party tools."""
    return register_core_tools(ToolRegistry(), config)
