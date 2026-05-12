"""WriteFileTool — create or overwrite a file with full content."""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import FileDiff, Tool, ToolConfirmation, ToolKind
from nexus.tools.utils import ensure_parent, resolve_path


class WriteFileTool(Tool):
    """Write content to a file, creating it (and parent directories) if needed.

    Completely overwrites existing files.  For surgical edits use
    :class:`~nexus.tools.builtin.edit_file.EditTool`.
    """

    name = "write_file"
    description = (
        "Write content to a file. Creates the file if it does not exist, or "
        "overwrites it completely if it does. Parent directories are created "
        "automatically. Prefer the edit tool for changes to existing files; use this for new files or true full rewrites."
    )
    kind = ToolKind.WRITE
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "File path (relative to workspace or absolute).",
            },
            "content": {
                "type": "string",
                "description": "Full content to write to the file.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        raw_path = str(arguments.get("path", "")).strip()
        path = resolve_path(context.working_directory, raw_path)
        is_new = not path.exists()
        old_content = ""
        if not is_new:
            try:
                old_content = path.read_text(encoding="utf-8")
            except OSError:
                pass
        new_content = str(arguments.get("content", ""))
        diff = FileDiff(path=path, old_content=old_content, new_content=new_content, is_new_file=is_new)
        action = "Create" if is_new else "Overwrite"
        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=f"{action} file: {path}",
            diff=diff,
            affected_paths=[path],
            is_dangerous=not is_new,
        )

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: path", is_error=True)

        workspace = context.working_directory.resolve()
        path = resolve_path(workspace, raw_path)

        # Workspace boundary check
        try:
            path.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to write outside the current workspace.",
                is_error=True,
            )

        # Refuse writes into .nexus managed state
        nexus_root = (workspace / ".nexus").resolve()
        try:
            path.relative_to(nexus_root)
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to write into .nexus managed state directory.",
                is_error=True,
            )
        except ValueError:
            pass

        is_new = not path.exists()
        content = str(arguments.get("content", ""))

        try:
            ensure_parent(path)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

        action = "Created" if is_new else "Updated"
        lines = len(content.splitlines())
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=f"{action} {path.relative_to(workspace)} — {lines} lines",
            metadata={"path": str(path.relative_to(workspace)), "is_new_file": is_new, "lines": lines},
        )
