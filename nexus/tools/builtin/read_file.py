"""ReadFileTool — read text file contents with optional line range."""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.utils import allow_hidden_reads, count_tokens, is_binary_file, read_path_policy_error, resolve_path, truncate_text

_MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB
_MAX_OUTPUT_TOKENS = 25_000


class ReadFileTool(Tool):
    """Read the contents of a text file within the workspace.

    Supports ``start_line`` / ``end_line`` for line ranges (also accepts
    ``offset`` / ``limit`` for reference-code compatibility).
    Binary files are rejected with a descriptive error.
    """

    name = "read_file"
    description = (
        "Read the contents of a text file. "
        "Use start_line and end_line (1-indexed, inclusive) to read a specific range. "
        "Cannot read files outside the workspace or binary files."
    )
    kind = ToolKind.READ
    is_mutating = False
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
                "description": "First line to read (1-based). Defaults to 1.",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Last line to read (1-based, inclusive). Defaults to end of file.",
            },
            "offset": {
                "type": "integer",
                "minimum": 1,
                "description": "Alias for start_line (reference code compat).",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Max number of lines to read from offset (reference code compat).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

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
        allow_hidden = allow_hidden_reads(context.metadata)

        # Workspace boundary check
        try:
            path.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to access paths outside the current workspace.",
                is_error=True,
            )

        policy_error = read_path_policy_error(path, workspace, allow_hidden=allow_hidden)
        if policy_error is not None:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=policy_error,
                is_error=True,
            )

        if not path.exists():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"File not found: {raw_path}", is_error=True)
        if not path.is_file():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Path is not a file: {raw_path}", is_error=True)

        file_size = path.stat().st_size
        if file_size > _MAX_FILE_BYTES:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"File too large ({file_size / (1024*1024):.1f} MB). Maximum is 10 MB.",
                is_error=True,
            )

        if is_binary_file(path):
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"Cannot read binary file: {path.name}. This tool only reads text files.",
                is_error=True,
            )

        try:
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="latin-1")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Read error: {exc}", is_error=True)

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)

        if total_lines == 0:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="File is empty.",
                metadata={"total_lines": 0, "lines_read": 0},
            )

        # Resolve range — prefer start_line/end_line; fall back to offset/limit
        offset = arguments.get("offset")
        limit = arguments.get("limit")
        start_line = arguments.get("start_line")
        end_line = arguments.get("end_line")

        if start_line is not None:
            start_idx = max(0, int(start_line) - 1)
        elif offset is not None:
            start_idx = max(0, int(offset) - 1)
        else:
            start_idx = 0

        if end_line is not None:
            end_idx = min(int(end_line), total_lines)
        elif limit is not None:
            end_idx = min(start_idx + int(limit), total_lines)
        else:
            end_idx = total_lines

        selected = lines[start_idx:end_idx]
        output = "".join(selected).rstrip("\n")

        if count_tokens(output) > _MAX_OUTPUT_TOKENS:
            output = truncate_text(
                output,
                _MAX_OUTPUT_TOKENS,
                suffix=f"\n... [truncated — {total_lines} total lines]",
            )

        lines_read = end_idx - start_idx
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            metadata={
                "path": str(path.relative_to(workspace)),
                "total_lines": total_lines,
                "start_line": start_idx + 1,
                "end_line": end_idx,
                "lines_read": lines_read,
            },
        )
