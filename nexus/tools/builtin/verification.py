"""Structured verification and formatter command tools."""
from __future__ import annotations

import asyncio
import json
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
        result = await _run_command(command, cwd=cwd, timeout=timeout)
        payload = {
            "tool": self.name,
            "command": list(command),
            "cwd": str(cwd.relative_to(context.working_directory.resolve())),
            "passed": result["exit_code"] == 0,
            "exit_code": result["exit_code"],
            "stdout_tail": _tail(result["stdout"]),
            "stderr_tail": _tail(result["stderr"]),
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


class RunLinterTool(_CommandTool):
    name = "run_linter"
    description = "Run a structured lint check. Defaults to `python -m compileall -q` over discovered Python targets in the workspace."
    command = ("python", "-m", "compileall", "-q")
    default_timeout = 180

    def _build_command(self, cwd: Path, raw_args: list[str]) -> tuple[tuple[str, ...], str | None]:
        return _compileall_command(self.command, cwd, raw_args)


class RunTypecheckTool(_CommandTool):
    name = "run_typecheck"
    description = "Run a structured type/syntax check. Defaults to `python -m compileall -q` over discovered Python targets in the workspace."
    command = ("python", "-m", "compileall", "-q")
    default_timeout = 180

    def _build_command(self, cwd: Path, raw_args: list[str]) -> tuple[tuple[str, ...], str | None]:
        return _compileall_command(self.command, cwd, raw_args)


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


async def _run_command(command: tuple[str, ...], *, cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        return {"exit_code": 127, "stdout": "", "stderr": str(exc), "timed_out": False}

    try:
        stdout_data, stderr_data = await asyncio.wait_for(process.communicate(), timeout=timeout)
        timed_out = False
    except asyncio.TimeoutError:
        process.kill()
        stdout_data, stderr_data = await process.communicate()
        timed_out = True

    return {
        "exit_code": process.returncode if process.returncode is not None else 124,
        "stdout": stdout_data.decode("utf-8", errors="replace"),
        "stderr": stderr_data.decode("utf-8", errors="replace"),
        "timed_out": timed_out,
    }


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
    options_with_values = {"-d", "-s", "-p", "-j", "-x"}
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
    resolved = candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()
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
