"""Structured verification and formatter command tools."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolConfirmation, ToolKind
from nexus.tools.utils import resolve_path


class _CommandTool(Tool):
    kind = ToolKind.SHELL
    is_mutating = False
    command: tuple[str, ...] = ()
    default_timeout = 120

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional command arguments appended after the default command.",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": 600,
                "description": "Timeout in seconds.",
            },
            "cwd": {
                "type": "string",
                "description": "Workspace-relative working directory.",
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
        timeout = int(arguments.get("timeout", self.default_timeout))
        raw_args = arguments.get("args") or []
        if not isinstance(raw_args, list) or any(not isinstance(arg, str) for arg in raw_args):
            return ToolResult(call_id=call_id, tool_name=self.name, output="args must be a list of strings.", is_error=True)
        cwd = _resolve_cwd(context, str(arguments.get("cwd", "")).strip())
        if cwd is None:
            return ToolResult(call_id=call_id, tool_name=self.name, output="cwd is outside the workspace.", is_error=True)
        if not cwd.exists() or not cwd.is_dir():
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"cwd does not exist: {cwd}", is_error=True)
        command, validation_error = self._build_command(cwd, raw_args)
        if validation_error is not None:
            return ToolResult(call_id=call_id, tool_name=self.name, output=validation_error, is_error=True)
        result = await _run_command(command, cwd=cwd, timeout=timeout, max_output_chars=_max_output_chars(context))
        payload = {
            "tool": self.name,
            "command": list(command),
            "cwd": str(cwd.relative_to(context.working_directory.resolve())),
            "passed": result["exit_code"] == 0,
            "exit_code": result["exit_code"],
            "stdout_tail": _tail(result["stdout"]),
            "stderr_tail": _tail(result["stderr"]),
            "stdout_truncated": result["stdout_truncated"],
            "stderr_truncated": result["stderr_truncated"],
            "output_truncated": result["stdout_truncated"] or result["stderr_truncated"],
            "timed_out": result["timed_out"],
        }
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=json.dumps(payload, indent=2),
            is_error=payload["exit_code"] != 0 or payload["timed_out"],
            metadata=payload,
        )

    def _build_command(self, cwd: Path, raw_args: list[str]) -> tuple[tuple[str, ...], str | None]:
        del cwd
        return (*self.command, *raw_args), None


class RunTestsTool(_CommandTool):
    name = "run_tests"
    description = "Run the project test suite with structured pass/fail metadata. Defaults to `uv run pytest`."
    command = ("uv", "run", "pytest")
    default_timeout = 600


class RunPythonCheckTool(_CommandTool):
    name = "run_python_check"
    description = "Run a structured Python syntax check. Defaults to `python -m compileall -q` over discovered Python targets in the workspace."
    command = ("python", "-m", "compileall", "-q")
    default_timeout = 180

    def _build_command(self, cwd: Path, raw_args: list[str]) -> tuple[tuple[str, ...], str | None]:
        # Use the running interpreter so the tool works on systems where
        # "python" is not on PATH (e.g. Linux distros that only ship python3).
        cmd = (sys.executable, "-m", "compileall", "-q")
        return _compileall_command(cmd, cwd, raw_args)


class RunFormatterTool(_CommandTool):
    name = "run_formatter"
    description = "Run the formatter with structured metadata. Defaults to `ruff format .`."
    is_mutating = True
    command = ("ruff", "format", ".")
    default_timeout = 180

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        del call_id, context
        raw_args = arguments.get("args") or []
        command = " ".join((*self.command, *(str(arg) for arg in raw_args)))
        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=f"Run formatter command: {command}",
            command=command,
        )


async def _run_command(command: tuple[str, ...], *, cwd: Path, timeout: int, max_output_chars: int = 100 * 1024) -> dict[str, Any]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "stdout": "",
            "stderr": str(exc),
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
        }

    stdout_buffer = _BoundedByteBuffer(max_output_chars)
    stderr_buffer = _BoundedByteBuffer(max_output_chars)
    stdout_task = asyncio.create_task(_read_stream(process.stdout, stdout_buffer))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, stderr_buffer))
    try:
        await asyncio.wait_for(asyncio.gather(process.wait(), stdout_task, stderr_task), timeout=timeout)
        timed_out = False
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        timed_out = True

    return {
        "exit_code": process.returncode if process.returncode is not None else 124,
        "stdout": stdout_buffer.value.decode("utf-8", errors="replace"),
        "stderr": stderr_buffer.value.decode("utf-8", errors="replace"),
        "stdout_truncated": stdout_buffer.truncated,
        "stderr_truncated": stderr_buffer.truncated,
        "timed_out": timed_out,
    }


async def _read_stream(stream: asyncio.StreamReader | None, sink: "_BoundedByteBuffer") -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        sink.append(chunk)


class _BoundedByteBuffer:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(1, max_bytes)
        self._parts: list[bytes] = []
        self._size = 0
        self.truncated = False

    @property
    def value(self) -> bytes:
        return b"".join(self._parts)

    def append(self, chunk: bytes) -> None:
        if len(chunk) >= self.max_bytes:
            self._parts = [chunk[-self.max_bytes:]]
            self._size = self.max_bytes
            self.truncated = True
            return
        self._parts.append(chunk)
        self._size += len(chunk)
        while self._size > self.max_bytes and self._parts:
            overflow = self._size - self.max_bytes
            first = self._parts[0]
            if overflow >= len(first):
                self._parts.pop(0)
                self._size -= len(first)
                self.truncated = True
                continue
            self._parts[0] = first[overflow:]
            self._size -= overflow
            self.truncated = True
            break


def _max_output_chars(context: ToolExecutionContext) -> int:
    config = context.metadata.get("config")
    raw_value = getattr(config, "tool_output_max_chars", 100 * 1024)
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return 100 * 1024


def _resolve_cwd(context: ToolExecutionContext, raw_cwd: str) -> Path | None:
    workspace = context.working_directory.resolve()
    if not raw_cwd:
        return workspace
    path = resolve_path(workspace, raw_cwd)
    try:
        path.relative_to(workspace)
    except ValueError:
        return None
    return path


def _compileall_command(base_command: tuple[str, ...], cwd: Path, raw_args: list[str]) -> tuple[tuple[str, ...], str | None]:
    targets = _explicit_compile_targets(raw_args)
    if raw_args:
        outside = [target for target in targets if not _target_inside_cwd(cwd, target)]
        if outside:
            return (), "Verification target is outside the workspace: " + ", ".join(outside)
        missing = [target for target in targets if not (cwd / target).exists()]
        if missing:
            return (), "Verification target does not exist: " + ", ".join(missing)
        if not targets:
            return (), "Verification requires at least one existing Python target."
        return (*base_command, *raw_args), None

    discovered = _discover_python_targets(cwd)
    if not discovered:
        return (), "No Python files or packages found to verify in the workspace."
    return (*base_command, *discovered), None


def _explicit_compile_targets(args: list[str]) -> list[str]:
    targets: list[str] = []
    skip_next = False
    # -c takes a value in the Python CLI; include it so that `python -c <code>` patterns
    # don't leak the code string into the compile targets list.
    options_with_values = {"-c", "-d", "-s", "-p", "-j", "-x"}
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in options_with_values:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        targets.append(arg)
    return targets


def _target_inside_cwd(cwd: Path, target: str) -> bool:
    candidate = Path(target)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()
    except OSError:
        # Path construction can raise OSError (e.g. ENAMETOOLONG) for strings that
        # are not valid filesystem paths, such as inline Python code snippets.
        return False
    try:
        resolved.relative_to(cwd.resolve())
    except ValueError:
        return False
    return True


def _discover_python_targets(cwd: Path) -> list[str]:
    excluded = {
        ".git",
        ".hg",
        ".mypy_cache",
        ".nexus",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "reference_code",
        "venv",
    }
    targets: list[str] = []
    for entry in sorted(cwd.iterdir(), key=lambda path: path.name):
        if entry.name in excluded or entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix == ".py":
            targets.append(entry.name)
            continue
        if entry.is_dir() and _contains_python(entry, excluded):
            targets.append(entry.name)
    return targets


def _contains_python(root: Path, excluded: set[str]) -> bool:
    for path in root.rglob("*.py"):
        if any(part in excluded or part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        return True
    return False


def _tail(value: str, *, limit: int = 8000) -> str:
    value = value.rstrip()
    if len(value) <= limit:
        return value
    return value[-limit:]
