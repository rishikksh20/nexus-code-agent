"""TodoTool — session-scoped task list for the agent.

Todos exist only for the lifetime of the current agent session (in-memory).
They are useful for tracking progress on multi-step tasks.
"""
from __future__ import annotations

import uuid
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind


class TodoTool(Tool):
    """Manage a task list for the current agent session.

    Actions: ``add``, ``complete``, ``list``, ``clear``.

    Todos are in-memory and do not survive across sessions.
    """

    name = "todos"
    description = (
        "Manage a task list for the current session. "
        "Use this to track progress on multi-step tasks. "
        "Actions: add, complete, list, clear."
    )
    kind = ToolKind.MEMORY
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "complete", "list", "clear"],
                "description": "Action to perform on the todo list.",
            },
            "id": {
                "type": "string",
                "description": "Todo ID (required for complete).",
            },
            "content": {
                "type": "string",
                "description": "Todo description (required for add).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self) -> None:
        self._todos: dict[str, str] = {}

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        action = str(arguments.get("action", "")).lower().strip()

        if action == "add":
            content = arguments.get("content")
            if not content:
                return ToolResult(call_id=call_id, tool_name=self.name, output="'content' is required for add", is_error=True)
            todo_id = str(uuid.uuid4())[:8]
            self._todos[todo_id] = str(content)
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Added [{todo_id}]: {content}")

        if action == "complete":
            todo_id = arguments.get("id")
            if not todo_id:
                return ToolResult(call_id=call_id, tool_name=self.name, output="'id' is required for complete", is_error=True)
            if str(todo_id) not in self._todos:
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Todo not found: {todo_id}", is_error=True)
            content = self._todos.pop(str(todo_id))
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Completed [{todo_id}]: {content}")

        if action == "list":
            if not self._todos:
                return ToolResult(call_id=call_id, tool_name=self.name, output="No todos.", metadata={"count": 0})
            lines = ["Todos:"] + [f"  [{tid}] {text}" for tid, text in self._todos.items()]
            return ToolResult(call_id=call_id, tool_name=self.name, output="\n".join(lines), metadata={"count": len(self._todos)})

        if action == "clear":
            count = len(self._todos)
            self._todos.clear()
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Cleared {count} todos.")

        return ToolResult(call_id=call_id, tool_name=self.name, output=f"Unknown action: {action!r}. Use add, complete, list, or clear.", is_error=True)
