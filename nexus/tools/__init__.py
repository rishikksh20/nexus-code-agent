"""Builtin tool interfaces and implementations."""

from nexus.tools.base import FileDiff, Tool, ToolConfirmation, ToolKind, ToolRegistry
from nexus.tools.builtin import (
    AskUserTool,
    EditTool,
    GetTimeTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    PythonLspTool,
    MemoryTool,
    ReadFileTool,
    ShellTool,
    TodoTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
)
from nexus.tools.mcp import (
    MCPCallResult,
    MCPClient,
    MCPRefreshReport,
    MCPServerConfig,
    MCPServerRuntime,
    MCPToolAdapter,
    MCPToolSpec,
    mcp_server_example_for_workspace,
    refresh_mcp_server_tools,
    register_discovered_mcp_tools,
)
from nexus.tools.registry import create_tool_registry, get_core_tools, register_core_tools, tool_enabled

__all__ = [
    # Base types
    "FileDiff",
    "Tool",
    "ToolConfirmation",
    "ToolKind",
    "ToolRegistry",
    # Builtin tools
    "AskUserTool",
    "EditTool",
    "GetTimeTool",
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "PythonLspTool",
    "MemoryTool",
    "ReadFileTool",
    "ShellTool",
    "TodoTool",
    "WebFetchTool",
    "WebSearchTool",
    "WriteFileTool",
    # MCP tools
    "MCPCallResult",
    "MCPClient",
    "MCPRefreshReport",
    "MCPServerConfig",
    "MCPServerRuntime",
    "MCPToolAdapter",
    "MCPToolSpec",
    "mcp_server_example_for_workspace",
    "refresh_mcp_server_tools",
    "register_discovered_mcp_tools",
    # Factory
    "create_tool_registry",
    "get_core_tools",
    "register_core_tools",
    "tool_enabled",
]
