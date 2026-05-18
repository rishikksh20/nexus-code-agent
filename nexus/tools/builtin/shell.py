"""ShellTool — execute a shell command with safety checks.

Blocked-command list and process-group kill on timeout mirror the reference
implementation.  The tool name stays ``"bash"`` for backward compatibility
with existing config and permission policies.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import signal
import shlex
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolConfirmation, ToolKind
from nexus.tools.utils import resolve_path

# ---------------------------------------------------------------------------
# Risk classification
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

        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        stream_output = _stream_output_callback(context, call_id, self.name)
        stdout_task = asyncio.create_task(
            _read_process_stream(
                process.stdout,
                "stdout",
                stdout_parts,
                stream_output,
            )
        )
        stderr_task = asyncio.create_task(
            _read_process_stream(
                process.stderr,
                "stderr",
                stderr_parts,
                stream_output,
            )
        )

        try:
            await asyncio.wait_for(
                asyncio.gather(process.wait(), stdout_task, stderr_task),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                if sys.platform != "win32":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            for task in (stdout_task, stderr_task):
                task.cancel()
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Command timed out after {timeout}s", is_error=True, metadata={"timeout": True})

        stdout_data = b"".join(stdout_parts)
        stderr_data = b"".join(stderr_parts)
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
            metadata={"exit_code": exit_code, "risk": classify_bash_risk(command)},
        )

    def _build_env(self) -> dict[str, str]:
        return os.environ.copy()


StreamOutputCallback = Callable[[str, str], Awaitable[None] | None]


def _stream_output_callback(
    context: ToolExecutionContext,
    call_id: str,
    tool_name: str,
) -> StreamOutputCallback | None:
    ui = context.metadata.get("ui")
    callback = getattr(ui, "stream_tool_output", None)
    if not callable(callback):
        return None

    def stream(stream_name: str, chunk: str) -> Awaitable[None] | None:
        return callback(call_id, tool_name, stream_name, chunk)

    return stream


async def _read_process_stream(
    stream: asyncio.StreamReader | None,
    stream_name: str,
    sink: list[bytes],
    callback: StreamOutputCallback | None,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        sink.append(chunk)
        if callback is None:
            continue
        text = chunk.decode("utf-8", errors="replace")
        maybe_awaitable = callback(stream_name, text)
        if maybe_awaitable is not None:
            await maybe_awaitable


# Alias — "BashTool" is the name used in tests and the filesystem shim
BashTool = ShellTool
