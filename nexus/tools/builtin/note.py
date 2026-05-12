"""WriteNoteTool — write a short note within the current workspace."""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind


class WriteNoteTool(Tool):
    """Write a short note file inside the current workspace directory.

    Cannot write outside the workspace boundary or into ``.nexus/`` state.
    """

    name = "write_note"
    description = "Write a short note within the current workspace."
    kind = ToolKind.WRITE
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, *, max_bytes: int = 65_536) -> None:
        self.max_bytes = max_bytes

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: path", is_error=True)

        target = (context.working_directory / raw_path).resolve()
        workspace = context.working_directory.resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Refusing to write outside the current workspace.", is_error=True)

        content = str(arguments.get("content", ""))
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"Content is larger than {self.max_bytes} bytes ({len(encoded)} bytes). Use write_file for large content.",
                is_error=True,
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=f"Wrote {len(encoded)} bytes to {target.relative_to(workspace)}",
            metadata={"path": str(target.relative_to(workspace)), "bytes": len(encoded)},
        )
