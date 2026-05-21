"""Compatibility filesystem tool surface.

The runtime's canonical tools live under :mod:`nexus.tools.builtin`.  This
module keeps older imports working without adding legacy names to the default
core registry.
"""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import FileDiff, ToolConfirmation
from nexus.tools.builtin import (
    BashTool,
    GlobTool,
    GrepTool,
    LsTool,
    ReadFileTool,
    WriteFileTool,
)
from nexus.tools.builtin.edit_file import EditTool
from nexus.tools.builtin.shell import classify_bash_risk
from nexus.tools.utils import ensure_parent, resolve_path


class ModifyFileTool(EditTool):
    """Legacy line-range editor backed by the current workspace path policy."""

    name = "modify_file"
    description = "Compatibility line-range editor. Prefer edit or insert_edit_into_file in new code."

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        del call_id
        workspace = context.working_directory.resolve()
        path = resolve_path(workspace, str(arguments.get("path", "")).strip())
        if not _is_writable_workspace_path(path, workspace):
            return None
        old_content = path.read_text(encoding="utf-8") if path.exists() else ""
        new_content = _apply_line_range(old_content, arguments)
        if new_content is None:
            return None
        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=f"Modify file: {path}",
            diff=FileDiff(path=path, old_content=old_content, new_content=new_content),
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
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: path", is_error=True)
        workspace = context.working_directory.resolve()
        path = resolve_path(workspace, raw_path)
        error = _write_path_error(path, workspace)
        if error is not None:
            return ToolResult(call_id=call_id, tool_name=self.name, output=error, is_error=True)
        if not path.exists():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"File not found: {raw_path}", is_error=True)
        try:
            old_content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Read error: {exc}", is_error=True)
        new_content = _apply_line_range(old_content, arguments)
        if new_content is None:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Invalid line range.", is_error=True)
        try:
            ensure_parent(path)
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=f"Updated {path.relative_to(workspace)}",
            metadata={"path": str(path.relative_to(workspace))},
        )


class ReplaceTextTool(EditTool):
    """Legacy first-match text replacement wrapper."""

    name = "replace_text"
    description = "Compatibility text replacement tool. Prefer edit in new code."

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        return await super().get_confirmation(call_id, _edit_arguments(arguments), context)

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        return await super().execute(call_id, _edit_arguments(arguments), context)


def _edit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    replace_all = bool(arguments.get("replace_all", False))
    return {
        "path": arguments.get("path", ""),
        "old_string": arguments.get("old_text", ""),
        "new_string": arguments.get("new_text", ""),
        "replace_all": replace_all,
        "_replace_first": not replace_all,
    }


def _apply_line_range(content: str, arguments: dict[str, Any]) -> str | None:
    try:
        start_line = int(arguments.get("start_line", 0))
        end_line = int(arguments.get("end_line", 0))
    except (TypeError, ValueError):
        return None
    if start_line < 1 or end_line < start_line:
        return None
    lines = content.splitlines(keepends=True)
    if start_line > len(lines):
        return None
    replacement = str(arguments.get("new_content", "")).splitlines(keepends=True)
    if arguments.get("new_content", "") and not replacement:
        replacement = [str(arguments.get("new_content", ""))]
    return "".join(lines[: start_line - 1] + replacement + lines[end_line:])


def _is_writable_workspace_path(path, workspace) -> bool:
    return _write_path_error(path, workspace) is None


def _write_path_error(path, workspace) -> str | None:
    try:
        path.relative_to(workspace)
    except ValueError:
        return "Refusing to write outside the current workspace."
    try:
        path.relative_to((workspace / ".nexus").resolve())
    except ValueError:
        return None
    return "Refusing to write into .nexus managed state directory."


__all__ = [
    "BashTool",
    "GlobTool",
    "GrepTool",
    "LsTool",
    "ModifyFileTool",
    "ReadFileTool",
    "ReplaceTextTool",
    "WriteFileTool",
    "classify_bash_risk",
]
