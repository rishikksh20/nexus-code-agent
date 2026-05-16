"""Cognitive sub-agent tools.

``SubAgentTool`` is itself a normal tool from the supervisor model's point of
view. Invoking the cognitive tool is not mutating. Any normal tool the
sub-agent calls inside its own agent loop keeps the usual permission behavior.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from nexus.models import AgentEventType, ConfirmationKind, Message, ToolCall, ToolExecutionContext, ToolResult
from nexus.runtime.execution import ExecutionMode
from nexus.security.manager import ApprovalScope
from nexus.security.policy import ApprovalPolicy
from nexus.tools.base import ToolKind

# ---------------------------------------------------------------------------
# SubagentDefinition — pre-configured cognitive persona
# ---------------------------------------------------------------------------

@dataclass
class SubagentDefinition:
    """Pre-configure a named cognitive sub-agent persona.

    When a :class:`SubAgentTool` is initialised with a
    ``SubagentDefinition``, the tool name and description are derived from the
    definition so the LLM can pick the right specialist.

    Parameters
    ----------
    name:
        Short identifier used as part of the tool name
        (``subagent_<name>``).
    description:
        One-sentence description shown to the LLM in the tool schema.
    goal_prompt:
        System prompt / goal injected into the cognitive sub-agent.
    allowed_tools:
        Restrict which normal tools the sub-agent may use. ``None`` = all.
    max_turns:
        Maximum number of agent turns for the sub-agent.
    timeout_seconds:
        Hard wall-clock timeout for the sub-agent in seconds.
    """

    name: str
    description: str
    goal_prompt: str
    allowed_tools: list[str] | None = None
    max_turns: int = 20
    timeout_seconds: float = 600.0


class SubAgentTool:
    """Run a focused cognitive sub-agent and return a structured result."""

    # Default class-level attrs — overridden per-instance when a definition is given
    name = "subagent"
    kind = ToolKind.AGENT
    is_mutating = False
    description = (
        "Run a focused cognitive sub-agent and return a structured result. "
        "The cognitive tool itself is non-mutating; normal tools it calls keep "
        "their normal permission behavior."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "Short human-readable title for the cognitive task. Use role/task language, not the full user request.",
            },
            "instructions": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Detailed bounded objective for the cognitive sub-agent. Include role, goal, constraints, "
                    "relevant file hints, expected JSON fields, and stop condition. Do not paste the full conversation."
                ),
            },
            "allowed_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional supervisor override for normal tool names the sub-agent may use. Omit to use the specialist's default allowlist.",
            },
            "input_packet_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional handoff packet ids that define the only shared context inputs for the sub-agent.",
            },
        },
        "required": ["title", "instructions"],
        "additionalProperties": False,
    }

    _MAX_OUTPUT_BYTES: int = 8_192

    def __init__(
        self,
        definition: SubagentDefinition | None = None,
        *,
        model_client_factory=None,
        base_tool_registry=None,
        config=None,
        delegation=None,
    ) -> None:
        del delegation
        self._definition = definition
        self._model_client_factory = model_client_factory
        self._base_tool_registry = base_tool_registry
        self._config = config
        if definition is not None:
            # Override the instance tool name/description from the definition
            self.name = f"subagent_{definition.name}"
            self.description = definition.description

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        title = str(arguments.get("title", "")).strip()
        instructions = str(arguments.get("instructions", "")).strip()
        raw_tools: list[str] | None = arguments.get("allowed_tools")
        raw_packet_ids: list[str] | None = arguments.get("input_packet_ids")
        input_packet_ids = tuple(str(item) for item in raw_packet_ids or () if str(item).strip())

        if not title or not instructions:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output="Both 'title' and 'instructions' are required.",
                is_error=True,
            )

        # Merge definition-level allowed_tools if caller didn't override
        if raw_tools is None and self._definition and self._definition.allowed_tools:
            raw_tools = self._definition.allowed_tools

        # Prepend definition goal_prompt if available
        if self._definition and self._definition.goal_prompt:
            instructions = f"{self._definition.goal_prompt}\n\n{instructions}"

        return await self._execute_direct(
            call_id,
            title=title,
            instructions=instructions,
            allowed_tools=tuple(raw_tools) if raw_tools else (),
            input_packet_ids=input_packet_ids,
            outer_context=context,
        )

    async def _execute_direct(
        self,
        call_id: str,
        *,
        title: str,
        instructions: str,
        allowed_tools: tuple[str, ...],
        input_packet_ids: tuple[str, ...],
        outer_context: ToolExecutionContext,
    ) -> ToolResult:
        from nexus.integrations.fake_model import FakeModelClient
        from nexus.runtime.agent import Agent
        from nexus.tools.base import ToolRegistry

        task_id = call_id
        if self._base_tool_registry is None:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output="Sub-agent tool is not attached to a tool registry.",
                is_error=True,
            )

        registry = ToolRegistry()
        allowed = set(allowed_tools)
        for record in self._base_tool_registry.records():
            if record.name == self.name or record.name.startswith("subagent_") or record.name == "delegate_task":
                continue
            if allowed and record.name not in allowed:
                continue
            registry.register(record.tool, source=record.source, origin=record.origin)

        model_factory = self._model_client_factory or (lambda: FakeModelClient())
        agent = Agent(model_client=model_factory(), tool_registry=registry)
        shared_context = _packet_summaries_from_context(outer_context, input_packet_ids)
        sub_context = ToolExecutionContext(
            session_id=f"{outer_context.session_id}-subagent-{call_id}",
            working_directory=outer_context.working_directory,
            user_id=outer_context.user_id,
            metadata={
                **outer_context.metadata,
                "context_scope": "isolated",
                "parent_session_id": outer_context.session_id,
                "subagent": self.name,
                "tool_display_prefix": self.name,
                "supervisor_cognitive_tools_only": False,
                "input_packet_ids": list(input_packet_ids),
                "shared_context": list(shared_context),
            },
        )
        system_prompt = _direct_subagent_system_prompt(
            self._definition,
            title=title,
            instructions=instructions,
            allowed_tools=tuple(record.name for record in registry.records()),
            shared_context=shared_context,
            input_packet_ids=input_packet_ids,
        )
        history = [Message(role="user", content=instructions)]
        final_response = ""
        tool_call_count = 0
        status = "completed"
        error = None

        for _ in range(int(getattr(self._definition, "max_turns", 20) or 20)):
            events = [
                event
                async for event in agent.run(
                    history,
                    sub_context,
                    system_prompt=system_prompt,
                    model_name=str(getattr(self._config, "model_name", "fake-model")),
                    mode=ExecutionMode(str(outer_context.metadata.get("execution_mode", "default"))),
                    approval_manager=outer_context.metadata.get("approval_manager"),
                    auto_confirm=bool(outer_context.metadata.get("auto_confirm", False)),
                    auto_confirm_read_only=bool(outer_context.metadata.get("auto_confirm_read_only", True)),
                    temperature=float(getattr(self._config, "temperature", 0.0)),
                    max_output_tokens=getattr(self._config, "max_output_tokens", None),
                    max_turns=1,
                )
            ]
            _render_inner_events(outer_context, events)

            confirmation = next((event for event in events if event.kind == AgentEventType.CONFIRMATION_REQUESTED), None)
            if confirmation is not None:
                pending_message = None
                for event in events:
                    if event.kind == AgentEventType.MODEL_RESPONSE:
                        pending_message = event.payload.message
                        if pending_message.content:
                            final_response = pending_message.content
                decision = await _handle_inner_confirmation(confirmation.payload, outer_context)
                if decision == "approved":
                    if pending_message is not None:
                        history.append(pending_message)
                    resume_call = ToolCall(
                        call_id=confirmation.payload.call_id,
                        tool_name=confirmation.payload.tool_name,
                        arguments=confirmation.payload.arguments,
                    )
                    resume_events = [
                        event
                        async for event in agent.run(
                            history,
                            sub_context,
                            system_prompt=system_prompt,
                            model_name=str(getattr(self._config, "model_name", "fake-model")),
                            mode=ExecutionMode(str(outer_context.metadata.get("execution_mode", "default"))),
                            approval_manager=outer_context.metadata.get("approval_manager"),
                            auto_confirm=bool(outer_context.metadata.get("auto_confirm", False)),
                            auto_confirm_read_only=bool(outer_context.metadata.get("auto_confirm_read_only", True)),
                            temperature=float(getattr(self._config, "temperature", 0.0)),
                            max_output_tokens=getattr(self._config, "max_output_tokens", None),
                            max_turns=1,
                            resume_tool_calls=(resume_call,),
                        )
                    ]
                    _render_inner_events(outer_context, resume_events)
                    for event in resume_events:
                        if event.kind == AgentEventType.TOOL_RESULT:
                            tool_call_count += 1
                            history.append(
                                Message(
                                    role="tool",
                                    content=event.payload.output,
                                    name=event.payload.tool_name,
                                    tool_call_id=event.payload.call_id,
                                )
                            )
                            final_response = event.payload.output
                        elif event.kind == AgentEventType.AGENT_ERROR:
                            status = "failed"
                            error = str(event.payload)
                    continue
                if decision == "denied":
                    continue
                status = "needs_approval" if confirmation.payload.kind is ConfirmationKind.APPROVAL else "needs_clarification"
                final_response = confirmation.payload.prompt
                break

            for event in events:
                if event.kind == AgentEventType.MODEL_RESPONSE:
                    message = event.payload.message
                    history.append(message)
                    if message.content:
                        final_response = message.content
                elif event.kind == AgentEventType.TOOL_RESULT:
                    tool_call_count += 1
                    history.append(
                        Message(
                            role="tool",
                            content=event.payload.output,
                            name=event.payload.tool_name,
                            tool_call_id=event.payload.call_id,
                        )
                    )
                    final_response = event.payload.output
                elif event.kind == AgentEventType.AGENT_ERROR:
                    status = "failed"
                    error = str(event.payload)
            if not any(event.kind == AgentEventType.TOOL_RESULT for event in events):
                break

        context_snapshot = {
            "scope": "isolated",
            "input_packet_ids": list(input_packet_ids),
            "shared_context": list(shared_context),
            "allowed_tools": [record.name for record in registry.records()],
            "tool_call_count": tool_call_count,
            "message_count": len(history),
            "token_estimate": 0,
            "isolation": {
                "local_history_shared": False,
                "shared_outputs": "final_summary_and_context_snapshot_only",
            },
        }
        is_failed = status in {"failed", "needs_approval"}
        raw_output = final_response or error or "(no output)"
        output = _subagent_result_envelope(
            tool_name=self.name,
            definition=self._definition,
            task_id=task_id,
            title=title,
            status=status,
            is_error=is_failed,
            raw_result=raw_output,
            input_packet_ids=input_packet_ids,
            context_snapshot=context_snapshot,
        )
        if len(output) > self._MAX_OUTPUT_BYTES:
            output = output[: self._MAX_OUTPUT_BYTES] + "\n\u2026[truncated]"

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            is_error=is_failed,
            metadata={
                "task_id": task_id,
                "title": title,
                "status": status,
                "agent": self.name,
                "role": self._definition.name if self._definition else "delegate",
                "input_packet_ids": list(input_packet_ids),
                "context_snapshot": context_snapshot,
            },
        )


def _packet_summaries_from_context(
    context: ToolExecutionContext,
    packet_ids: tuple[str, ...],
) -> tuple[str, ...]:
    summaries = context.metadata.get("multi_agent_packet_summaries", {})
    if not isinstance(summaries, dict):
        return ()
    rendered: list[str] = []
    for packet_id in packet_ids:
        summary = summaries.get(packet_id)
        if isinstance(summary, str) and summary.strip():
            rendered.append(summary)
    return tuple(rendered)


def _render_inner_events(outer_context: ToolExecutionContext, events) -> None:
    ui = outer_context.metadata.get("ui")
    if ui is None:
        return
    render_event = getattr(ui, "render_event", None)
    if render_event is None:
        return
    visible_kinds = {
        AgentEventType.THINKING_STARTED,
        AgentEventType.TOOL_CALL_START,
        AgentEventType.TOOL_CALL_COMPLETE,
        AgentEventType.TOOL_DENIED,
        AgentEventType.CONFIRMATION_REQUESTED,
        AgentEventType.AGENT_ERROR,
    }
    for event in events:
        if event.kind not in visible_kinds:
            continue
        render_event(
            event,
            stream_output=bool(outer_context.metadata.get("stream_output", True)),
            show_tool_calls=bool(outer_context.metadata.get("show_tool_calls", True)),
            show_thinking_indicator=True,
        )


def _direct_subagent_system_prompt(
    definition: SubagentDefinition | None,
    *,
    title: str,
    instructions: str,
    allowed_tools: tuple[str, ...],
    shared_context: tuple[str, ...],
    input_packet_ids: tuple[str, ...],
) -> str:
    tools_text = ", ".join(allowed_tools) if allowed_tools else "no tools"
    shared_text = "\n".join(shared_context) if shared_context else "(none)"
    packet_text = ", ".join(input_packet_ids) if input_packet_ids else "(none)"
    role_prompt = definition.goal_prompt if definition is not None else "Complete the delegated cognitive task."
    return (
        "You are a Nexus cognitive sub-agent running inside a tool call.\n"
        "Your local reasoning, messages, and tool history are isolated from the supervisor.\n"
        "Only your final JSON result and compact context snapshot are returned.\n"
        "Do not ask the user directly; put missing information in `clarifications_needed`.\n"
        "Calling this cognitive tool is not mutating, but every normal tool you call keeps its normal permission behavior.\n\n"
        f"Role instructions:\n{role_prompt}\n\n"
        f"Task title: {title}\n"
        f"Task instructions:\n{instructions}\n\n"
        f"Input packet ids: {packet_text}\n"
        f"Shared handoff context:\n{shared_text}\n\n"
        f"Allowed normal tools: {tools_text}\n\n"
        "Return only a JSON object with keys: status, summary, findings, changed_files, "
        "related_files, tests_run, risks, clarifications_needed, recommended_next_action."
    )


async def _handle_inner_confirmation(request, outer_context: ToolExecutionContext) -> str:
    callback = outer_context.metadata.get("approval_callback")
    if callback is None:
        return ""
    response = await callback(request)
    approval_manager = outer_context.metadata.get("approval_manager")
    if request.kind is ConfirmationKind.APPROVAL:
        if response.approved:
            if approval_manager is not None:
                _record_inner_approval(approval_manager, request, response)
            return "approved"
        if response.denied:
            if approval_manager is not None:
                approval_manager.record_refusal(
                    request.tool_name,
                    arguments=request.arguments,
                )
            return "denied"
    return ""


def _record_inner_approval(approval_manager, request, response) -> None:
    scope = _approval_scope_from_response(request, response)
    request_policy = ApprovalPolicy(str(request.payload.get("approval_policy", ApprovalPolicy.ON_REQUEST.value)))
    if scope is ApprovalScope.TURN and request_policy is ApprovalPolicy.ON_REQUEST:
        if _supports_turn_wide_approval(request):
            approval_manager.record_turn_wide_mutating_approval()
            return
        approval_manager.record_approval(
            request.tool_name,
            ApprovalScope.ONCE,
            arguments=request.arguments,
        )
        return
    approval_manager.record_approval(
        request.tool_name,
        scope,
        arguments=request.arguments,
    )


def _approval_scope_from_response(request, response) -> ApprovalScope:
    raw_scope = response.scope
    if not raw_scope:
        request_policy = ApprovalPolicy(str(request.payload.get("approval_policy", ApprovalPolicy.ON_REQUEST.value)))
        if request_policy is ApprovalPolicy.APPROVE_TURN:
            return ApprovalScope.TURN
        if request_policy is ApprovalPolicy.APPROVE_SESSION:
            return ApprovalScope.SESSION
        return ApprovalScope.ONCE
    return ApprovalScope(str(raw_scope))


def _supports_turn_wide_approval(request) -> bool:
    risk_level = str(request.payload.get("risk_level", "medium")).strip().lower().split(".")[-1]
    return not (request.tool_name == "bash" and risk_level in {"high", "dangerous"})


def _subagent_result_envelope(
    *,
    tool_name: str,
    definition: SubagentDefinition | None,
    task_id: str,
    title: str,
    status: str,
    is_error: bool,
    raw_result: str,
    input_packet_ids: tuple[str, ...],
    context_snapshot: dict[str, Any],
) -> str:
    normalized_status = _infer_result_status(raw_result, is_error=is_error)
    context = {
        "scope": "isolated",
        "input_packet_ids": list(input_packet_ids),
        "shared_context": context_snapshot.get("shared_context", []),
        "allowed_tools": context_snapshot.get("allowed_tools", []),
        "tool_call_count": context_snapshot.get("tool_call_count", 0),
        "message_count": context_snapshot.get("message_count", 0),
        "token_estimate": context_snapshot.get("token_estimate", 0),
    }
    payload = {
        "schema_version": 1,
        "status": normalized_status,
        "agent": tool_name,
        "role": definition.name if definition else "delegate",
        "task_id": task_id,
        "title": title,
        "summary": _summary_line(raw_result),
        "raw_result": raw_result,
        "clarifications_needed": _clarifications_from_result(raw_result),
        "changed_files": _list_field_from_snapshot(context_snapshot, "modified_files"),
        "related_files": _list_field_from_snapshot(context_snapshot, "related_files"),
        "tests_run": _list_field_from_snapshot(context_snapshot, "tests_run"),
        "context": context,
        "recommended_next_action": "ask_user" if normalized_status == "needs_clarification" else "continue",
        "runtime_status": status,
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _infer_result_status(raw_result: str, *, is_error: bool) -> str:
    lowered = raw_result.lower()
    if "requires clarification" in lowered or "needs clarification" in lowered:
        return "needs_clarification"
    if is_error:
        return "failed"
    return "completed"


def _summary_line(raw_result: str) -> str:
    for line in raw_result.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:500]
    return "(no output)"


def _clarifications_from_result(raw_result: str) -> list[str]:
    lowered = raw_result.lower()
    if "requires clarification" not in lowered and "needs clarification" not in lowered:
        return []
    return [raw_result.strip()[:500]]


def _list_field_from_snapshot(snapshot: dict[str, Any], key: str) -> list[str]:
    value = snapshot.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
