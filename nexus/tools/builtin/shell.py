"""ShellTool — execute a shell command with safety checks.

Blocked-command list and process-group kill on timeout mirror the reference
implementation.  The tool name stays ``"bash"`` for backward compatibility
with existing config and permission policies.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import signal
import sys
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolConfirmation, ToolKind
from nexus.tools.utils import resolve_path

# ---------------------------------------------------------------------------
# Risk classification (self-contained to avoid circular imports)
# ---------------------------------------------------------------------------
import re as _re
import shlex as _shlex

_HIGH_PATTERNS = [
    _re.compile(r"\brm\b.*\s-[a-zA-Z]*[rRfF][a-zA-Z]*"),
    _re.compile(r"\bsudo\b"),
    _re.compile(r"\bdd\b.*(if=|of=)"),
    _re.compile(r"\b(mkfs|fdisk|parted)\b"),
    _re.compile(r"\|\s*(sh|bash|zsh|fish)(\s|$)"),
    _re.compile(r"\b(killall|pkill)\b"),
]
_MEDIUM_PATTERNS = [
    _re.compile(r"\brm\b"),
    _re.compile(r"\bmv\b"),
    _re.compile(r"\bcp\b"),
    _re.compile(r"\bchmod\b"),
    _re.compile(r"\bchown\b"),
    _re.compile(r"\bsed\b.*-i"),
    _re.compile(r">+\s*\S"),
    _re.compile(r"\bgit\s+(add|commit|push|reset|rebase|merge)\b"),
    _re.compile(r"\b(npm|pip|pip3|uv|brew|apt|apt-get)\s+install\b"),
]
_LOW_CMDS = frozenset({
    "cat", "echo", "printf", "pwd", "date", "ls", "find", "grep", "rg",
    "awk", "wc", "head", "tail", "sort", "uniq", "diff", "which", "type",
    "env", "printenv", "uname", "hostname", "whoami", "id", "ps", "pgrep",
    "file", "stat", "du", "df", "tree", "jq", "python", "python3", "node",
})
_LOW_GIT = frozenset({"status", "log", "diff", "show", "branch", "remote", "fetch", "ls-files"})


def _classify_risk(command: str) -> str:
    s = command.strip()
    for p in _HIGH_PATTERNS:
        if p.search(s):
            return "high"
    for p in _MEDIUM_PATTERNS:
        if p.search(s):
            return "medium"
    try:
        tokens = _shlex.split(s)
    except ValueError:
        return "medium"
    if not tokens:
        return "low"
    from pathlib import Path as _Path
    base = _Path(tokens[0]).name
    if base == "git":
        sub = tokens[1] if len(tokens) > 1 else ""
        return "low" if sub in _LOW_GIT else "medium"
    return "low" if base in _LOW_CMDS else "medium"

# Commands blocked outright — no confirmation, instant rejection
BLOCKED_COMMANDS: frozenset[str] = frozenset({
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "mkfs",
    "fdisk",
    "parted",
    ":(){ :|:& };:",   # Fork bomb
    "chmod 777 /",
    "chmod -R 777",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init 0",
    "init 6",
})


def _is_blocked(command: str) -> bool:
    cmd_lower = command.lower().strip()
    return any(blocked in cmd_lower for blocked in BLOCKED_COMMANDS)


class ShellTool(Tool):
    """Execute a shell command in the workspace root (or a given sub-directory).

    Commands are checked against a blocked list before execution.  Timed-out
    processes are killed via their process group so spawned children are also
    terminated.
    """

    name = "bash"           # keep "bash" for backward compat with config / permissions
    description = (
        "Execute a shell command. "
        "Dangerous commands (rm -rf /, shutdown, …) are blocked outright. "
        "Specify timeout (seconds) and cwd (relative to workspace) as needed."
    )
    kind = ToolKind.SHELL
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": 600,
                "description": "Timeout in seconds (default: 120).",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory relative to workspace root (default: workspace root).",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        command = str(arguments.get("command", ""))
        is_dangerous = _is_blocked(command)
        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=f"Execute{'  ⚠ BLOCKED' if is_dangerous else ''}: {command}",
            command=command,
            is_dangerous=is_dangerous,
        )

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: command", is_error=True)

        if _is_blocked(command):
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"Command blocked for safety: {command}",
                is_error=True,
                metadata={"blocked": True},
            )

        timeout = int(arguments.get("timeout", 120))
        raw_cwd = arguments.get("cwd")
        if raw_cwd:
            work_dir = resolve_path(context.working_directory, str(raw_cwd))
            if not work_dir.exists():
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Working directory does not exist: {work_dir}", is_error=True)
        else:
            work_dir = context.working_directory

        env = self._build_env()
        shell_cmd = ["cmd.exe", "/c", command] if sys.platform == "win32" else ["/bin/bash", "-c", command]

        process = await asyncio.create_subprocess_exec(
            *shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env=env,
            start_new_session=True,
        )

        try:
            stdout_data, stderr_data = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Command timed out after {timeout}s", is_error=True, metadata={"timeout": True})

        stdout = stdout_data.decode("utf-8", errors="replace")
        stderr = stderr_data.decode("utf-8", errors="replace")
        exit_code = process.returncode

        output = stdout.rstrip()
        if stderr.strip():
            output += "\n--- stderr ---\n" + stderr.rstrip()
        if exit_code != 0:
            output += f"\nExit code: {exit_code}"

        # Cap at 100 KB to protect context window
        if len(output) > 100 * 1024:
            output = output[: 100 * 1024] + "\n... [output truncated]"

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            is_error=exit_code != 0,
            metadata={"exit_code": exit_code, "risk": _classify_risk(command)},
        )

    def _build_env(self) -> dict[str, str]:
        return os.environ.copy()


# Alias — "BashTool" is the name used in tests and the filesystem shim
BashTool = ShellTool
