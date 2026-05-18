from __future__ import annotations

import time

from collections.abc import AsyncGenerator
from pathlib import Path
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
from nexus.tools.base import FileDiff, ToolConfirmation, ToolRegistry, tool_to_schema


TOOL_CALL_LIMIT_FINISH_REASON = "tool_call_limit"
INVALID_TOOL_CALL_RETRY_LIMIT = 2


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
        mutating_paths_this_batch: set[str] = set()
        for tool_call in tool_calls:
            try:
                record = self.tool_registry.record(tool_call.tool_name)
            except LookupError:
                result = _unknown_tool_result(
                    tool_call,
                    self.tool_registry,
                    retry_count=1,
                    retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
                )
                yield AgentEvent.tool_call_start(
                    tool_call.call_id,
                    str(tool_call.tool_name),
                    tool_call.arguments,
                    actor=_tool_actor(context),
                )
                yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                yield AgentEvent.tool_call_complete(result)
                yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)
                continue
            if not _tool_available_in_context(record, context):
                result = _tool_not_available_result(tool_call, self.tool_registry, context)
                yield AgentEvent.tool_call_start(
                    tool_call.call_id,
                    str(tool_call.tool_name),
                    tool_call.arguments,
                    actor=_tool_actor(context),
                )
                yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                yield AgentEvent.tool_call_complete(result)
                yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)
                continue
            tool = record.tool
            confirmation = await _get_tool_confirmation(tool, tool_call.call_id, tool_call.arguments, context)
            confirmation_preview = _confirmation_preview(confirmation)
            affected_paths = _affected_file_paths(tool, tool_call.arguments, confirmation, context)
            duplicate_paths = affected_paths & mutating_paths_this_batch if tool.is_mutating else set()
            if duplicate_paths:
                blocked_result = _same_file_mutation_result(tool_call, duplicate_paths)
                yield AgentEvent.tool_call_complete(blocked_result)
                yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=blocked_result)
                continue
            if tool.is_mutating:
                mutating_paths_this_batch.update(affected_paths)
            async for event in self._execute_tool_call(
                record,
                tool,
                tool_call,
                context,
                approval_manager=approval_manager,
                confirmation_preview=confirmation_preview,
                affected_paths=affected_paths,
            ):
                yield event

    def preapproved_tool_calls_from_batch(
        self,
        tool_calls: tuple[ToolCall, ...],
        *,
        first_tool_call: ToolCall,
        mode: ExecutionMode,
        context: ToolExecutionContext,
        approval_manager: ApprovalManager,
        auto_confirm_read_only: bool,
    ) -> tuple[ToolCall, ...]:
        """Return same-batch calls executable under the current approval state."""
        if not tool_calls:
            return (first_tool_call,)
        start_index = next(
            (index for index, call in enumerate(tool_calls) if call.call_id == first_tool_call.call_id),
            0,
        )
        approved_calls: list[ToolCall] = []
        mutating_paths_this_batch: set[str] = set()
        for call in tool_calls[start_index:]:
            try:
                record = self.tool_registry.record(call.tool_name)
            except Exception:
                continue
            if not _tool_available_in_context(record, context):
                continue
            affected_paths = _affected_file_paths(record.tool, call.arguments, None, context)
            if record.tool.is_mutating and affected_paths & mutating_paths_this_batch:
                continue
            decision = self.permission_checker.evaluate(
                record.tool,
                call.arguments,
                mode,
                context=context,
                auto_confirm_read_only=auto_confirm_read_only,
            )
            risk_level = _risk_level_name(decision.risk_level)
            if decision.decision is PermissionDecision.DENY:
                continue
            if approval_manager.is_pre_approved(
                call.tool_name,
                call.arguments,
            ) or approval_manager.is_turn_wide_mutating_preapproved(
                call.tool_name,
                is_mutating=record.tool.is_mutating,
                risk_level=risk_level,
            ):
                approved_calls.append(call)
                if record.tool.is_mutating:
                    mutating_paths_this_batch.update(affected_paths)
        return tuple(approved_calls or (first_tool_call,))

    async def _execute_tool_call(
        self,
        record: Any,
        tool: Any,
        tool_call: ToolCall,
        context: ToolExecutionContext,
        *,
        approval_manager: ApprovalManager | None,
        confirmation_preview: dict[str, Any],
        affected_paths: set[str] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        actor = _tool_actor(context)
        yield AgentEvent.tool_call_start(
            tool_call.call_id,
            tool_call.tool_name,
            tool_call.arguments,
            preview=confirmation_preview,
            actor=actor,
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
        if actor:
            result.metadata = {**result.metadata, "actor": actor}
        if tool.is_mutating:
            result = _with_post_mutation_refresh(
                result,
                affected_paths or set(),
                context,
            )
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
        unknown_tool_retries: dict[str, int] = {}
        invalid_argument_retries: dict[tuple[str, tuple[str, ...]], int] = {}

        for turn_index in range(max_turns):
            if turn_index > 0:
                yield AgentEvent(kind=AgentEventType.THINKING_STARTED)
            request = RuntimeRequest(
                model_name=model_name,
                system_prompt=system_prompt,
                messages=tuple(history),
                tool_schemas=_tool_schemas_for_context(self.tool_registry, context),
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
            mutating_paths_this_batch: set[str] = set()
            for tool_call in tool_calls:
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

                try:
                    record = self.tool_registry.record(tool_call.tool_name)
                except LookupError:
                    tool_name = _tool_name_text(tool_call.tool_name)
                    retry_count = unknown_tool_retries.get(tool_name, 0) + 1
                    unknown_tool_retries[tool_name] = retry_count
                    unknown_result = _unknown_tool_result(
                        tool_call,
                        self.tool_registry,
                        retry_count=retry_count,
                        retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
                    )
                    tool_calls_executed += 1
                    tool_result_messages.append(_tool_result_message(unknown_result))
                    yield AgentEvent.tool_call_start(
                        tool_call.call_id,
                        tool_name,
                        tool_call.arguments,
                        actor=_tool_actor(context),
                    )
                    yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                    yield AgentEvent.tool_call_complete(unknown_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=unknown_result)
                    loop_detector.record_action("tool_call", tool_name=tool_name, result="unknown_tool")
                    if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                        for msg in tool_result_messages:
                            history.append(msg)
                        stop_message = _invalid_tool_call_stop_message(tool_name, "unknown tool name")
                        yield AgentEvent.text_complete(stop_message)
                        yield AgentEvent(
                            kind=AgentEventType.MODEL_RESPONSE,
                            payload=RuntimeResponse(
                                message=Message(role="assistant", content=stop_message),
                                finish_reason="invalid_tool_call",
                            ),
                        )
                        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="invalid_tool_call")
                        return
                    continue
                if not _tool_available_in_context(record, context):
                    tool_name = _tool_name_text(tool_call.tool_name)
                    retry_count = unknown_tool_retries.get(tool_name, 0) + 1
                    unknown_tool_retries[tool_name] = retry_count
                    unavailable_result = _tool_not_available_result(
                        tool_call,
                        self.tool_registry,
                        context,
                        retry_count=retry_count,
                        retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
                    )
                    tool_calls_executed += 1
                    tool_result_messages.append(_tool_result_message(unavailable_result))
                    yield AgentEvent.tool_call_start(
                        tool_call.call_id,
                        tool_name,
                        tool_call.arguments,
                        actor=_tool_actor(context),
                    )
                    yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                    yield AgentEvent.tool_call_complete(unavailable_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=unavailable_result)
                    loop_detector.record_action("tool_call", tool_name=tool_name, result="unavailable_tool")
                    if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                        for msg in tool_result_messages:
                            history.append(msg)
                        stop_message = _invalid_tool_call_stop_message(tool_name, "tool unavailable in this context")
                        yield AgentEvent.text_complete(stop_message)
                        yield AgentEvent(
                            kind=AgentEventType.MODEL_RESPONSE,
                            payload=RuntimeResponse(
                                message=Message(role="assistant", content=stop_message),
                                finish_reason="invalid_tool_call",
                            ),
                        )
                        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="invalid_tool_call")
                        return
                    continue

                tool = record.tool
                missing_fields = _missing_required_fields(tool.input_schema, tool_call.arguments)
                if missing_fields:
                    if _missing_fields_should_be_repaired_by_model(missing_fields):
                        retry_key = (tool.name, tuple(missing_fields))
                        retry_count = invalid_argument_retries.get(retry_key, 0) + 1
                        invalid_argument_retries[retry_key] = retry_count
                        invalid_result = _missing_required_argument_result(
                            tool_call,
                            tool.name,
                            missing_fields,
                            retry_count=retry_count,
                            retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
                        )
                        tool_calls_executed += 1
                        tool_result_messages.append(_tool_result_message(invalid_result))
                        yield AgentEvent.tool_call_start(
                            tool_call.call_id,
                            tool.name,
                            tool_call.arguments,
                            actor=_tool_actor(context),
                        )
                        yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                        yield AgentEvent.tool_call_complete(invalid_result)
                        yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=invalid_result)
                        loop_detector.record_action("tool_call", tool_name=tool.name, result="missing_argument")
                        if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                            for msg in tool_result_messages:
                                history.append(msg)
                            stop_message = _invalid_tool_call_stop_message(tool.name, "missing required arguments")
                            yield AgentEvent.text_complete(stop_message)
                            yield AgentEvent(
                                kind=AgentEventType.MODEL_RESPONSE,
                                payload=RuntimeResponse(
                                    message=Message(role="assistant", content=stop_message),
                                    finish_reason="invalid_tool_call",
                                ),
                            )
                            yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="invalid_tool_call")
                            return
                        continue

                    missing_field = missing_fields[0]
                    confirmation_preview: dict[str, Any] = {}
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
                        payload={"tool_name": tool.name, "field": missing_field, "actor": _tool_actor(context)},
                        arguments=tool_call.arguments,
                        preview=confirmation_preview,
                    )
                    yield AgentEvent(
                        kind=AgentEventType.CONFIRMATION_REQUESTED,
                        payload=clarification_request,
                    )
                    return

                confirmation = await _get_tool_confirmation(tool, tool_call.call_id, tool_call.arguments, context)
                confirmation_preview = _confirmation_preview(confirmation)
                affected_paths = _affected_file_paths(tool, tool_call.arguments, confirmation, context)

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
                    tool_result_messages.append(_tool_result_message(denied_result))
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
                    tool_result_messages.append(_tool_result_message(refused_result))
                    yield AgentEvent.tool_call_complete(refused_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=refused_result)
                    loop_detector.record_action("tool_call", tool_name=tool_call.tool_name, result="refused")
                    continue

                duplicate_paths = affected_paths & mutating_paths_this_batch if tool.is_mutating else set()
                if duplicate_paths:
                    blocked_result = _same_file_mutation_result(tool_call, duplicate_paths)
                    tool_calls_executed += 1
                    tool_result_messages.append(_tool_result_message(blocked_result))
                    yield AgentEvent.tool_call_complete(blocked_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=blocked_result)
                    loop_detector.record_action("tool_call", tool_name=tool_call.tool_name, result="same_file_blocked")
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
                            "actor": _tool_actor(context),
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

                if tool.is_mutating:
                    mutating_paths_this_batch.update(affected_paths)
                result: ToolResult | None = None
                async for event in self._execute_tool_call(
                    record,
                    tool,
                    tool_call,
                    context,
                    approval_manager=approval_manager,
                    confirmation_preview=confirmation_preview,
                    affected_paths=affected_paths,
                ):
                    if event.kind == AgentEventType.TOOL_RESULT:
                        result = event.payload
                    yield event
                if result is None:
                    continue
                tool_calls_executed += 1
                tool_result_messages.append(_tool_result_message(result))
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


def _tool_result_message(result: ToolResult) -> Message:
    return Message(
        role="tool",
        content=result.output,
        name=result.tool_name,
        tool_call_id=result.call_id,
    )


def _tool_name_text(tool_name: Any) -> str:
    if isinstance(tool_name, str):
        return tool_name or "(empty)"
    return repr(tool_name)


def _unknown_tool_result(
    tool_call: ToolCall,
    registry: ToolRegistry,
    *,
    retry_count: int,
    retry_limit: int,
) -> ToolResult:
    tool_name = _tool_name_text(tool_call.tool_name)
    available = ", ".join(record.name for record in registry.records()) or "(no tools registered)"
    if retry_count > retry_limit:
        guidance = (
            f"Retry limit exceeded after {retry_limit} repair attempts. Stop using this hallucinated tool name."
        )
    else:
        guidance = (
            "Retry with one of the available tool names from the current tool schema. "
            f"Repair attempt {retry_count} of {retry_limit}."
        )
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=tool_name,
        output=(
            f"Unknown tool name: {tool_name}. This tool is not registered in the current context. "
            f"Available tools: {available}. {guidance}"
        ),
        is_error=True,
        metadata={
            "unknown_tool": True,
            "tool_name": tool_name,
            "retry_count": retry_count,
            "retry_limit": retry_limit,
            "available_tools": [record.name for record in registry.records()],
        },
    )


def _tool_not_available_result(
    tool_call: ToolCall,
    registry: ToolRegistry,
    context: ToolExecutionContext,
    *,
    retry_count: int = 1,
    retry_limit: int = INVALID_TOOL_CALL_RETRY_LIMIT,
) -> ToolResult:
    del context
    tool_name = _tool_name_text(tool_call.tool_name)
    available = ", ".join(
        record.name
        for record in registry.records()
        if record.name.startswith("subagent_")
    ) or "(none)"
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=tool_name,
        output=(
            f"Tool '{tool_name}' is not available to the supervisor in advanced mode. "
            f"Call the appropriate cognitive sub-agent instead. Available supervisor tools: {available}."
        ),
        is_error=True,
        metadata={
            "tool_unavailable": True,
            "tool_name": tool_name,
            "retry_count": retry_count,
            "retry_limit": retry_limit,
            "available_tools": [record.name for record in registry.records() if record.name.startswith("subagent_")],
        },
    )


def _missing_fields_should_be_repaired_by_model(missing_fields: list[str]) -> bool:
    model_owned_fields = {
        "content",
        "new_content",
        "old_content",
        "new_string",
        "old_string",
        "code",
        "patch",
    }
    return any(field in model_owned_fields for field in missing_fields)


def _missing_required_argument_result(
    tool_call: ToolCall,
    tool_name: str,
    missing_fields: list[str],
    *,
    retry_count: int,
    retry_limit: int,
) -> ToolResult:
    fields_text = ", ".join(f"'{field}'" for field in missing_fields)
    supplied_keys = ", ".join(sorted(str(key) for key in tool_call.arguments)) or "(none)"
    alias_hint = ""
    if "content" in missing_fields and "text" in tool_call.arguments:
        alias_hint = " You supplied 'text'; use 'content' for the file body instead."
    if retry_count > retry_limit:
        guidance = (
            f"Retry limit exceeded after {retry_limit} repair attempts. Stop and explain the blocker."
        )
    else:
        guidance = (
            "Retry the tool call with valid arguments generated from the task context. "
            f"Repair attempt {retry_count} of {retry_limit}."
        )
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=tool_name,
        output=(
            f"Missing required argument(s) for tool '{tool_name}': {fields_text}. "
            f"Supplied argument keys: {supplied_keys}.{alias_hint} {guidance}"
        ),
        is_error=True,
        metadata={
            "missing_required_arguments": missing_fields,
            "retry_count": retry_count,
            "retry_limit": retry_limit,
        },
    )


def _invalid_tool_call_stop_message(tool_name: str, reason: str) -> str:
    return (
        f"Stopping because the model repeatedly produced an invalid tool call for '{tool_name}' "
        f"({reason}) after {INVALID_TOOL_CALL_RETRY_LIMIT} repair attempts."
    )


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


def _tool_actor(context: ToolExecutionContext) -> str:
    actor = context.metadata.get("tool_display_prefix") or context.metadata.get("subagent")
    return str(actor).strip() if actor else ""


def _tool_schemas_for_context(registry: ToolRegistry, context: ToolExecutionContext) -> tuple[dict[str, Any], ...]:
    if not context.metadata.get("supervisor_cognitive_tools_only"):
        return registry.schemas()
    return tuple(
        tool_to_schema(record.tool)
        for record in registry.records()
        if record.name.startswith("subagent_")
    )


def _tool_available_in_context(record: Any, context: ToolExecutionContext) -> bool:
    if not context.metadata.get("supervisor_cognitive_tools_only"):
        return True
    return str(record.name).startswith("subagent_")


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


def _affected_file_paths(
    tool: Any,
    arguments: dict[str, Any],
    confirmation: ToolConfirmation | None,
    context: ToolExecutionContext,
) -> set[str]:
    if not getattr(tool, "is_mutating", False):
        return set()

    workspace = context.working_directory.resolve()
    candidates: list[Path] = []
    if confirmation is not None:
        candidates.extend(confirmation.affected_paths)

    raw_path = arguments.get("path")
    if raw_path:
        candidates.append(_resolve_workspace_path(workspace, str(raw_path)))

    if getattr(tool, "name", "") == "apply_patch":
        candidates.extend(_paths_from_patch_argument(workspace, str(arguments.get("patch", "")), int(arguments.get("strip", 1))))

    paths: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        try:
            rel = resolved.relative_to(workspace)
            paths.add(str(rel))
        except ValueError:
            paths.add(str(resolved))
    return paths


def _resolve_workspace_path(workspace: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _paths_from_patch_argument(workspace: Path, patch_text: str, strip: int) -> list[Path]:
    paths: list[Path] = []
    pending_old_path: str | None = None
    for line in patch_text.splitlines():
        if line.startswith("--- "):
            pending_old_path = _clean_patch_path(line[4:])
            continue
        if line.startswith("+++ ") and pending_old_path is not None:
            new_path = _clean_patch_path(line[4:])
            raw_path = pending_old_path if new_path == "/dev/null" else new_path
            pending_old_path = None
            if raw_path == "/dev/null":
                continue
            parts = Path(raw_path).parts
            if strip > 0 and len(parts) > strip:
                raw_path = str(Path(*parts[strip:]))
            paths.append(_resolve_workspace_path(workspace, raw_path))
    return paths


def _clean_patch_path(raw_path: str) -> str:
    path = raw_path.split("\t")[0].strip()
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _same_file_mutation_result(tool_call: ToolCall, duplicate_paths: set[str]) -> ToolResult:
    paths = ", ".join(sorted(duplicate_paths))
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=tool_call.tool_name,
        output=(
            "Skipped mutating tool call: another mutating tool call in the same model response "
            f"already targeted {paths}. Read the file again after the prior mutation and issue a fresh edit."
        ),
        is_error=True,
        metadata={
            "same_file_mutation_blocked": True,
            "paths": sorted(duplicate_paths),
        },
    )


def _with_post_mutation_refresh(
    result: ToolResult,
    affected_paths: set[str],
    context: ToolExecutionContext,
) -> ToolResult:
    if not affected_paths or (result.is_error and int(result.metadata.get("files_patched", 0) or 0) <= 0):
        return result

    workspace = context.working_directory.resolve()
    refreshes: list[dict[str, Any]] = []
    output_parts = [result.output]
    for path_text in sorted(affected_paths):
        path = _resolve_workspace_path(workspace, path_text)
        try:
            rel = str(path.relative_to(workspace))
        except ValueError:
            rel = str(path)

        if not path.exists():
            refreshes.append({"path": rel, "exists": False, "content": "", "truncated": False})
            output_parts.append(f"\n[Post-mutation refresh]\nRead {rel} after mutation: file no longer exists.")
            continue
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            refreshes.append({"path": rel, "exists": True, "error": str(exc)})
            output_parts.append(f"\n[Post-mutation refresh]\nCould not read {rel} after mutation: {exc}")
            continue

        max_chars = int(context.metadata.get("post_mutation_read_max_chars", 12_000) or 12_000)
        clipped = content[:max_chars]
        truncated = len(content) > max_chars
        refreshes.append(
            {
                "path": rel,
                "exists": True,
                "content": clipped,
                "truncated": truncated,
                "line_count": len(content.splitlines()),
            }
        )
        suffix = "\n...[truncated]" if truncated else ""
        output_parts.append(
            f"\n[Post-mutation refresh]\nRead {rel} after mutation ({len(content.splitlines())} lines):\n"
            f"```text\n{clipped}{suffix}\n```"
        )

    if not refreshes:
        return result

    metadata = dict(result.metadata)
    metadata["post_mutation_reads"] = refreshes
    metadata["affected_paths"] = sorted(affected_paths)
    return ToolResult(
        call_id=result.call_id,
        tool_name=result.tool_name,
        output="\n".join(output_parts),
        is_error=result.is_error,
        metadata=metadata,
    )


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
