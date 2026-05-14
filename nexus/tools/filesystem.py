"""Filesystem and shell tools for the Nexus agent.

This module is now a **shim**.  The core tools (ReadFileTool, WriteFileTool,
GlobTool, GrepTool, ListDirTool/LsTool, ShellTool/BashTool) have been moved
to ``nexus.tools.builtin`` where each tool lives in its own module.

**Nexus-specific tools** not found in the reference implementation remain here:
- :class:`ModifyFileTool` — line-range replacement (targets a specific range)
- :class:`ReplaceTextTool` — literal text find-and-replace in a file

``classify_bash_risk`` is re-exported here for compatibility with older imports.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import FileDiff, ToolConfirmation, ToolKind
from nexus.tools.builtin.edit_file import EditTool

# ---------------------------------------------------------------------------
# Re-exports from nexus.tools.builtin (backward-compat imports)
# ---------------------------------------------------------------------------
from nexus.tools.builtin.glob import GlobTool
from nexus.tools.builtin.grep import GrepTool
from nexus.tools.builtin.list_dir import ListDirTool, LsTool
from nexus.tools.builtin.read_file import ReadFileTool
from nexus.tools.builtin.shell import BashTool, ShellTool, classify_bash_risk
from nexus.tools.builtin.write_file import WriteFileTool

__all__ = [
    # Re-exported from builtin/
    "ReadFileTool",
    "WriteFileTool",
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "LsTool",
    "ShellTool",
    "BashTool",
    # Nexus-specific, defined here
    "ModifyFileTool",
    "ReplaceTextTool",
    "classify_bash_risk",
]


# ---------------------------------------------------------------------------
# Workspace path helpers (shared by ModifyFileTool and ReplaceTextTool)
# ---------------------------------------------------------------------------

def _resolve_path(workspace: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (workspace / candidate).resolve()


def _workspace_write_check(target: Path, workspace: Path) -> str | None:
    try:
        target.relative_to(workspace)
    except ValueError:
        return "Refusing to write outside the current workspace."
    nexus_root = (workspace / ".nexus").resolve()
    try:
        target.relative_to(nexus_root)
        return "Refusing to write into Nexus-managed .nexus state."
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tool: modify_file   [Nexus-specific — line-range replacement]
# ---------------------------------------------------------------------------

class ModifyFileTool:
    name = "modify_file"
    description = (
        "Replace a specific range of lines in an existing file within the workspace. "
        "start_line and end_line are 1-indexed and inclusive. "
        "Use read_file first to check current line numbers before calling this tool. "
        "Cannot modify files outside the workspace or in .nexus/ managed state."
    )
    kind = ToolKind.WRITE
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "File path relative to workspace root.",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "First line to replace (1-indexed, inclusive).",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Last line to replace (1-indexed, inclusive).",
            },
            "new_content": {
                "type": "string",
                "description": "Replacement content for the specified line range.",
            },
        },
        "required": ["path", "start_line", "end_line", "new_content"],
        "additionalProperties": False,
    }
    is_mutating = True

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        del call_id
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return None

        workspace = context.working_directory.resolve()
        target = _resolve_path(workspace, raw_path)
        if _workspace_write_check(target, workspace) or not target.exists() or not target.is_file():
            return None

        try:
            original = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        lines = original.splitlines(keepends=True)
        start_line = int(arguments.get("start_line", 1))
        end_line = int(arguments.get("end_line", start_line))
        if start_line > end_line or start_line < 1 or start_line > len(lines):
            return None

        new_content = str(arguments.get("new_content", ""))
        replacement_lines = new_content.splitlines(keepends=True)
        if replacement_lines and not replacement_lines[-1].endswith("\n"):
            replacement_lines[-1] += "\n"
        updated = "".join(lines[: start_line - 1] + replacement_lines + lines[end_line:])

        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=f"Modify lines {start_line}-{end_line} in {target}",
            diff=FileDiff(path=target, old_content=original, new_content=updated),
            affected_paths=[target],
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
        target = _resolve_path(workspace, raw_path)
        if err := _workspace_write_check(target, workspace):
            return ToolResult(call_id=call_id, tool_name=self.name, output=err, is_error=True)

        if not target.exists():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"File not found: {raw_path}", is_error=True)
        if not target.is_file():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Not a file: {raw_path}", is_error=True)

        start_line = int(arguments.get("start_line", 1))
        end_line = int(arguments.get("end_line", start_line))
        if start_line > end_line:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"start_line ({start_line}) must be ≤ end_line ({end_line})",
                is_error=True,
            )

        try:
            original = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Read error: {exc}", is_error=True)

        lines = original.splitlines(keepends=True)
        total_lines = len(lines)
        if start_line > total_lines:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"start_line {start_line} exceeds file length ({total_lines} lines)",
                is_error=True,
            )

        new_content = str(arguments.get("new_content", ""))
        replacement_lines = new_content.splitlines(keepends=True)
        if replacement_lines and not replacement_lines[-1].endswith("\n"):
            replacement_lines[-1] += "\n"

        result_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
        updated = "".join(result_lines)

        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

        lines_replaced = end_line - start_line + 1
        lines_written = len(replacement_lines)
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=(
                f"Replaced lines {start_line}–{end_line} "
                f"({lines_replaced} line(s) → {lines_written} line(s)) "
                f"in {target.relative_to(workspace)}"
            ),
            metadata={
                "path": str(target.relative_to(workspace)),
                "start_line": start_line,
                "end_line": end_line,
                "lines_replaced": lines_replaced,
                "lines_written": lines_written,
            },
        )


# ---------------------------------------------------------------------------
# Tool: replace_text   [Nexus-specific — literal find-and-replace]
# ---------------------------------------------------------------------------


class ReplaceTextTool(EditTool):
    name = "replace_text"
    description = (
        "Replace literal text in an existing file within the workspace. "
        "By default only the first occurrence is replaced; set replace_all=true "
        "to replace every occurrence. Compatibility wrapper around the edit tool."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def _edit_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": arguments.get("path", ""),
            "old_string": arguments.get("old_text", ""),
            "new_string": arguments.get("new_text", ""),
            "replace_all": bool(arguments.get("replace_all", False)),
            "_replace_first": True,
        }

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        confirmation = await super().get_confirmation(call_id, self._edit_arguments(arguments), context)
        if confirmation is not None:
            confirmation.tool_name = self.name
            confirmation.params = arguments
        return confirmation

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        if not str(arguments.get("old_text", "")):
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: old_text", is_error=True)
        result = await super().execute(call_id, self._edit_arguments(arguments), context)
        result.tool_name = self.name
        return result


# ---------------------------------------------------------------------------
# End of Nexus-specific tools
# ---------------------------------------------------------------------------
