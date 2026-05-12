"""GrepTool — regex search across file contents."""
from __future__ import annotations

import re
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.utils import resolve_path, walk_text_files


class GrepTool(Tool):
    """Search for a regex pattern in file contents.

    Returns matching lines grouped by file with line numbers.
    """

    name = "grep"
    description = (
        "Search for a regex pattern in file contents. "
        "Returns matching lines with file paths and line numbers."
    )
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "minLength": 1,
                "description": "Regular expression pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (default: workspace root).",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive search (default: false).",
            },
            "fixed_string": {
                "type": "boolean",
                "description": "Treat pattern as a literal string, not a regex (default: false).",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        pattern_str = str(arguments.get("pattern", "")).strip()
        if not pattern_str:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: pattern", is_error=True)

        raw_path = str(arguments.get("path", "."))
        workspace = context.working_directory.resolve()
        search_path = resolve_path(workspace, raw_path)

        # Workspace boundary check
        try:
            search_path.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to search outside the current workspace.",
                is_error=True,
            )

        case_insensitive = bool(arguments.get("case_insensitive", False))
        fixed_string = bool(arguments.get("fixed_string", False))

        if not search_path.exists():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Path does not exist: {search_path}", is_error=True)

        try:
            if fixed_string:
                compiled_pattern_str = re.escape(pattern_str)
            else:
                compiled_pattern_str = pattern_str
            flags = re.IGNORECASE if case_insensitive else 0
            pattern = re.compile(compiled_pattern_str, flags)
        except re.error as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Invalid regex: {exc}", is_error=True)

        files = walk_text_files(search_path) if search_path.is_dir() else [search_path]

        output_lines: list[str] = []
        total_matches = 0

        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            file_matches: list[str] = []
            for i, line in enumerate(content.splitlines(), start=1):
                if pattern.search(line):
                    file_matches.append(f"{i}:{line}")
                    total_matches += 1

            if file_matches:
                try:
                    rel = file_path.relative_to(context.working_directory)
                except ValueError:
                    rel = file_path
                output_lines.append(f"=== {rel} ===")
                output_lines.extend(file_matches)
                output_lines.append("")

        if not output_lines:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"No matches found for pattern '{pattern_str}'",
                metadata={"pattern": pattern_str, "matches": 0, "files_searched": len(files)},
            )

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output="\n".join(output_lines),
            metadata={"pattern": pattern_str, "matches": total_matches, "files_searched": len(files)},
        )
