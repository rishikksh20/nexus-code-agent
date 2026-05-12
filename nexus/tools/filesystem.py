"""Filesystem and shell tools for the Nexus agent.

This module is now a **shim**.  The core tools (ReadFileTool, WriteFileTool,
GlobTool, GrepTool, ListDirTool/LsTool, ShellTool/BashTool) have been moved
to ``nexus.tools.builtin`` where each tool lives in its own module.

**Nexus-specific tools** not found in the reference implementation remain here:
- :class:`ModifyFileTool` — line-range replacement (targets a specific range)
- :class:`ReplaceTextTool` — literal text find-and-replace in a file

``classify_bash_risk`` and the workspace path helpers also remain here because
the permission / approval system imports them directly from this module.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import ToolKind

# ---------------------------------------------------------------------------
# Re-exports from nexus.tools.builtin (backward-compat imports)
# ---------------------------------------------------------------------------
from nexus.tools.builtin.glob import GlobTool
from nexus.tools.builtin.grep import GrepTool
from nexus.tools.builtin.list_dir import ListDirTool, LsTool
from nexus.tools.builtin.read_file import ReadFileTool
from nexus.tools.builtin.shell import BashTool, ShellTool
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
# Bash risk classifier — used by the permission / approval system
# ---------------------------------------------------------------------------

_HIGH_RISK_REGEXES: list[re.Pattern[str]] = [
    re.compile(r"\brm\b.*\s-[a-zA-Z]*[rRfF][a-zA-Z]*"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bsu\b(\s|$)"),
    re.compile(r"\bdd\b.*(if=|of=)"),
    re.compile(r"\b(mkfs|fdisk|parted)\b"),
    re.compile(r"\|\s*(sh|bash|zsh|fish|ksh|csh)(\s|$)"),
    re.compile(r"\bkill\s+(-9|-SIGKILL)\b"),
    re.compile(r"\b(killall|pkill)\b"),
    re.compile(r"\bshred\b"),
    re.compile(r"\bchmod\b.*\s-[rR]\b"),
    re.compile(r"\bchown\b.*\s-[rR]\b"),
    re.compile(r">+\s*/(?:etc|usr|bin|sbin|lib|boot|sys|proc|dev)/"),
]

_MEDIUM_RISK_REGEXES: list[re.Pattern[str]] = [
    re.compile(r"\brm\b"),
    re.compile(r"\bmv\b"),
    re.compile(r"\bcp\b"),
    re.compile(r"\btouch\b"),
    re.compile(r"\bmkdir\b"),
    re.compile(r"\bchmod\b"),
    re.compile(r"\bchown\b"),
    re.compile(r"\bsed\b.*-i"),
    re.compile(r"\btee\b"),
    re.compile(r">+\s*\S"),
    re.compile(r"\bgit\s+(add|commit|push|reset|rebase|merge)\b"),
    re.compile(r"\bgit\s+checkout\s+-[bB]\b"),
    re.compile(r"\b(npm|pip|pip3|uv|brew|apt|apt-get|yum|dnf|pacman|snap)\s+install\b"),
    re.compile(r"\bpython3?\s+-m\s+pip\b"),
]

_LOW_RISK_BASE_COMMANDS: frozenset[str] = frozenset({
    "cat", "echo", "printf", "pwd", "date", "ls", "ll", "la",
    "find", "locate", "grep", "rg", "ag", "awk", "wc",
    "head", "tail", "sort", "uniq", "diff",
    "which", "type", "command", "env", "printenv",
    "uname", "hostname", "whoami", "id", "groups",
    "ps", "pgrep",
    "file", "stat", "du", "df", "lsof",
    "tree", "less", "more", "bat",
    "jq", "yq", "xmllint",
    "python", "python3", "node", "ruby", "perl",
})

_LOW_RISK_GIT_SUBCMDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "remote",
    "fetch", "ls-files", "ls-tree", "describe", "tag", "--version",
    "shortlog", "stash list",
})


def classify_bash_risk(command: str) -> str:
    """Return ``'low'``, ``'medium'``, or ``'high'`` for *command*."""
    stripped = command.strip()
    for pattern in _HIGH_RISK_REGEXES:
        if pattern.search(stripped):
            return "high"
    for pattern in _MEDIUM_RISK_REGEXES:
        if pattern.search(stripped):
            return "medium"
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return "medium"
    if not tokens:
        return "low"
    base_cmd = Path(tokens[0]).name
    if base_cmd == "git":
        sub = tokens[1] if len(tokens) > 1 else ""
        return "low" if sub in _LOW_RISK_GIT_SUBCMDS else "medium"
    return "low" if base_cmd in _LOW_RISK_BASE_COMMANDS else "medium"


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
# Tool: replace_text   [Nexus-specific — literal text find-and-replace]
# ---------------------------------------------------------------------------

class ReplaceTextTool:
    name = "replace_text"
    description = (
        "Find and replace a literal text string in a file within the workspace. "
        "Returns an error if old_text is not found. "
        "Set replace_all=true to replace every occurrence; default is first occurrence only. "
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
            "old_text": {
                "type": "string",
                "minLength": 1,
                "description": "The exact text to find.",
            },
            "new_text": {
                "type": "string",
                "description": "The replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence. Defaults to false (first occurrence only).",
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }
    is_mutating = True

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

        old_text = str(arguments.get("old_text", ""))
        new_text = str(arguments.get("new_text", ""))
        replace_all = bool(arguments.get("replace_all", False))
        if not old_text:
            return ToolResult(call_id=call_id, tool_name=self.name, output="old_text must not be empty", is_error=True)

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Read error: {exc}", is_error=True)

        count = content.count(old_text)
        if count == 0:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"Text not found in {raw_path}: {old_text!r}",
                is_error=True,
            )

        updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
        replaced = count if replace_all else 1

        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=f"Replaced {replaced} of {count} occurrence(s) in {target.relative_to(workspace)}",
            metadata={
                "path": str(target.relative_to(workspace)),
                "occurrences_found": count,
                "occurrences_replaced": replaced,
            },
        )
