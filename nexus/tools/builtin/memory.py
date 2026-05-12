"""MemoryTool — persistent key/value agent memory store.

Memory entries survive across sessions.  They are stored as a JSON file in
the configured memory directory (defaults to ``~/.nexus/memory/``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind

_DEFAULT_MEMORY_DIR = Path.home() / ".nexus" / "memory"
_MEMORY_FILE = "agent_memory.json"


class MemoryTool(Tool):
    """Store and retrieve persistent key/value memory.

    Actions: ``set``, ``get``, ``delete``, ``list``, ``clear``.

    Memory persists across agent sessions in a JSON file on disk.
    """

    name = "memory"
    description = (
        "Store and retrieve persistent memory across sessions. "
        "Actions: set, get, delete, list, clear."
    )
    kind = ToolKind.MEMORY
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "get", "delete", "list", "clear"],
                "description": "Memory action to perform.",
            },
            "key": {
                "type": "string",
                "description": "Memory key (required for set, get, delete).",
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
        self._memory_path = (memory_dir or _DEFAULT_MEMORY_DIR) / _MEMORY_FILE

    # ------------------------------------------------------------------
    # Private persistence helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        if not self._memory_path.exists():
            return {}
        try:
            data = json.loads(self._memory_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, entries: dict[str, str]) -> None:
        self._memory_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        action = str(arguments.get("action", "")).lower().strip()
        key = arguments.get("key")
        value = arguments.get("value")

        if action == "set":
            if not key or value is None:
                return ToolResult(call_id=call_id, tool_name=self.name, output="'key' and 'value' are required for set", is_error=True)
            entries = self._load()
            entries[str(key)] = str(value)
            self._save(entries)
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Stored memory: {key}")

        if action == "get":
            if not key:
                return ToolResult(call_id=call_id, tool_name=self.name, output="'key' is required for get", is_error=True)
            entries = self._load()
            if str(key) not in entries:
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Memory not found: {key}", metadata={"found": False})
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"{key}: {entries[str(key)]}",
                metadata={"found": True},
            )

        if action == "delete":
            if not key:
                return ToolResult(call_id=call_id, tool_name=self.name, output="'key' is required for delete", is_error=True)
            entries = self._load()
            if str(key) not in entries:
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Memory not found: {key}")
            del entries[str(key)]
            self._save(entries)
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Deleted memory: {key}")

        if action == "list":
            entries = self._load()
            if not entries:
                return ToolResult(call_id=call_id, tool_name=self.name, output="No memories stored.", metadata={"count": 0})
            lines = ["Stored memories:"] + [f"  {k}: {v}" for k, v in sorted(entries.items())]
            return ToolResult(call_id=call_id, tool_name=self.name, output="\n".join(lines), metadata={"count": len(entries)})

        if action == "clear":
            entries = self._load()
            count = len(entries)
            self._save({})
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Cleared {count} memory entries.")

        return ToolResult(call_id=call_id, tool_name=self.name, output=f"Unknown action: {action!r}. Use set, get, delete, list, or clear.", is_error=True)
