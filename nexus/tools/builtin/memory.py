"""MemoryTool — persistent key/value agent memory store.

Memory entries survive across sessions. They are stored as a single JSON
dictionary under the configured memory directory (defaults to
``~/.nexus/memory/``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.memory.store import MemoryEntry, MemoryStore
from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind

_DEFAULT_MEMORY_DIR = Path.home() / ".nexus" / "memory"


class MemoryTool(Tool):
    """Store and retrieve persistent key/value memory.

    Actions: ``set``, ``get``, ``delete``, ``list``, ``search``, ``clear``.

    Memory persists across agent sessions in a JSON file on disk.
    """

    name = "memory"
    description = (
        "Store and retrieve persistent memory across sessions. Use this for user preferences, identity, and important context instead "
        "of writing ad-hoc memory files. Actions: set, get, delete, list, search, clear."
    )
    kind = ToolKind.MEMORY
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "get", "delete", "list", "search", "clear"],
                "description": "Memory action to perform.",
            },
            "key": {
                "type": "string",
                "description": "Memory key (required for set, get, delete; optional for search as query term).",
            },
            "value": {
                "type": "string",
                "description": "Value to store (required for set).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, memory_dir: Path | None = None) -> None:
        root = memory_dir or _DEFAULT_MEMORY_DIR
        self._store = MemoryStore(root)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context
        action = str(arguments.get("action", "")).lower().strip()
        key = arguments.get("key")
        value = arguments.get("value")

        if action == "set":
            if not key or not value:
                return ToolResult(call_id=call_id, tool_name=self.name, output="`key` and `value` are required for 'set' action", is_error=True)
            self._store.save(MemoryEntry(key=str(key), content=str(value), keywords=(str(key),)))
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Set memory: {key}")

        if action == "get":
            if not key:
                return ToolResult(call_id=call_id, tool_name=self.name, output="`key` required for 'get' action", is_error=True)
            entry = self._store.load(str(key))
            if entry is None:
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Memory not found: {key}", metadata={"found": False})
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"Memory found: {key}: {entry.content}",
                metadata={"found": True},
            )

        if action == "delete":
            if not key:
                return ToolResult(call_id=call_id, tool_name=self.name, output="`key` required for 'delete' action", is_error=True)
            if not self._store.delete(str(key)):
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Memory not found: {key}")
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Deleted memory: {key}")

        if action == "list":
            entries = {
                entry.key: entry.content
                for entry in (self._store.load(key) for key in self._store.list_keys())
                if entry is not None
            }
            if not entries:
                return ToolResult(call_id=call_id, tool_name=self.name, output="No memories stored", metadata={"found": False, "count": 0})
            lines = ["Stored memories:"] + [f"  {k}: {v}" for k, v in sorted(entries.items())]
            return ToolResult(call_id=call_id, tool_name=self.name, output="\n".join(lines), metadata={"found": True, "count": len(entries)})

        if action == "search":
            query = str(key or value or "").strip()
            if not query:
                return ToolResult(call_id=call_id, tool_name=self.name, output="`key` (as search query) required for 'search' action", is_error=True)
            matches = self._store.search(query)
            if not matches:
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"No memories found for: {query}", metadata={"found": False, "count": 0})
            lines = [f"Found {len(matches)} match(es) for '{query}':"] + [f"  {e.key}: {e.content}" for e in matches]
            return ToolResult(call_id=call_id, tool_name=self.name, output="\n".join(lines), metadata={"found": True, "count": len(matches)})

        if action == "clear":
            count = self._store.clear()
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Cleared {count} memory entries")

        return ToolResult(call_id=call_id, tool_name=self.name, output=f"Unknown action: {action}", is_error=True)
