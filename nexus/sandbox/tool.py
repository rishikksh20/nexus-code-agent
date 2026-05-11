"""Sandboxed command tool — the tool-interface layer for Docker execution.

:class:`SandboxedCommandTool` exposes Docker sandbox execution as a first-class
Nexus tool, satisfying :class:`~nexus.tools.base.BaseTool` with
``kind = ToolKind.SANDBOX``.

This is the only public surface the agent and tool registry ever see; the
concrete :class:`~nexus.sandbox.docker.DockerSandbox` is an implementation
detail injected at construction time.
"""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.sandbox.docker import DockerSandbox
from nexus.tools.base import ToolKind


class SandboxedCommandTool:
    """Execute a shell command inside a Docker sandbox.

    The agent calls this tool exactly like any other tool.  All Docker
    orchestration is delegated to the injected :class:`~nexus.sandbox.docker.DockerSandbox`.

    **Security guarantees** (enforced by :class:`~nexus.sandbox.docker.DockerSandbox`):

    * Network access disabled by default.
    * Workspace mounted read-only; writes are contained inside the ephemeral
      container layer and discarded on exit.
    * Dropped Linux capabilities and ``no-new-privileges``.
    * Memory and process-count limits.

    Parameters
    ----------
    sandbox:
        A fully configured :class:`~nexus.sandbox.docker.DockerSandbox`.
        Construct one via :func:`~nexus.sandbox.factory.build_sandbox` or
        pass a custom instance for testing.
    """

    name = "run_command"
    kind = ToolKind.SANDBOX
    is_mutating = True
    description = (
        "Execute a shell command inside a Docker sandbox. "
        "The workspace is mounted read-only at /workspace; network is disabled "
        "by default. Writes are contained within the ephemeral container layer."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "description": "Shell command to run inside the sandbox container.",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(self, sandbox: DockerSandbox) -> None:
        self._sandbox = sandbox

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        command = str(arguments.get("command", "")).strip()
        if not command:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output="Missing required argument: command",
                is_error=True,
                metadata={"source": "sandbox", "image": self._sandbox.config.image},
            )
        result = await self._sandbox.run(command, cwd=context.working_directory)
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=result.output,
            is_error=result.is_error,
            metadata=result.metadata,
        )
