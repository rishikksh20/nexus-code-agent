"""Filesystem and shell tools for the Nexus agent.

Risk levels used by the permission system
------------------------------------------
- LOW    read-only, no side effects (cat, grep, ls, git status …)
- MEDIUM targeted writes that are recoverable (mkdir, cp, mv, git commit …)
- HIGH   destructive or privileged operations that may be irreversible
         (rm -rf, sudo, pipe-to-shell, package installs …)

File tools enforce workspace isolation: paths outside the workspace root or
inside the .nexus/ managed state directory are rejected before execution.
Bash commands are run with the workspace root as the working directory.
"""
from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import ToolKind

# ---------------------------------------------------------------------------
# Bash risk classifier
# ---------------------------------------------------------------------------

# Ordered from most specific / dangerous to least.
_HIGH_RISK_REGEXES: list[re.Pattern[str]] = [
    # rm with recursive or force flags (rm -rf, rm -fr, rm -r, etc.)
    re.compile(r"\brm\b.*\s-[a-zA-Z]*[rRfF][a-zA-Z]*"),
    # Privilege escalation
    re.compile(r"\bsudo\b"),
    re.compile(r"\bsu\b(\s|$)"),
    # Disk / block device operations
    re.compile(r"\bdd\b.*(if=|of=)"),
    re.compile(r"\b(mkfs|fdisk|parted)\b"),
    # Pipe output directly into a shell interpreter
    re.compile(r"\|\s*(sh|bash|zsh|fish|ksh|csh)(\s|$)"),
    # Signals
    re.compile(r"\bkill\s+(-9|-SIGKILL)\b"),
    re.compile(r"\b(killall|pkill)\b"),
    # Data destruction
    re.compile(r"\bshred\b"),
    # Recursive permission / ownership changes
    re.compile(r"\bchmod\b.*\s-[rR]\b"),
    re.compile(r"\bchown\b.*\s-[rR]\b"),
    # Writes to system directories
    re.compile(r">+\s*/(?:etc|usr|bin|sbin|lib|boot|sys|proc|dev)/"),
]

_MEDIUM_RISK_REGEXES: list[re.Pattern[str]] = [
    re.compile(r"\brm\b"),            # rm without destructive flags still deletes
    re.compile(r"\bmv\b"),            # rename / move
    re.compile(r"\bcp\b"),            # copy (may overwrite)
    re.compile(r"\btouch\b"),         # create empty file
    re.compile(r"\bmkdir\b"),         # create directory
    re.compile(r"\bchmod\b"),         # change permissions
    re.compile(r"\bchown\b"),         # change ownership
    re.compile(r"\bsed\b.*-i"),       # in-place sed edit
    re.compile(r"\btee\b"),           # tee writes to a file
    re.compile(r">+\s*\S"),           # any output redirection (>file or >>file)
    # git write operations
    re.compile(r"\bgit\s+(add|commit|push|reset|rebase|merge)\b"),
    re.compile(r"\bgit\s+checkout\s+-[bB]\b"),
    # package managers installing new software
    re.compile(r"\b(npm|pip|pip3|uv|brew|apt|apt-get|yum|dnf|pacman|snap)\s+install\b"),
    re.compile(r"\bpython3?\s+-m\s+pip\b"),
]

# Base commands considered inherently low-risk (read-only).
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

# git subcommands that are read-only.
_LOW_RISK_GIT_SUBCMDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "remote",
    "fetch", "ls-files", "ls-tree", "describe", "tag", "--version",
    "shortlog", "stash list",
})


def classify_bash_risk(command: str) -> str:
    """Return ``'low'``, ``'medium'``, or ``'high'`` for *command*.

    Evaluation order:

    1. HIGH patterns are checked first — any match returns ``'high'``.
    2. MEDIUM patterns are checked next — any match returns ``'medium'``.
    3. If the leading token is a known read-only command, return ``'low'``.
       ``git`` is special-cased: only known read-only subcommands are low.
    4. Unknown commands default to ``'medium'`` (safer than assuming low).
    """
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
        return "medium"  # unparsable → escalate

    if not tokens:
        return "low"

    base_cmd = Path(tokens[0]).name  # /usr/bin/grep → grep

    if base_cmd == "git":
        sub = tokens[1] if len(tokens) > 1 else ""
        return "low" if sub in _LOW_RISK_GIT_SUBCMDS else "medium"

    return "low" if base_cmd in _LOW_RISK_BASE_COMMANDS else "medium"


# ---------------------------------------------------------------------------
# Workspace path helpers
# ---------------------------------------------------------------------------

def _resolve_path(workspace: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (workspace / candidate).resolve()


def _workspace_read_check(target: Path, workspace: Path) -> str | None:
    """Return an error string if *target* is outside *workspace*, else None."""
    try:
        target.relative_to(workspace)
    except ValueError:
        return "Refusing to access paths outside the current workspace."
    return None


def _workspace_write_check(target: Path, workspace: Path) -> str | None:
    """Return an error string if *target* violates write policy, else None.

    Write policy:
    - Must be inside workspace root.
    - Must not be inside .nexus/ managed state.
    """
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


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1_024:
        return f"{size_bytes}B"
    if size_bytes < 1_048_576:
        return f"{size_bytes / 1_024:.1f}KB"
    return f"{size_bytes / 1_048_576:.1f}MB"


# ---------------------------------------------------------------------------
# Tool: read_file
# ---------------------------------------------------------------------------

class ReadFileTool:
    name = "read_file"
    description = (
        "Read the contents of a file within the workspace. "
        "Use start_line / end_line (both 1-indexed, inclusive) to read a specific line range. "
        "Returns file content; metadata includes total_lines and lines_read."
    )
    kind = ToolKind.READ
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
                "description": "First line to return (1-indexed). Defaults to 1.",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Last line to return (1-indexed, inclusive). Defaults to end of file.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    is_mutating = False

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
        if err := _workspace_read_check(target, workspace):
            return ToolResult(call_id=call_id, tool_name=self.name, output=err, is_error=True)

        if not target.exists():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"File not found: {raw_path}", is_error=True)
        if not target.is_file():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Not a file: {raw_path}", is_error=True)

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Read error: {exc}", is_error=True)

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        start_idx = max(0, int(arguments.get("start_line", 1)) - 1)
        end_idx = min(total_lines, int(arguments.get("end_line", total_lines)))
        selected = "".join(lines[start_idx:end_idx])

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=selected,
            metadata={
                "path": str(target.relative_to(workspace)),
                "total_lines": total_lines,
                "start_line": start_idx + 1,
                "end_line": end_idx,
                "lines_read": end_idx - start_idx,
            },
        )


# ---------------------------------------------------------------------------
# Tool: write_file   [HIGH RISK — always confirmed]
# ---------------------------------------------------------------------------

class WriteFileTool:
    name = "write_file"
    description = (
        "Write content to a file within the workspace, creating it if it does not exist "
        "or completely overwriting it if it does. "
        "HIGH RISK — the entire file is replaced. Use modify_file or replace_text for targeted edits. "
        "Cannot write outside the workspace or into .nexus/ managed state."
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
            "content": {
                "type": "string",
                "description": "Full content to write to the file.",
            },
        },
        "required": ["path", "content"],
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

        content = str(arguments.get("content", ""))
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

        byte_count = len(content.encode("utf-8"))
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=f"Wrote {target.relative_to(workspace)}",
            metadata={
                "path": str(target.relative_to(workspace)),
                "bytes_written": byte_count,
            },
        )


# ---------------------------------------------------------------------------
# Tool: modify_file   [MEDIUM RISK — confirmed in default mode]
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
                call_id=call_id,
                tool_name=self.name,
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
                call_id=call_id,
                tool_name=self.name,
                output=f"start_line {start_line} exceeds file length ({total_lines} lines)",
                is_error=True,
            )

        new_content = str(arguments.get("new_content", ""))
        replacement_lines = new_content.splitlines(keepends=True)
        # Ensure the last replacement line ends with a newline so it does not
        # run into the first line that follows.
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
# Tool: replace_text   [MEDIUM RISK — confirmed in default mode]
# ---------------------------------------------------------------------------

class ReplaceTextTool:
    name = "replace_text"
    description = (
        "Find and replace a literal text string in a file within the workspace. "
        "Returns an error if old_text is not found. "
        "Set replace_all=true to replace every occurrence; default is the first occurrence only. "
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
                call_id=call_id,
                tool_name=self.name,
                output=f"Text not found in {raw_path}: {old_text!r}",
                is_error=True,
            )

        if replace_all:
            updated = content.replace(old_text, new_text)
            replaced = count
        else:
            updated = content.replace(old_text, new_text, 1)
            replaced = 1

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


# ---------------------------------------------------------------------------
# Tool: glob   [LOW RISK — auto-approved]
# ---------------------------------------------------------------------------

class GlobTool:
    name = "glob"
    description = (
        "Search for files and directories within the workspace matching a glob pattern. "
        "The pattern is evaluated relative to the workspace root. "
        "Use ** to match across subdirectories (e.g. 'src/**/*.py'). "
        "Directories are excluded from results by default."
    )
    kind = ToolKind.READ
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "minLength": 1,
                "description": "Glob pattern relative to workspace root (e.g. '**/*.py').",
            },
            "include_dirs": {
                "type": "boolean",
                "description": "Include directories in results. Defaults to false.",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    is_mutating = False

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        pattern = str(arguments.get("pattern", "")).strip()
        if not pattern:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: pattern", is_error=True)

        include_dirs = bool(arguments.get("include_dirs", False))
        workspace = context.working_directory.resolve()

        try:
            raw_matches = sorted(workspace.glob(pattern))
        except Exception as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Glob error: {exc}", is_error=True)

        relative_matches: list[str] = []
        for match in raw_matches:
            if not include_dirs and not match.is_file():
                continue
            try:
                relative_matches.append(str(match.relative_to(workspace)))
            except ValueError:
                pass  # skip paths that escaped the workspace

        if not relative_matches:
            output = f"No matches for pattern: {pattern}"
        else:
            output = "\n".join(relative_matches)

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            metadata={"pattern": pattern, "match_count": len(relative_matches)},
        )


# ---------------------------------------------------------------------------
# Tool: grep   [LOW RISK — auto-approved]
# ---------------------------------------------------------------------------

class GrepTool:
    name = "grep"
    kind = ToolKind.READ
    description = (
        "Search file contents within the workspace for lines matching a pattern. "
        "Returns matching lines with their file path and 1-indexed line number. "
        "Supports regular expressions (default) and literal string matching (fixed_string=true). "
        "Searches recursively when given a directory path."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "minLength": 1,
                "description": "Regex or literal text to search for.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search (relative to workspace root). Defaults to workspace root.",
            },
            "fixed_string": {
                "type": "boolean",
                "description": "Treat pattern as a literal string rather than a regex. Defaults to false.",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive matching. Defaults to false.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "description": "Maximum matching lines to return. Defaults to 100.",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    is_mutating = False

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        pattern = str(arguments.get("pattern", "")).strip()
        if not pattern:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: pattern", is_error=True)

        workspace = context.working_directory.resolve()
        raw_path = str(arguments.get("path", "")).strip()
        if raw_path:
            search_root = _resolve_path(workspace, raw_path)
            if err := _workspace_read_check(search_root, workspace):
                return ToolResult(call_id=call_id, tool_name=self.name, output=err, is_error=True)
        else:
            search_root = workspace

        fixed = bool(arguments.get("fixed_string", False))
        case_insensitive = bool(arguments.get("case_insensitive", False))
        max_results = min(500, int(arguments.get("max_results", 100)))
        flags = re.IGNORECASE if case_insensitive else 0

        try:
            compiled = re.compile(re.escape(pattern) if fixed else pattern, flags)
        except re.error as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Invalid regex pattern: {exc}", is_error=True)

        files: list[Path]
        if search_root.is_file():
            files = [search_root]
        else:
            files = sorted(p for p in search_root.rglob("*") if p.is_file())

        results: list[str] = []
        truncated = False
        for file_path in files:
            if len(results) >= max_results:
                truncated = True
                break
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_num, line in enumerate(text.splitlines(), start=1):
                if len(results) >= max_results:
                    truncated = True
                    break
                if compiled.search(line):
                    try:
                        rel = file_path.relative_to(workspace)
                    except ValueError:
                        continue
                    results.append(f"{rel}:{line_num}: {line}")

        if not results:
            output = f"No matches for: {pattern}"
        else:
            suffix = f"\n(truncated at {max_results} results)" if truncated else ""
            output = "\n".join(results) + suffix

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            metadata={"pattern": pattern, "match_count": len(results), "truncated": truncated},
        )


# ---------------------------------------------------------------------------
# Tool: ls   [LOW RISK — auto-approved]
# ---------------------------------------------------------------------------

class LsTool:
    name = "ls"
    kind = ToolKind.READ
    description = (
        "List the contents of a directory within the workspace. "
        "Shows file names, types (file/dir), and sizes. "
        "Defaults to the workspace root when no path is given."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to workspace root. Defaults to workspace root.",
            },
            "show_hidden": {
                "type": "boolean",
                "description": "Include hidden entries (names starting with '.'). Defaults to false.",
            },
        },
        "required": [],
        "additionalProperties": False,
    }
    is_mutating = False

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        workspace = context.working_directory.resolve()
        raw_path = str(arguments.get("path", "")).strip()
        if raw_path:
            target = _resolve_path(workspace, raw_path)
            if err := _workspace_read_check(target, workspace):
                return ToolResult(call_id=call_id, tool_name=self.name, output=err, is_error=True)
        else:
            target = workspace

        if not target.exists():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Path not found: {raw_path or '.'}", is_error=True)
        if not target.is_dir():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Not a directory: {raw_path or '.'}", is_error=True)

        show_hidden = bool(arguments.get("show_hidden", False))
        # Sort: dirs first, then files; within each group alphabetically.
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))

        lines: list[str] = []
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            if entry.is_dir():
                lines.append(f"  {entry.name}/")
            elif entry.is_file():
                lines.append(f"  {entry.name}  ({_format_size(entry.stat().st_size)})")
            else:
                lines.append(f"  {entry.name}  (special)")

        rel = str(target.relative_to(workspace)) if target != workspace else "."
        output = f"{rel}:\n" + ("\n".join(lines) if lines else "  (empty)")

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            metadata={"path": rel, "entry_count": len(lines)},
        )


# ---------------------------------------------------------------------------
# Tool: bash   [LOW / MEDIUM / HIGH RISK — classified per command]
# ---------------------------------------------------------------------------

class BashTool:
    """Execute a bash command in the workspace root directory.

    Risk is classified per command by ``classify_bash_risk`` and drives the
    permission decision in ``PermissionChecker._bash_policy``:

    - LOW:    read-only commands — auto-approved in all modes.
    - MEDIUM: targeted writes — requires confirmation in default mode,
              auto-approved in auto mode, denied in plan mode.
    - HIGH:   destructive / privileged — confirmation required in all modes
              (including auto), denied in plan mode.
    """

    name = "bash"
    kind = ToolKind.SHELL
    description = (
        "Run a bash command in the workspace root directory.\n"
        "Commands are risk-classified before execution:\n"
        "  LOW    — read-only (cat, grep, ls, git status …)         auto-approved\n"
        "  MEDIUM — targeted writes (mkdir, mv, git commit …)        confirmed in default mode\n"
        "  HIGH   — destructive / privileged (rm -rf, sudo, …)      always confirmed\n"
        "The working directory is always the workspace root. Timeout: 30 s."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "description": "The bash command to execute.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    is_mutating = True
    _TIMEOUT_SECONDS: int = 30

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: command", is_error=True)

        workspace = context.working_directory.resolve()
        risk = classify_bash_risk(command)

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workspace,
                ),
                timeout=self._TIMEOUT_SECONDS,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"Command timed out after {self._TIMEOUT_SECONDS}s",
                is_error=True,
                metadata={"command": command, "risk": risk},
            )
        except OSError as exc:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"Failed to start process: {exc}",
                is_error=True,
                metadata={"command": command, "risk": risk},
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace").rstrip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").rstrip()

        if stdout and stderr:
            combined = f"{stdout}\n[stderr]\n{stderr}"
        elif stderr:
            combined = f"[stderr]\n{stderr}"
        else:
            combined = stdout

        is_error = proc.returncode != 0
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=combined or "(no output)",
            is_error=is_error,
            metadata={"command": command, "risk": risk, "exit_code": proc.returncode},
        )
