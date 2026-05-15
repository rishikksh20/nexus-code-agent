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
        command = (*self.command, *raw_args)
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


class RunTestsTool(_CommandTool):
    name = "run_tests"
    description = "Run the project test suite with structured pass/fail metadata. Defaults to `uv run pytest`."
    command = ("uv", "run", "pytest")
    default_timeout = 600


class RunLinterTool(_CommandTool):
    name = "run_linter"
    description = "Run a structured lint check. Defaults to `python -m compileall -q nexus tests` for dependency-free lint-like validation."
    command = ("python", "-m", "compileall", "-q", "nexus", "tests")
    default_timeout = 180


class RunTypecheckTool(_CommandTool):
    name = "run_typecheck"
    description = "Run a structured type/syntax check. Defaults to `python -m compileall -q nexus tests`."
    command = ("python", "-m", "compileall", "-q", "nexus", "tests")
    default_timeout = 180


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


def _tail(value: str, *, limit: int = 8000) -> str:
    value = value.rstrip()
    if len(value) <= limit:
        return value
    return value[-limit:]
