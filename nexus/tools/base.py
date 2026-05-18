from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from nexus.models import ToolExecutionContext, ToolResult


class ToolKind(str, Enum):
    """Semantic classification of a tool's capability and risk profile.

    Used by the permission system, UI, and observability layer to apply
    appropriate policies without hard-coding tool names.
    """

    READ = "read"        # Non-mutating reads (files, time, memory lookups)
    WRITE = "write"      # Persistent writes
    SHELL = "shell"      # Arbitrary shell / process execution
    NETWORK = "network"  # Outbound network calls
    MEMORY = "memory"    # Agent memory store operations
    MCP = "mcp"          # Remote MCP server tools
    AGENT = "agent"      # Cognitive sub-agent tools
    SANDBOX = "sandbox"  # Isolated container execution


@dataclass
class FileDiff:
    """Captures the before/after state of a file edit for confirmation prompts."""

    path: Path
    old_content: str
    new_content: str
    is_new_file: bool = False
    is_deletion: bool = False

    def to_diff(self) -> str:
        import difflib

        old_lines = self.old_content.splitlines(keepends=True)
        new_lines = self.new_content.splitlines(keepends=True)
        if old_lines and not old_lines[-1].endswith("\n"):
            old_lines[-1] += "\n"
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        old_name = "/dev/null" if self.is_new_file else str(self.path)
        new_name = "/dev/null" if self.is_deletion else str(self.path)
        return "".join(
            difflib.unified_diff(old_lines, new_lines, fromfile=old_name, tofile=new_name)
        )


@dataclass
class ToolConfirmation:
    """Rich confirmation payload shown to the user before a mutating tool runs."""

    tool_name: str
    params: dict[str, Any]
    description: str | None = None
    diff: FileDiff | None = None
    affected_paths: list[Path] = field(default_factory=list)
    is_dangerous: bool = False
    command: str | None = None


class Tool(abc.ABC):
    """Abstract base class for all Nexus tools.

    Subclass this to build first-party and plugin tools.  The registry accepts
    any object that satisfies :class:`BaseTool` (structural Protocol), but
    inheriting from :class:`Tool` gives you ``get_confirmation``,
    ``validate_params``, and ``to_openai_schema`` for free.

    Class attributes
    ----------------
    name:
        Machine-readable identifier used in tool-call payloads.
    description:
        Human-readable summary surfaced in the system prompt.
    kind:
        Semantic classification used by the permission system.
    input_schema:
        JSON Schema dict describing accepted parameters.
    is_mutating:
        Whether this tool modifies external state.
    """

    name: str
    description: str
    kind: ToolKind = ToolKind.READ
    input_schema: dict[str, Any] = field(default_factory=dict)
    is_mutating: bool = False

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        """Return a :class:`ToolConfirmation` if this invocation needs approval.

        The default implementation returns ``None`` (auto-approved).  Override
        for tools that show diffs or need explicit user consent.
        """
        return None

    def validate_params(self, arguments: dict[str, Any]) -> list[str]:
        """Validate *arguments* against ``input_schema``.

        Returns a (possibly empty) list of human-readable error strings.
        The default implementation accepts anything; override or rely on JSON
        Schema validation done upstream in the agent loop.
        """
        return []

    def to_openai_schema(self) -> dict[str, Any]:
        """Return an OpenAI-compatible function schema dict."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    @abc.abstractmethod
    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Execute the tool and return a :class:`~nexus.models.ToolResult`."""


class BaseTool(Protocol):
    """Structural protocol every Nexus tool must satisfy.

    Tools are registered in a :class:`ToolRegistry` and invoked by the agent
    during its agentic loop.  They must be stateless across calls — all
    per-call state lives in :class:`~nexus.models.ToolExecutionContext`.

    Prefer subclassing :class:`Tool` which satisfies this protocol and
    provides useful default implementations.
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

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def unregister_source(self, *, source: str, origin: str | None = None) -> tuple[str, ...]:
        removed: list[str] = []
        for name, record in list(self._tools.items()):
            if record.source != source:
                continue
            if origin is not None and record.origin != origin:
                continue
            removed.append(name)
            del self._tools[name]
        return tuple(removed)

    def schemas(self) -> tuple[dict[str, Any], ...]:
        return tuple(tool_to_schema(tool) for tool in self.all())

    def clear(self) -> None:
        """Remove all registered tools. Mutates in-place so existing references stay valid."""
        self._tools.clear()
