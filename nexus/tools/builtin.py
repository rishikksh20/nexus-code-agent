from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import ToolKind


class GetTimeTool:
    name = "get_time"
    description = "Return the current UTC timestamp."
    kind = ToolKind.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    is_mutating = False

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        now = datetime.now(UTC).isoformat()
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=now,
            metadata={"timezone": "UTC"},
        )


class WriteNoteTool:
    name = "write_note"
    description = "Write a short note within the current workspace."
    kind = ToolKind.WRITE
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    is_mutating = True

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
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output="Missing required argument: path",
                is_error=True,
            )

        target = (context.working_directory / raw_path).resolve()
        workspace = context.working_directory.resolve()
        try:
            target.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output="Refusing to write outside the current workspace.",
                is_error=True,
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        content = str(arguments.get("content", ""))
        byte_count = len(content.encode("utf-8"))
        if byte_count > self.max_bytes:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"Refusing to write note larger than {self.max_bytes} bytes.",
                is_error=True,
                metadata={"bytes_attempted": byte_count, "max_bytes": self.max_bytes},
            )
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=f"Wrote {Path(target).relative_to(workspace)}",
            metadata={"bytes_written": byte_count, "max_bytes": self.max_bytes},
        )
