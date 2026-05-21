"""Sandboxed command execution compatibility surface."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolConfirmation, ToolKind


@dataclass(slots=True)
class SandboxConfig:
    image: str = "python:3.12-slim"
    timeout_seconds: float = 30.0
    network_disabled: bool = True


class DockerSandbox:
    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()

    async def run(self, command: str, *, workspace: Path) -> tuple[int, str, str]:
        docker_command = [
            "docker",
            "run",
            "--rm",
            "--workdir",
            "/workspace",
            "--volume",
            f"{workspace.resolve()}:/workspace",
            "--cap-drop",
            "ALL",
        ]
        if self.config.network_disabled:
            docker_command.append("--network=none")
        docker_command.extend([self.config.image, "/bin/bash", "-lc", command])
        process = await asyncio.create_subprocess_exec(
            *docker_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                process.communicate(),
                timeout=self.config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout_data, stderr_data = await process.communicate()
            return 124, stdout_data.decode("utf-8", errors="replace"), stderr_data.decode("utf-8", errors="replace")
        return (
            process.returncode if process.returncode is not None else 1,
            stdout_data.decode("utf-8", errors="replace"),
            stderr_data.decode("utf-8", errors="replace"),
        )


class SandboxedCommandTool(Tool):
    name = "sandboxed_command"
    description = "Run a shell command inside a Docker sandbox."
    kind = ToolKind.SHELL
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(self, sandbox: DockerSandbox | None = None) -> None:
        self.sandbox = sandbox or DockerSandbox()

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        del call_id, context
        command = str(arguments.get("command", "")).strip()
        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=f"Run sandboxed command: {command}",
            command=command,
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
        exit_code, stdout, stderr = await self.sandbox.run(command, workspace=context.working_directory)
        output = stdout.rstrip()
        if stderr.strip():
            output = f"{output}\n--- stderr ---\n{stderr.rstrip()}" if output else stderr.rstrip()
        if exit_code != 0:
            output = f"{output}\nExit code: {exit_code}" if output else f"Exit code: {exit_code}"
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            is_error=exit_code != 0,
            metadata={"exit_code": exit_code},
        )


__all__ = ["DockerSandbox", "SandboxConfig", "SandboxedCommandTool"]
