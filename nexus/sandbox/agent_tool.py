"""SubAgentTool — lets the LLM delegate a sub-task to a worker agent.

:class:`SubAgentTool` wraps :class:`~nexus.runtime.delegation.DelegationRuntime`
behind the standard :class:`~nexus.tools.base.BaseTool` interface so the main
agent can spawn and await a worker via the tool-call mechanism rather than a
slash command.

Only registered when delegation is enabled (checked by
:func:`~nexus.sandbox.factory.register_agent_tool`).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import ToolKind

if TYPE_CHECKING:
    from nexus.runtime.delegation import DelegationRuntime


class SubAgentTool:
    """Submit a task to a worker agent and wait for the result.

    The tool call blocks until the worker finishes (or the delegation runtime
    reports a terminal status).  Large responses are truncated to 8 KB so they
    don't pollute the main context.

    Parameters
    ----------
    delegation:
        A running :class:`~nexus.runtime.delegation.DelegationRuntime`
        instance.  Must be alive for the duration of the agent session.
    """

    name = "delegate_task"
    kind = ToolKind.AGENT
    is_mutating = True
    description = (
        "Spawn a worker agent to handle a focused sub-task and return its result. "
        "Use for long-running, self-contained sub-problems that can run "
        "independently.  The call blocks until the worker finishes."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "Short human-readable title for the delegated task.",
            },
            "instructions": {
                "type": "string",
                "minLength": 1,
                "description": "Detailed instructions for the worker agent.",
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of tool names the worker may use.",
            },
        },
        "required": ["title", "instructions"],
        "additionalProperties": False,
    }

    _MAX_OUTPUT_BYTES: int = 8_192

    def __init__(self, delegation: "DelegationRuntime") -> None:
        self._delegation = delegation

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        from nexus.runtime.delegation import DelegationRequest, TaskStatus

        title = str(arguments.get("title", "")).strip()
        instructions = str(arguments.get("instructions", "")).strip()
        raw_tools: list[str] | None = arguments.get("allowed_tools")

        if not title or not instructions:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output="Both 'title' and 'instructions' are required.",
                is_error=True,
            )

        request = DelegationRequest(
            title=title,
            instructions=instructions,
            allowed_tools=tuple(raw_tools) if raw_tools else (),
        )
        task_record = await self._delegation.submit(request)
        record = await self._delegation.wait_for_task(task_record.task_id)

        if record is None:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output="Task record not found after submission.",
                is_error=True,
                metadata={"task_id": task_record.task_id, "title": title},
            )

        is_failed = record.status is TaskStatus.FAILED
        output = record.result_summary or record.error or "(no output)"
        if len(output) > self._MAX_OUTPUT_BYTES:
            output = output[: self._MAX_OUTPUT_BYTES] + "\n\u2026[truncated]"

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            is_error=is_failed,
            metadata={
                "task_id": record.task_id,
                "title": title,
                "status": record.status.value,
            },
        )
