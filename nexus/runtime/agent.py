from __future__ import annotations

import time

from collections.abc import AsyncGenerator
from typing import Any, Protocol, cast, runtime_checkable

from nexus.models import (
    AgentEvent,
    AgentEventType,
    ConfirmationKind,
    ConfirmationRequest,
    Message,
    RuntimeRequest,
    RuntimeResponse,
    StreamEvent,
    StreamEventType,
    ToolCall,
    ToolExecutionContext,
    ToolResult,
    UsageSnapshot,
)
from nexus.runtime.execution import ExecutionMode
from nexus.hooks import HookEvent, HookExecutor
from nexus.security import PermissionChecker, PermissionDecision
from nexus.context import LoopDetector, prune_tool_outputs
from nexus.security.manager import ApprovalManager
from nexus.tools.base import FileDiff, ToolConfirmation, ToolRegistry


TOOL_CALL_LIMIT_FINISH_REASON = "tool_call_limit"


@runtime_checkable
class ModelClient(Protocol):
    """Protocol that every model client must satisfy.

    The primary interface is :meth:`chat_completion`, which yields
    :class:`~nexus.models.StreamEvent` objects for both streaming and
    non-streaming provider calls.  The legacy :meth:`complete` method is
    retained for backward compatibility with existing tests.
    """

    def chat_completion(
        self,
        request: RuntimeRequest,
        *,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        ...


class Agent:
    def __init__(
        self,
        model_client: ModelClient,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker | None = None,
        hooks: HookExecutor | None = None,
    ) -> None:
        self.model_client = model_client
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker or PermissionChecker()
        self.hooks = hooks or HookExecutor()

    async def run(
        self,
        messages: list[Message],
        context: ToolExecutionContext,
        *,
        system_prompt: str = "You are Nexus, a CLI-first agent.",
        model_name: str = "fake-model",
        mode: ExecutionMode = ExecutionMode.DEFAULT,
        approved_tools: set[str] | None = None,
        approval_manager: ApprovalManager | None = None,
        auto_confirm: bool = False,
        auto_confirm_read_only: bool = True,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        max_turns: int = 3,
        max_tool_calls_per_turn: int = 30,
        resume_tool_calls: tuple[ToolCall, ...] = (),
    ) -> AsyncGenerator[AgentEvent, None]:
        """Run the agentic loop, yielding :class:`AgentEvent` objects.

        Emits both the new reference-style events (``TEXT_DELTA``,
        ``TEXT_COMPLETE``, ``TOOL_CALL_START``, ``TOOL_CALL_COMPLETE``,
        ``AGENT_START``, ``AGENT_STOP``) and the legacy Nexus events
        (``model_response``, ``tool_call_requested``, ``tool_result``,
        ``turn_completed``) so existing consumers continue to work unchanged.
        """
        history = list(messages)
        approved = approved_tools or set()
        final_response: str | None = None

        if resume_tool_calls:
            async for event in self._execute_approved_tool_calls(
                resume_tool_calls,
                context,
                approval_manager=approval_manager,
            ):
                yield event
            return

        yield AgentEvent(kind=AgentEventType.THINKING_STARTED)
        yield AgentEvent.agent_start(messages[-1].content if messages else "")

        async for event in self._agentic_loop(
            history,
            context,
            system_prompt=system_prompt,
            model_name=model_name,
            mode=mode,
            approved_tools=approved,
            approval_manager=approval_manager,
            auto_confirm=auto_confirm,
            auto_confirm_read_only=auto_confirm_read_only,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            max_turns=max_turns,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
        ):
            if event.kind == AgentEventType.TEXT_COMPLETE:
                final_response = event.payload
            yield event

        yield AgentEvent.agent_stop(response=final_response)

    async def _execute_approved_tool_calls(
        self,
        tool_calls: tuple[ToolCall, ...],
        context: ToolExecutionContext,
        *,
        approval_manager: ApprovalManager | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        for tool_call in tool_calls:
            record = self.tool_registry.record(tool_call.tool_name)
            tool = record.tool
            confirmation = await _get_tool_confirmation(tool, tool_call.call_id, tool_call.arguments, context)
            confirmation_preview = _confirmation_preview(confirmation)
            async for event in self._execute_tool_call(
                record,
                tool,
                tool_call,
                context,
                approval_manager=approval_manager,
                confirmation_preview=confirmation_preview,
            ):
                yield event

    async def _execute_tool_call(
        self,
        record: Any,
        tool: Any,
        tool_call: ToolCall,
        context: ToolExecutionContext,
        *,
        approval_manager: ApprovalManager | None,
        confirmation_preview: dict[str, Any],
    ) -> AsyncGenerator[AgentEvent, None]:
        yield AgentEvent.tool_call_start(
            tool_call.call_id,
            tool_call.tool_name,
            tool_call.arguments,
            preview=confirmation_preview,
        )
        yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)

        await self.hooks.emit(
            HookEvent.PRE_TOOL_USE,
            {
                "tool_name": tool.name,
                "tool_source": record.source,
                "tool_origin": record.origin,
                "arguments": tool_call.arguments,
                "call_id": tool_call.call_id,
                "session_id": context.session_id,
                "is_mutating": tool.is_mutating,
                **_correlation_payload(context, tool_call_id=tool_call.call_id),
            },
        )

        started_at = time.perf_counter()
        result = await tool.execute(tool_call.call_id, tool_call.arguments, context)
        if approval_manager is not None:
            approval_manager.consume_approval(tool.name, arguments=tool_call.arguments)

        await self.hooks.emit(
            HookEvent.POST_TOOL_USE,
            {
                "tool_name": tool.name,
                "tool_source": record.source,
                "tool_origin": record.origin,
                "arguments": tool_call.arguments,
                "call_id": tool_call.call_id,
                "session_id": context.session_id,
                "is_mutating": tool.is_mutating,
                "is_error": result.is_error,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "output": result.output,
                **_correlation_payload(context, tool_call_id=tool_call.call_id),
            },
        )

        yield AgentEvent.tool_call_complete(result)
        yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)

    async def _agentic_loop(
        self,
        history: list[Message],
        context: ToolExecutionContext,
        *,
        system_prompt: str,
        model_name: str,
        mode: ExecutionMode,
        approved_tools: set[str],
        approval_manager: ApprovalManager | None,
        auto_confirm: bool,
        auto_confirm_read_only: bool,
        temperature: float,
        max_output_tokens: int | None,
        max_turns: int,
        max_tool_calls_per_turn: int,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Core agentic loop — processes stream events and executes tools."""

        tool_calls_executed = 0
        loop_detector = LoopDetector()

        for _ in range(max_turns):
            request = RuntimeRequest(
                model_name=model_name,
                system_prompt=system_prompt,
                messages=tuple(history),
                tool_schemas=self.tool_registry.schemas(),
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )

            # ----------------------------------------------------------------
            # Stream from the model client
            # ----------------------------------------------------------------
            response_text = ""
            stream_tool_calls: list[ToolCall] = []
            usage: UsageSnapshot | None = None

            stream = cast(AsyncGenerator[StreamEvent, None], self.model_client.chat_completion(request, stream=True))
            async for stream_event in stream:
                if stream_event.type == StreamEventType.TEXT_DELTA:
                    if stream_event.text_delta and stream_event.text_delta.content:
                        chunk = stream_event.text_delta.content
                        response_text += chunk
                        yield AgentEvent.text_delta(chunk)

                elif stream_event.type == StreamEventType.TOOL_CALL_COMPLETE:
                    if stream_event.tool_call:
                        stream_tool_calls.append(stream_event.tool_call)

                elif stream_event.type == StreamEventType.MESSAGE_COMPLETE:
                    usage = stream_event.usage

                elif stream_event.type == StreamEventType.ERROR:
                    yield AgentEvent.agent_error(stream_event.error)
                    return

            if response_text:
                yield AgentEvent.text_complete(response_text)
                loop_detector.record_action("response", text=response_text[:200])

            # Build the assistant message and RuntimeResponse for history.
            tool_calls: tuple[ToolCall, ...] = tuple(stream_tool_calls)
            message = Message(
                role="assistant",
                content=response_text or "",
                tool_calls=tool_calls,
            )
            should_record_message = bool(message.content or message.tool_calls)
            if should_record_message:
                history.append(message)

            runtime_response = RuntimeResponse(
                message=message,
                tool_calls=tool_calls,
                usage=usage,
                finish_reason="tool_calls" if tool_calls else "stop",
            )

            if usage is not None:
                await self.hooks.emit(
                    HookEvent.NOTIFICATION,
                    {
                        "event": "model_usage",
                        "session_id": context.session_id,
                        **_correlation_payload(context),
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                        "estimated_cost_usd": usage.estimated_cost_usd,
                        "provider": usage.provider,
                        "model": usage.model,
                    },
                )

            # Emit the legacy MODEL_RESPONSE event for backward-compatible consumers
            # (history management in apply_events_to_history, etc.).
            if should_record_message:
                yield AgentEvent(kind=AgentEventType.MODEL_RESPONSE, payload=runtime_response)

            if not tool_calls:
                yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload=runtime_response.finish_reason)
                return

            # ----------------------------------------------------------------
            # Tool execution
            # ----------------------------------------------------------------
            tool_result_messages: list[Message] = []
            for tool_call in tool_calls:
                record = self.tool_registry.record(tool_call.tool_name)
                tool = record.tool
                confirmation = await _get_tool_confirmation(tool, tool_call.call_id, tool_call.arguments, context)
                confirmation_preview = _confirmation_preview(confirmation)

                if tool_calls_executed >= max_tool_calls_per_turn:
                    pause_message = _tool_call_limit_pause_message(max_tool_calls_per_turn)
                    yield AgentEvent.text_complete(pause_message)
                    yield AgentEvent(
                        kind=AgentEventType.MODEL_RESPONSE,
                        payload=RuntimeResponse(
                            message=Message(role="assistant", content=pause_message),
                            finish_reason=TOOL_CALL_LIMIT_FINISH_REASON,
                        ),
                    )
                    yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload=TOOL_CALL_LIMIT_FINISH_REASON)
                    return

                missing_fields = _missing_required_fields(tool.input_schema, tool_call.arguments)
                if missing_fields:
                    missing_field = missing_fields[0]
                    await self.hooks.emit(
                        HookEvent.NOTIFICATION,
                        {
                            "event": "clarification_requested",
                            "tool_name": tool.name,
                            "tool_source": record.source,
                            "tool_origin": record.origin,
                            "field": missing_field,
                            "session_id": context.session_id,
                            "call_id": tool_call.call_id,
                            **_correlation_payload(context, tool_call_id=tool_call.call_id),
                        },
                    )
                    clarification_request = ConfirmationRequest(
                        kind=ConfirmationKind.CLARIFICATION,
                        tool_name=tool.name,
                        prompt=f"Provide a value for '{missing_field}' before running '{tool.name}'.",
                        reason="Required tool argument is missing.",
                        call_id=tool_call.call_id,
                        payload={"tool_name": tool.name, "field": missing_field},
                        arguments=tool_call.arguments,
                        preview=confirmation_preview,
                    )
                    yield AgentEvent(
                        kind=AgentEventType.CONFIRMATION_REQUESTED,
                        payload=clarification_request,
                    )
                    return

                decision = self.permission_checker.evaluate(
                    tool,
                    tool_call.arguments,
                    mode,
                    context=context,
                    auto_confirm_read_only=auto_confirm_read_only,
                )

                if decision.decision is PermissionDecision.DENY:
                    denied_result = _denied_tool_result(
                        tool_call.call_id,
                        tool.name,
                        decision.reason,
                        risk_level=_risk_level_name(decision.risk_level),
                    )
                    await self.hooks.emit(
                        HookEvent.NOTIFICATION,
                        {
                            "event": "tool_denied",
                            "tool_name": tool.name,
                            "tool_source": record.source,
                            "tool_origin": record.origin,
                            "arguments": tool_call.arguments,
                            "reason": decision.reason,
                            "session_id": context.session_id,
                            "call_id": tool_call.call_id,
                            **_correlation_payload(context, tool_call_id=tool_call.call_id),
                        },
                    )
                    tool_calls_executed += 1
                    tool_result_messages.append(Message(
                        role="tool",
                        content=denied_result.output,
                        name=denied_result.tool_name,
                        tool_call_id=denied_result.call_id,
                    ))
                    yield AgentEvent(kind=AgentEventType.TOOL_DENIED, payload=decision)
                    yield AgentEvent.tool_call_complete(denied_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=denied_result)
                    loop_detector.record_action("tool_call", tool_name=tool_call.tool_name, result="denied")
                    continue

                if approval_manager is not None and approval_manager.is_refused(tool.name, tool_call.arguments):
                    refused_reason = "User previously denied this tool call in the current turn. Continue without running it."
                    refused_result = _denied_tool_result(
                        tool_call.call_id,
                        tool.name,
                        refused_reason,
                        risk_level=_risk_level_name(decision.risk_level),
                    )
                    tool_calls_executed += 1
                    tool_result_messages.append(Message(
                        role="tool",
                        content=refused_result.output,
                        name=refused_result.tool_name,
                        tool_call_id=refused_result.call_id,
                    ))
                    yield AgentEvent.tool_call_complete(refused_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=refused_result)
                    loop_detector.record_action("tool_call", tool_name=tool_call.tool_name, result="refused")
                    continue

                if (
                    decision.decision is PermissionDecision.CONFIRM
                    and not auto_confirm
                    and not _is_tool_preapproved(
                        tool.name,
                        tool_call.arguments,
                        is_mutating=tool.is_mutating,
                        risk_level=_risk_level_name(decision.risk_level),
                        approved_tools=approved_tools,
                        approval_manager=approval_manager,
                    )
                ):
                    conf_request = ConfirmationRequest(
                        kind=ConfirmationKind.APPROVAL,
                        tool_name=tool.name,
                        prompt=f"Allow tool '{tool.name}'?",
                        reason=decision.reason,
                        call_id=tool_call.call_id,
                        payload={
                            "tool_name": tool.name,
                            "reason": decision.reason,
                            "approval_policy": str(context.metadata.get("approval_policy", "on-request")),
                            "risk_level": _risk_level_name(decision.risk_level),
                        },
                        arguments=tool_call.arguments,
                        preview=confirmation_preview,
                    )
                    await self.hooks.emit(
                        HookEvent.NOTIFICATION,
                        {
                            "event": "confirmation_requested",
                            "tool_name": tool.name,
                            "tool_source": record.source,
                            "tool_origin": record.origin,
                            "arguments": tool_call.arguments,
                            "reason": decision.reason,
                            "session_id": context.session_id,
                            "call_id": tool_call.call_id,
                            **_correlation_payload(context, tool_call_id=tool_call.call_id),
                        },
                    )
                    yield AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=conf_request)
                    return

                result: ToolResult | None = None
                async for event in self._execute_tool_call(
                    record,
                    tool,
                    tool_call,
                    context,
                    approval_manager=approval_manager,
                    confirmation_preview=confirmation_preview,
                ):
                    if event.kind == AgentEventType.TOOL_RESULT:
                        result = event.payload
                    yield event
                if result is None:
                    continue
                tool_calls_executed += 1
                tool_result_messages.append(Message(
                    role="tool",
                    content=result.output,
                    name=result.tool_name,
                    tool_call_id=result.call_id,
                ))
                loop_detector.record_action("tool_call", tool_name=tool_call.tool_name)

            # Add all tool results to history after the complete batch, then prune
            # and check for loops — matching the reference-code execution model.
            for msg in tool_result_messages:
                history.append(msg)
            prune_tool_outputs(history, protect_tokens=2000, minimum_tokens=500)
            loop_error = loop_detector.check_for_loop()
            if loop_error:
                history.append(Message(
                    role="user",
                    content=f"[Loop detected] {loop_error} Try a different approach.",
                ))

        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="max_turns")


def _missing_required_fields(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    missing: list[str] = []
    for field_name in required:
        if field_name not in arguments or arguments[field_name] is None:
            missing.append(field_name)
            continue
        value = arguments[field_name]
        if isinstance(value, str):
            min_length = properties.get(field_name, {}).get("minLength", 0)
            if min_length > 0 and not value.strip():
                missing.append(field_name)
    return missing


def _is_tool_preapproved(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    is_mutating: bool,
    risk_level: str,
    approved_tools: set[str],
    approval_manager: ApprovalManager | None,
) -> bool:
    if approval_manager is not None:
        return approval_manager.is_pre_approved(tool_name, arguments) or approval_manager.is_turn_wide_mutating_preapproved(
            tool_name,
            is_mutating=is_mutating,
            risk_level=risk_level,
        )
    return tool_name in approved_tools


def _tool_call_limit_pause_message(max_tool_calls_per_turn: int) -> str:
    return (
        "Tool call limit reached for this turn "
        f"({max_tool_calls_per_turn}). Write `continue` to resume the previous task."
    )


def _risk_level_name(value: Any) -> str:
    raw_value = getattr(value, "value", value)
    return str(raw_value).strip().lower().split(".")[-1]


def _correlation_payload(context: ToolExecutionContext, *, tool_call_id: str | None = None) -> dict[str, Any]:
    payload = {
        "turn_id": context.metadata.get("turn_id"),
        "trace_id": context.metadata.get("trace_id"),
        "worker_id": context.metadata.get("worker_id"),
    }
    if tool_call_id is not None:
        payload["tool_call_id"] = tool_call_id
    return {key: value for key, value in payload.items() if value}


async def _get_tool_confirmation(
    tool: Any,
    call_id: str,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolConfirmation | None:
    get_confirmation = getattr(tool, "get_confirmation", None)
    if get_confirmation is None:
        return None
    return await get_confirmation(call_id, arguments, context)


def _confirmation_preview(confirmation: ToolConfirmation | None) -> dict[str, Any]:
    if confirmation is None:
        return {}
    preview: dict[str, Any] = {
        "description": confirmation.description or "",
        "is_dangerous": confirmation.is_dangerous,
    }
    if confirmation.command:
        preview["command"] = confirmation.command
    if confirmation.affected_paths:
        preview["affected_paths"] = [str(path) for path in confirmation.affected_paths]
    if confirmation.diff is not None:
        preview["diff"] = _serialize_file_diff(confirmation.diff)
    return preview


def _serialize_file_diff(diff: FileDiff) -> dict[str, Any]:
    return {
        "path": str(diff.path),
        "is_new_file": diff.is_new_file,
        "is_deletion": diff.is_deletion,
        "old_content": diff.old_content,
        "new_content": diff.new_content,
        "unified_diff": diff.to_diff(),
    }


def _denied_tool_result(
    call_id: str,
    tool_name: str,
    reason: str,
    *,
    risk_level: str,
) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name=tool_name,
        output=f"Permission denied: {reason}",
        is_error=True,
        metadata={
            "denied": True,
            "reason": reason,
            "risk_level": risk_level,
        },
    )
