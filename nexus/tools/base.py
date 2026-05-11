from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from nexus.models import ToolExecutionContext, ToolResult


class ToolKind(str, Enum):
    """Semantic classification of a tool's capability and risk profile.

    Used by the permission system, UI, and observability layer to apply
    appropriate policies without hard-coding tool names.
    """

    READ = "read"        # Non-mutating reads (files, time, memory lookups)
    WRITE = "write"      # Persistent writes (files, notes)
    SHELL = "shell"      # Arbitrary shell / process execution
    NETWORK = "network"  # Outbound network calls
    MEMORY = "memory"    # Agent memory store operations
    MCP = "mcp"          # Remote MCP server tools
    AGENT = "agent"      # Sub-agent / delegation
    SANDBOX = "sandbox"  # Isolated container execution


class BaseTool(Protocol):
    """Structural protocol every Nexus tool must satisfy.

    Tools are registered in a :class:`ToolRegistry` and invoked by the agent
    during its agentic loop.  They must be stateless across calls — all
    per-call state lives in :class:`~nexus.models.ToolExecutionContext`.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    is_mutating: bool
    kind: ToolKind

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        ...


@dataclass(slots=True, frozen=True)
class ToolRecord:
    name: str
    tool: BaseTool
    source: str = "core"
    origin: str | None = None


def tool_to_schema(tool: BaseTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolRecord] = {}

    def register(self, tool: BaseTool, *, source: str = "core", origin: str | None = None) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = ToolRecord(name=tool.name, tool=tool, source=source, origin=origin)

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name].tool
        except KeyError as exc:
            raise LookupError(f"Unknown tool: {name}") from exc

    def record(self, name: str) -> ToolRecord:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise LookupError(f"Unknown tool: {name}") from exc

    def all(self) -> list[BaseTool]:
        return [record.tool for record in self._tools.values()]

    def records(self) -> list[ToolRecord]:
        return list(self._tools.values())

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return tuple(tool_to_schema(tool) for tool in self.all())

    def clear(self) -> None:
        """Remove all registered tools. Mutates in-place so existing references stay valid."""
        self._tools.clear()
