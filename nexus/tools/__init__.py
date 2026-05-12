"""Builtin tool interfaces and implementations."""

from nexus.tools.base import FileDiff, Tool, ToolConfirmation, ToolKind, ToolRegistry
from nexus.tools.builtin import (
    BashTool,
    EditTool,
    GetTimeTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    LsTool,
    MemoryTool,
    ReadFileTool,
    ShellTool,
    TodoTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
    WriteNoteTool,
    get_all_builtin_tools,
)
from nexus.tools.registry import create_tool_registry, get_core_tools, register_core_tools, tool_enabled
from nexus.tools.subagents import (
    SubAgentTool,
    SubagentDefinition,
    load_subagent_definitions,
    load_subagent_definitions_from_skills,
    register_agent_tool,
    register_skill_subagent_tools,
    register_subagent_tools,
)

__all__ = [
    # Base types
    "FileDiff",
    "Tool",
    "ToolConfirmation",
    "ToolKind",
    "ToolRegistry",
    # Builtin tools
    "BashTool",
    "EditTool",
    "GetTimeTool",
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "LsTool",
    "MemoryTool",
    "ReadFileTool",
    "ShellTool",
    "TodoTool",
    "WebFetchTool",
    "WebSearchTool",
    "WriteFileTool",
    "WriteNoteTool",
    # Subagent tools
    "SubAgentTool",
    "SubagentDefinition",
    "load_subagent_definitions",
    "load_subagent_definitions_from_skills",
    "register_agent_tool",
    "register_skill_subagent_tools",
    "register_subagent_tools",
    # Factory
    "create_tool_registry",
    "get_all_builtin_tools",
    "get_core_tools",
    "register_core_tools",
    "tool_enabled",
]
