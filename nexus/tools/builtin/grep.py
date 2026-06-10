"""GrepTool — regex search across file contents."""
from __future__ import annotations

import re
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.utils import allow_hidden_reads, read_path_policy_error, resolve_path, walk_text_files


DEFAULT_MAX_RESULTS = 200
MAX_MAX_RESULTS = 1000
DEFAULT_MAX_OUTPUT_CHARS = 20_000
MAX_MAX_OUTPUT_CHARS = 100_000


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
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_MAX_RESULTS,
                "description": f"Maximum matching lines to return (default: {DEFAULT_MAX_RESULTS}).",
            },
            "max_output_chars": {
                "type": "integer",
                "minimum": 100,
                "maximum": MAX_MAX_OUTPUT_CHARS,
                "description": f"Maximum output characters to return (default: {DEFAULT_MAX_OUTPUT_CHARS}).",
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
        allow_hidden = allow_hidden_reads(context.metadata)

        # Workspace boundary check
        try:
            search_path.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to search outside the current workspace.",
                is_error=True,
            )

        policy_error = read_path_policy_error(search_path, workspace, allow_hidden=allow_hidden)
        if policy_error is not None:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=policy_error,
                is_error=True,
            )

        case_insensitive = bool(arguments.get("case_insensitive", False))
        fixed_string = bool(arguments.get("fixed_string", False))
        max_results = _bounded_int(arguments.get("max_results"), DEFAULT_MAX_RESULTS, minimum=1, maximum=MAX_MAX_RESULTS)
        max_output_chars = _bounded_int(
            arguments.get("max_output_chars"),
            DEFAULT_MAX_OUTPUT_CHARS,
            minimum=100,
            maximum=MAX_MAX_OUTPUT_CHARS,
        )

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

        files = (
            walk_text_files(search_path, allow_hidden=allow_hidden)
            if search_path.is_dir() else [search_path]
        )

        output_lines: list[str] = []
        total_matches = 0
        emitted_matches = 0
        output_chars = 0
        truncated_by_results = False
        truncated_by_chars = False

        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            file_matches: list[str] = []
            for i, line in enumerate(content.splitlines(), start=1):
                if pattern.search(line):
                    total_matches += 1
                    if emitted_matches >= max_results:
                        truncated_by_results = True
                        break
                    file_matches.append(f"{i}:{line}")
                    emitted_matches += 1

            if file_matches:
                try:
                    rel = file_path.relative_to(context.working_directory)
                except ValueError:
                    rel = file_path
                candidate_lines = [f"=== {rel} ===", *file_matches, ""]
                for output_line in candidate_lines:
                    next_chars = output_chars + len(output_line) + 1
                    if next_chars > max_output_chars:
                        truncated_by_chars = True
                        break
                    output_lines.append(output_line)
                    output_chars = next_chars
                if truncated_by_chars:
                    break
            if truncated_by_results:
                break

        truncated = truncated_by_results or truncated_by_chars
        if not output_lines and truncated_by_chars and total_matches:
            output_lines.append(
                "Truncated grep results before the first matching line fit."
            )
            output_chars = len(output_lines[-1]) + 1
        if not output_lines:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"No matches found for pattern '{pattern_str}'",
                metadata={"pattern": pattern_str, "matches": 0, "files_searched": len(files)},
            )

        if truncated:
            output_chars = _append_notice_with_budget(
                output_lines,
                "Truncated grep results.",
                max_output_chars,
                output_chars,
            )

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output="\n".join(output_lines),
            metadata={
                "pattern": pattern_str,
                "matches": emitted_matches,
                "matches_seen": total_matches,
                "files_searched": len(files),
                "truncated": truncated,
                "truncated_by_results": truncated_by_results,
                "truncated_by_chars": truncated_by_chars,
                "max_results": max_results,
                "max_output_chars": max_output_chars,
            },
        )


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _append_notice_with_budget(lines: list[str], notice: str, max_chars: int, current_chars: int) -> int:
    notice_chars = len(notice) + 1
    while lines and current_chars + notice_chars > max_chars:
        removed = lines.pop()
        current_chars -= len(removed) + 1
    if current_chars + notice_chars <= max_chars:
        lines.append(notice)
        return current_chars + notice_chars
    lines[:] = [notice[:max_chars]]
    return len(lines[0])
