"""ListDirTool — list directory contents."""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.utils import resolve_path


class ListDirTool(Tool):
    """List the contents of a directory.

    Directories are listed before files.  Hidden entries (starting with ``.``)
    are excluded unless ``include_hidden`` is set.
    """

    name = "list_dir"
    description = "List contents of a directory. Directories appear before files."
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to workspace root (default: workspace root).",
            },
            "show_hidden": {
                "type": "boolean",
                "description": "Include hidden files/directories (those starting with '.') (default: false).",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Alias for show_hidden.",
            },
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        raw_path = str(arguments.get("path", "."))
        workspace = context.working_directory.resolve()
        dir_path = resolve_path(workspace, raw_path)

        # Workspace boundary check
        try:
            dir_path.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to list directories outside the current workspace.",
                is_error=True,
            )

        if not dir_path.exists() or not dir_path.is_dir():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Directory does not exist: {dir_path}", is_error=True)

        # Support both show_hidden and include_hidden param names
        show_hidden = bool(arguments.get("show_hidden", arguments.get("include_hidden", False)))

        try:
            items = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Error listing directory: {exc}", is_error=True)

        if not show_hidden:
            items = [item for item in items if not item.name.startswith(".")]

        if not items:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Directory is empty.",
                metadata={"path": str(dir_path), "entries": 0},
            )

        lines = [f"{item.name}/" if item.is_dir() else item.name for item in items]
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output="\n".join(lines),
            metadata={"path": str(dir_path), "entries": len(items)},
        )


# Alias kept for backward compatibility with tests/code that imported LsTool
LsTool = ListDirTool
