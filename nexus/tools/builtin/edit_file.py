"""EditTool — surgical find-and-replace editor.

Matches the reference ``edit`` tool: the ``old_string`` must match exactly
(including whitespace and indentation) and must be unique in the file unless
``replace_all`` is set.  Use :class:`~nexus.tools.builtin.write_file.WriteFileTool`
for full rewrites.
"""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import FileDiff, Tool, ToolConfirmation, ToolKind
from nexus.tools.utils import ensure_parent, resolve_path


class EditTool(Tool):
    """Edit a file by finding and replacing an exact text string.

    *old_string* must match exactly (whitespace, indentation included).
    When the file does not exist and *old_string* is empty, a new file is
    created with *new_string* as content.
    """

    name = "edit"
    description = (
        "Edit a file by replacing text. old_string must match exactly "
        "(including whitespace and indentation) and must be unique in the file "
        "unless replace_all is true. For new files or full rewrites use write_file."
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
            "old_string": {
                "type": "string",
                "description": "Exact text to find and replace. Leave empty to create a new file.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text. Can be empty to delete old_string.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence of old_string (default: false).",
            },
        },
        "required": ["path", "old_string", "new_string"],
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
        old_string = str(arguments.get("old_string", ""))
        new_string = str(arguments.get("new_string", ""))
        replace_all = bool(arguments.get("replace_all", False))
        is_new = not path.exists()

        if is_new:
            diff = FileDiff(path=path, old_content="", new_content=new_string, is_new_file=True)
            return ToolConfirmation(
                tool_name=self.name, params=arguments,
                description=f"Create new file: {path}", diff=diff, affected_paths=[path],
            )

        old_content = path.read_text(encoding="utf-8")
        new_content = (
            old_content.replace(old_string, new_string)
            if replace_all
            else old_content.replace(old_string, new_string, 1)
        )
        diff = FileDiff(path=path, old_content=old_content, new_content=new_content)
        return ToolConfirmation(
            tool_name=self.name, params=arguments,
            description=f"Edit file: {path}", diff=diff, affected_paths=[path],
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

        path = resolve_path(context.working_directory, raw_path)
        old_string = str(arguments.get("old_string", ""))
        new_string = str(arguments.get("new_string", ""))
        replace_all = bool(arguments.get("replace_all", False))

        # --- Create new file ---
        if not path.exists():
            if old_string:
                return ToolResult(
                    call_id=call_id, tool_name=self.name,
                    output=f"File does not exist: {path}. To create a new file, leave old_string empty.",
                    is_error=True,
                )
            ensure_parent(path)
            path.write_text(new_string, encoding="utf-8")
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"Created {path} — {len(new_string.splitlines())} lines",
                metadata={"path": str(path), "is_new_file": True},
            )

        # --- Edit existing file ---
        try:
            old_content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Read error: {exc}", is_error=True)

        if not old_string:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="old_string is empty but file exists. Provide old_string to edit, or use write_file to overwrite.",
                is_error=True,
            )

        count = old_content.count(old_string)
        if count == 0:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"old_string not found in {path}. Check whitespace/indentation.",
                is_error=True,
            )

        if count > 1 and not replace_all:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=(
                    f"old_string found {count} times in {path}. "
                    "Either provide more context to make the match unique, "
                    "or set replace_all=true."
                ),
                is_error=True,
                metadata={"occurrence_count": count},
            )

        if replace_all:
            new_content = old_content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = old_content.replace(old_string, new_string, 1)
            replaced = 1

        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=f"Replaced {replaced} of {count} occurrence(s) in {path}",
            metadata={"path": str(path), "occurrences_found": count, "occurrences_replaced": replaced},
        )
