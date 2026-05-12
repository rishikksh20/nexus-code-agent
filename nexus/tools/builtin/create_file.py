"""CreateFileTool — create a *new* file; refuse to overwrite existing ones.

Unlike :class:`~nexus.tools.builtin.write_file.WriteFileTool` (which creates
**or** overwrites), this tool is a strict *create-only* primitive.  Calling it
on a path that already exists returns an error, making it safe to use when the
intent is always to produce a brand-new artefact.

Use :class:`~nexus.tools.builtin.write_file.WriteFileTool` when you need to
replace an existing file, and
:class:`~nexus.tools.builtin.edit_file.EditTool` /
:class:`~nexus.tools.builtin.smart_edit.InsertEditIntoFileTool`
for surgical in-place edits.
"""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import FileDiff, Tool, ToolConfirmation, ToolKind
from nexus.tools.utils import ensure_parent, resolve_path


class CreateFileTool(Tool):
    """Create a brand-new file with the supplied content.

    Raises an error if the file already exists.  Parent directories are created
    automatically.  Cannot write outside the workspace or into ``.nexus/``.
    """

    name = "create_file"
    description = (
        "Create a new file with the given content. "
        "Fails with an error if the file already exists — use write_file to overwrite. "
        "Parent directories are created automatically. "
        "Cannot create files outside the workspace or inside .nexus/ managed state."
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
                "description": "Full content for the new file.",
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
        content = str(arguments.get("content", ""))
        diff = FileDiff(path=path, old_content="", new_content=content, is_new_file=True)
        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=f"Create new file: {path}",
            diff=diff,
            affected_paths=[path],
        )

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Missing required argument: path", is_error=True,
            )

        workspace = context.working_directory.resolve()
        path = resolve_path(workspace, raw_path)

        # Workspace boundary check
        try:
            path.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to write outside the current workspace.", is_error=True,
            )

        # Refuse writes into .nexus managed state
        nexus_root = (workspace / ".nexus").resolve()
        try:
            path.relative_to(nexus_root)
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to write into .nexus managed state directory.", is_error=True,
            )
        except ValueError:
            pass

        # ---- The key guard: refuse to overwrite ----
        if path.exists():
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=(
                    f"File already exists: {path.relative_to(workspace)}. "
                    "Use write_file to overwrite an existing file, or edit / insert_edit_into_file "
                    "to make targeted changes."
                ),
                is_error=True,
            )

        content = str(arguments.get("content", ""))

        try:
            ensure_parent(path)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"Write error: {exc}", is_error=True,
            )

        lines = len(content.splitlines())
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=f"Created {path.relative_to(workspace)} — {lines} lines",
            metadata={
                "path": str(path.relative_to(workspace)),
                "is_new_file": True,
                "lines": lines,
            },
        )

