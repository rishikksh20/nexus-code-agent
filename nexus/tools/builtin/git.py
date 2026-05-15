"""Read-only git inspection tools."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.utils import resolve_path


class GitStatusTool(Tool):
    name = "git_status"
    description = "Return structured git branch and working tree status for the current workspace."
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        del arguments
        result = await _run_git(context.working_directory, "status", "--porcelain=v1", "-b")
        if result["exit_code"] != 0:
            return ToolResult(call_id=call_id, tool_name=self.name, output=result["stderr"], is_error=True, metadata=result)
        parsed = _parse_porcelain_status(result["stdout"])
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=json.dumps(parsed, indent=2),
            metadata=parsed,
        )


class GitDiffTool(Tool):
    name = "git_diff"
    description = "Return a git diff for working tree, staged changes, a file, or a commit range."
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "enum": ["working", "staged", "head"],
                "description": "Diff target: working tree, staged/index, or working tree against HEAD.",
            },
            "path": {
                "type": "string",
                "description": "Optional workspace-relative path to limit the diff.",
            },
            "ref": {
                "type": "string",
                "description": "Optional git ref/range to diff, e.g. HEAD~1..HEAD.",
            },
            "stat": {
                "type": "boolean",
                "description": "Return --stat output instead of full patch.",
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
        target = str(arguments.get("target", "working")).strip() or "working"
        if target not in {"working", "staged", "head"}:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Invalid target.", is_error=True)

        args = ["diff"]
        if arguments.get("stat"):
            args.append("--stat")
        raw_ref = str(arguments.get("ref", "")).strip()
        if raw_ref:
            args.append(raw_ref)
        elif target == "staged":
            args.append("--staged")
        elif target == "head":
            args.append("HEAD")

        path_arg = str(arguments.get("path", "")).strip()
        if path_arg:
            path = resolve_path(context.working_directory, path_arg)
            try:
                path.relative_to(context.working_directory.resolve())
            except ValueError:
                return ToolResult(call_id=call_id, tool_name=self.name, output="Path is outside the workspace.", is_error=True)
            args.extend(["--", str(path.relative_to(context.working_directory.resolve()))])

        result = await _run_git(context.working_directory, *args)
        is_error = result["exit_code"] != 0
        output = result["stdout"] if result["stdout"] else result["stderr"]
        if len(output) > 100_000:
            output = output[:100_000] + "\n... [diff truncated]"
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output or "(no diff)",
            is_error=is_error,
            metadata={
                "target": target,
                "path": path_arg,
                "ref": raw_ref,
                "exit_code": result["exit_code"],
            },
        )


async def _run_git(workspace: Path, *args: str) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(workspace),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_data, stderr_data = await process.communicate()
    return {
        "exit_code": process.returncode,
        "stdout": stdout_data.decode("utf-8", errors="replace").rstrip(),
        "stderr": stderr_data.decode("utf-8", errors="replace").rstrip(),
    }


def _parse_porcelain_status(output: str) -> dict[str, Any]:
    branch = ""
    upstream = ""
    ahead = 0
    behind = 0
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    changed: list[dict[str, str]] = []

    for line in output.splitlines():
        if line.startswith("## "):
            branch_text = line[3:]
            branch, upstream, ahead, behind = _parse_branch_line(branch_text)
            continue
        if len(line) < 3:
            continue
        index_status = line[0]
        worktree_status = line[1]
        path = line[3:]
        changed.append({"path": path, "index": index_status, "worktree": worktree_status})
        if index_status == "?" and worktree_status == "?":
            untracked.append(path)
            continue
        if index_status != " ":
            staged.append(path)
        if worktree_status != " ":
            unstaged.append(path)

    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "changed": changed,
        "clean": not (staged or unstaged or untracked),
    }


def _parse_branch_line(value: str) -> tuple[str, str, int, int]:
    branch_part, _, meta = value.partition("...")
    branch = branch_part.strip()
    upstream = ""
    ahead = 0
    behind = 0
    if meta:
        upstream, _, rest = meta.partition(" ")
        if "[" in rest and "]" in rest:
            markers = rest[rest.index("[") + 1 : rest.index("]")].split(",")
            for marker in markers:
                parts = marker.strip().split()
                if len(parts) == 2 and parts[0] == "ahead":
                    ahead = int(parts[1])
                elif len(parts) == 2 and parts[0] == "behind":
                    behind = int(parts[1])
    return branch, upstream, ahead, behind
