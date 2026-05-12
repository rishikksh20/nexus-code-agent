"""GlobTool — find files matching a glob pattern."""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.utils import allow_hidden_reads, can_read_match, read_path_policy_error, resolve_path


class GlobTool(Tool):
    """Find files matching a glob pattern in a directory.

    Supports ``**`` for recursive matching.  Results are capped at 1 000
    entries to avoid overwhelming the context window.
    """

    name = "glob"
    description = (
        "Find files matching a glob pattern. "
        "Supports ** for recursive matching (e.g. 'src/**/*.py'). "
        "Results are relative to the workspace root."
    )
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "minLength": 1,
                "description": "Glob pattern to match.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (default: workspace root).",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    _MAX_RESULTS = 1_000

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        pattern = str(arguments.get("pattern", "")).strip()
        if not pattern:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: pattern", is_error=True)

        raw_path = str(arguments.get("path", "."))
        workspace = context.working_directory.resolve()
        search_path = resolve_path(workspace, raw_path)
        allow_hidden = allow_hidden_reads(context.metadata)

        try:
            search_path.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
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

        if not search_path.exists() or not search_path.is_dir():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Directory does not exist: {search_path}", is_error=True)

        try:
            matches = [
                p for p in search_path.glob(pattern)
                if p.is_file() and can_read_match(p, workspace, allow_hidden=allow_hidden)
            ]
        except Exception as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Glob error: {exc}", is_error=True)

        truncated = len(matches) > self._MAX_RESULTS
        shown = matches[: self._MAX_RESULTS]

        lines: list[str] = []
        for fp in shown:
            try:
                lines.append(str(fp.relative_to(context.working_directory)))
            except ValueError:
                lines.append(str(fp))

        if truncated:
            lines.append(f"... (limited to {self._MAX_RESULTS} results, {len(matches)} total)")

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output="\n".join(lines) if lines else f"No matches found for '{pattern}'",
            metadata={"path": str(search_path), "match_count": len(matches), "truncated": truncated},
        )
