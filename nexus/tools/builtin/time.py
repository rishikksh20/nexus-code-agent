"""GetTimeTool — return the current UTC timestamp."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind


class GetTimeTool(Tool):
    """Return the current UTC timestamp as an ISO-8601 string."""

    name = "get_time"
    description = "Return the current UTC timestamp."
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

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
