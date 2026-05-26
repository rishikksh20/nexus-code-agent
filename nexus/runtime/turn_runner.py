from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import cast
from uuid import uuid4

from nexus.context import ContextCompactor, TokenEstimator
from nexus.hooks import HookEvent
from nexus.models import (
    AgentEvent,
    AgentEventType,
    ConfirmationKind,
    ConfirmationRequest,
    ConfirmationResponse,
    Message,
    RuntimeResponse,
    ToolCall,
    ToolExecutionContext,
    ToolResult,
)
from nexus.observability import capture_exception_from_hooks, sentry_monitor_from_hooks
from nexus.runtime.agent import Agent, MAX_TURNS_FINISH_REASON, TOOL_CALL_LIMIT_FINISH_REASON
from nexus.runtime.repl_state import ReplState, apply_events_to_messages
from nexus.security.manager import ApprovalScope
from nexus.security.policy import ApprovalPolicy
from nexus.ui import TerminalUI


ConfirmationCallback = Callable[[ConfirmationRequest], Awaitable[ConfirmationResponse]]
logger = logging.getLogger(__name__)


def prompt_for_confirmation(
    request: ConfirmationRequest,
    *,
    input_func: Callable[[str], str] | None = None,
) -> ConfirmationResponse:
    input_reader = input if input_func is None else input_func
    try:
        if request.kind is ConfirmationKind.CLARIFICATION:
            answer = input_reader(f"{request.prompt} ").strip()
            return ConfirmationResponse(clarification=answer) if answer else ConfirmationResponse()
        approval_policy = approval_policy_for_request(request)
        answer = input_reader(f"{request.prompt} {approval_prompt_label(approval_policy)} ").strip().lower()
    except EOFError:
        return ConfirmationResponse()
    return approval_response_from_answer(answer, approval_policy)


async def run_agent_turn(
    state: ReplState,
    agent: Agent,
    *,
    prompt_text: str,
    ui: TerminalUI | None = None,
    approval_callback: ConfirmationCallback | None = None,
    auto_confirm: bool = False,
) -> list[AgentEvent]:
    """Run one AI turn and return the events that are safe to commit.

    The caller owns the outer user interaction: reading a prompt, dispatching
    slash commands, appending the user message, and applying returned events to
    the durable session history. This runner owns model streaming, tool
    execution, approval prompts, and the provider-safe history used between
    retries and approved tool resumes.
    """
    committed_events: list[AgentEvent] = []
    working_history = list(state.history)
    initial_prompt_text = prompt_text
    turn_id = state.current_turn_id or uuid4().hex[:12]
    trace_id = state.current_trace_id or uuid4().hex
    started_at = time.perf_counter()
    start_payload = _turn_lifecycle_payload(
        state,
        turn_id=turn_id,
        trace_id=trace_id,
        status="started",
        started_at=started_at,
    )
    if state.hooks is not None:
        await state.hooks.emit(HookEvent.TURN_START, start_payload)

    monitor = sentry_monitor_from_hooks(state.hooks)
    transaction = (
        monitor.start_transaction(
            name="nexus.turn",
            op="nexus.turn",
            attributes=start_payload,
        )
        if monitor is not None
        else None
    )
    transaction_context = transaction if transaction is not None else nullcontext()

    try:
        with transaction_context:
            while True:
                prepared_turn = state.prepare_turn(
                    prompt_text,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    history=working_history,
                    compactor_factory=ContextCompactor,
                    estimator_factory=TokenEstimator,
                )
                prepared_turn.context.metadata["approval_callback"] = approval_callback
                prepared_turn.context.metadata["approval_manager"] = state.approval_manager
                prepared_turn.context.metadata["execution_mode"] = state.mode.value
                prepared_turn.context.metadata["auto_confirm"] = auto_confirm
                prepared_turn.context.metadata["auto_confirm_read_only"] = state.config.auto_confirm_read_only
                prepared_turn.context.metadata["ui"] = ui
                prepared_turn.context.metadata["hooks"] = state.hooks
                prepared_turn.context.metadata["stream_output"] = state.config.stream_output
                prepared_turn.context.metadata["show_tool_calls"] = state.config.show_tool_calls
                prepared_turn.context.metadata["supervisor_cognitive_tools_only"] = (
                    str(getattr(state.config, "agent_mode", "basic")).strip().lower() == "advanced"
                )
                compaction_payload = prepared_turn.context.metadata.get("context_compaction")
                if state.hooks is not None and isinstance(compaction_payload, dict) and (
                    compaction_payload.get("compacted") or compaction_payload.get("pruned_tool_results")
                ):
                    await state.hooks.emit(HookEvent.CONTEXT_COMPACTION, compaction_payload)

                batch = await _run_model_batch(
                    state,
                    agent,
                    prepared_turn.model_messages,
                    prepared_turn.context,
                    system_prompt=prepared_turn.system_prompt,
                    ui=ui,
                    auto_confirm=auto_confirm,
                )

                confirmation_index = _first_unresolved_confirmation_index(batch)
                if confirmation_index is not None:
                    _commit_history_safe_prefix(
                        working_history,
                        committed_events,
                        batch[:confirmation_index],
                    )

                confirmation = None if confirmation_index is None else batch[confirmation_index]
                if confirmation is None:
                    committed_events.extend(batch)
                    await _finish_turn(
                        state,
                        committed_events,
                        initial_prompt_text=initial_prompt_text,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        started_at=started_at,
                        status=_turn_status_from_events(batch),
                    )
                    return committed_events

                confirmation_request = cast(ConfirmationRequest, confirmation.payload)

                if auto_confirm and confirmation_request.kind is ConfirmationKind.APPROVAL:
                    state.approval_manager.record_approval(
                        confirmation_request.tool_name,
                        approval_scope_for_policy(state.approval_manager.policy),
                        arguments=confirmation_request.arguments,
                    )
                    await _resume_approved_tool_calls(
                        state,
                        agent,
                        prepared_turn.context,
                        batch,
                        confirmation_index,
                        confirmation_request,
                        working_history,
                        committed_events,
                        ui=ui,
                        include_preapproved_batch=state.approval_manager.policy is ApprovalPolicy.APPROVE_TURN,
                    )
                    continue

                if approval_callback is None:
                    pending_events = [*committed_events, cast(AgentEvent, confirmation)]
                    await _finish_turn(
                        state,
                        pending_events,
                        initial_prompt_text=initial_prompt_text,
                        turn_id=turn_id,
                        trace_id=trace_id,
                        started_at=started_at,
                        status="awaiting_confirmation",
                    )
                    return pending_events

                response = await approval_callback(confirmation_request)
                if confirmation_request.kind is ConfirmationKind.APPROVAL and response.approved:
                    record_approval_response(state, confirmation_request, response)
                    await _resume_approved_tool_calls(
                        state,
                        agent,
                        prepared_turn.context,
                        batch,
                        confirmation_index,
                        confirmation_request,
                        working_history,
                        committed_events,
                        ui=ui,
                        include_preapproved_batch=(
                            response.scope == ApprovalScope.TURN.value
                            and supports_turn_wide_approval(confirmation_request)
                        ),
                    )
                    continue

                if confirmation_request.kind is ConfirmationKind.APPROVAL and response.denied:
                    state.approval_manager.record_refusal(
                        confirmation_request.tool_name,
                        arguments=confirmation_request.arguments,
                    )
                    continue

                if confirmation_request.kind is ConfirmationKind.CLARIFICATION and response.clarification:
                    clarification_text = (
                        f"Clarification for {confirmation_request.tool_name} "
                        f"({confirmation_request.payload.get('field', 'value')}): {response.clarification}"
                    )
                    clarification_message = Message(role="user", content=clarification_text)
                    state.history.append(clarification_message)
                    working_history.append(clarification_message)
                    prompt_text = clarification_text
                    continue

                pending_events = [*committed_events, cast(AgentEvent, confirmation)]
                await _finish_turn(
                    state,
                    pending_events,
                    initial_prompt_text=initial_prompt_text,
                    turn_id=turn_id,
                    trace_id=trace_id,
                    started_at=started_at,
                    status="stopped",
                )
                return pending_events
    except asyncio.CancelledError:
        await _emit_turn_end(
            state,
            events=committed_events,
            turn_id=turn_id,
            trace_id=trace_id,
            started_at=started_at,
            status="cancelled",
        )
        raise
    except Exception as exc:
        capture_exception_from_hooks(
            state.hooks,
            exc,
            context=_turn_lifecycle_payload(
                state,
                turn_id=turn_id,
                trace_id=trace_id,
                status="failed",
                started_at=started_at,
                error=str(exc),
            ),
        )
        await _emit_turn_end(
            state,
            events=committed_events,
            turn_id=turn_id,
            trace_id=trace_id,
            started_at=started_at,
            status="failed",
            error=str(exc),
        )
        raise


# Backward-compatible name used by existing tests and earlier docs.
collect_turn_events = run_agent_turn


async def _run_model_batch(
    state: ReplState,
    agent: Agent,
    messages: list[Message],
    context: ToolExecutionContext,
    *,
    system_prompt: str,
    ui: TerminalUI | None,
    auto_confirm: bool,
) -> list[AgentEvent]:
    batch: list[AgentEvent] = []
    logger.debug(
        "turn_runner.batch.start session_id=%s turn_id=%s trace_id=%s messages=%s last_role=%s model=%s",
        context.session_id,
        context.metadata.get("turn_id", ""),
        context.metadata.get("trace_id", ""),
        len(messages),
        messages[-1].role if messages else "",
        state.config.model_name,
    )
    try:
        async for event in agent.run(
            messages,
            context,
            system_prompt=system_prompt,
            model_name=state.config.model_name,
            mode=state.mode,
            approval_manager=state.approval_manager,
            auto_confirm=auto_confirm,
            auto_confirm_read_only=state.config.auto_confirm_read_only,
            temperature=state.config.temperature,
            max_output_tokens=state.config.max_output_tokens,
            max_turns=state.config.max_loop_iterations,
            max_tool_calls_per_turn=state.config.max_tool_calls_per_turn,
            parallel_tools=state.config.parallel_tools,
            parallel_tool_window=state.config.parallel_tool_window,
        ):
            _render_event(ui, state, event)
            batch.append(event)
    except Exception as exc:  # noqa: BLE001
        error_area = str(getattr(exc, "_nexus_error_area", "model"))
        if not getattr(exc, "_nexus_sentry_captured", False):
            capture_exception_from_hooks(
                state.hooks,
                exc,
                context={
                    "session_id": context.session_id,
                    "turn_id": context.metadata.get("turn_id", ""),
                    "trace_id": context.metadata.get("trace_id", ""),
                    "provider": state.config.provider,
                    "model": state.config.model_name,
                    "events": len(batch),
                    "error_area": error_area,
                },
            )
        if state.hooks is not None and error_area != "tool":
            await state.hooks.emit(
                HookEvent.NOTIFICATION,
                {
                    "event": "model_error",
                    "session_id": context.session_id,
                    "turn_id": context.metadata.get("turn_id", ""),
                    "trace_id": context.metadata.get("trace_id", ""),
                    "provider": state.config.provider,
                    "model": state.config.model_name,
                    "error": str(exc) or exc.__class__.__name__,
                },
            )
        logger.exception(
            "turn_runner.batch.exception session_id=%s turn_id=%s trace_id=%s events=%s error=%s",
            context.session_id,
            context.metadata.get("turn_id", ""),
            context.metadata.get("trace_id", ""),
            len(batch),
            exc,
        )
        error_event = AgentEvent.agent_error(str(exc) or exc.__class__.__name__)
        _render_event(ui, state, error_event)
        batch.append(error_event)
    logger.debug(
        "turn_runner.batch.end session_id=%s turn_id=%s trace_id=%s events=%s status=%s",
        context.session_id,
        context.metadata.get("turn_id", ""),
        context.metadata.get("trace_id", ""),
        len(batch),
        _turn_status_from_events(batch),
    )
    return batch


async def _resume_approved_tool_calls(
    state: ReplState,
    agent: Agent,
    context: ToolExecutionContext,
    batch: list[AgentEvent],
    confirmation_index: int,
    request: ConfirmationRequest,
    working_history: list[Message],
    committed_events: list[AgentEvent],
    *,
    ui: TerminalUI | None,
    include_preapproved_batch: bool,
) -> None:
    tool_call = _tool_call_for_confirmation(batch, confirmation_index, request)
    tool_calls = [tool_call]
    if include_preapproved_batch:
        tool_calls = list(
            agent.preapproved_tool_calls_from_batch(
                _tool_calls_from_confirmation_model_response(batch, confirmation_index),
                first_tool_call=tool_call,
                mode=state.mode,
                context=context,
                approval_manager=state.approval_manager,
                auto_confirm_read_only=state.config.auto_confirm_read_only,
            )
        )

    model_event = _model_response_for_pending_tool_calls(batch, confirmation_index, tool_calls)
    if model_event is not None:
        working_history.append(model_event.payload.message)
        committed_events.append(model_event)

    execution_events: list[AgentEvent] = []
    async for event in agent.run(
        working_history,
        context,
        approval_manager=state.approval_manager,
        parallel_tools=state.config.parallel_tools,
        parallel_tool_window=state.config.parallel_tool_window,
        resume_tool_calls=tuple(tool_calls),
    ):
        _render_event(ui, state, event)
        execution_events.append(event)

    apply_events_to_messages(working_history, execution_events)
    committed_events.extend(execution_events)


def _commit_history_safe_prefix(
    working_history: list[Message],
    committed_events: list[AgentEvent],
    events: list[AgentEvent],
) -> None:
    completed_events = _history_safe_completed_events(events)
    if completed_events:
        apply_events_to_messages(working_history, completed_events)
        committed_events.extend(completed_events)


def _render_event(ui: TerminalUI | None, state: ReplState, event: AgentEvent) -> None:
    if ui is None:
        return
    ui.render_event(
        event,
        stream_output=state.config.stream_output,
        show_tool_calls=state.config.show_tool_calls,
        show_thinking_indicator=state.config.show_thinking_indicator,
    )


def _turn_status_from_events(events: list[AgentEvent]) -> str:
    return "failed" if any(event.kind == AgentEventType.AGENT_ERROR for event in events) else "completed"


async def _finish_turn(
    state: ReplState,
    events: list[AgentEvent],
    *,
    initial_prompt_text: str,
    turn_id: str,
    trace_id: str,
    started_at: float,
    status: str,
) -> None:
    _sync_paused_turn_state(state, events, prompt_text=initial_prompt_text)
    duration_ms = (time.perf_counter() - started_at) * 1000
    _record_turn_telemetry(
        state,
        events,
        turn_id=turn_id,
        trace_id=trace_id,
        duration_ms=duration_ms,
        status=status,
    )
    await _emit_turn_end(
        state,
        events=events,
        turn_id=turn_id,
        trace_id=trace_id,
        started_at=started_at,
        status=status,
    )


def approval_scope_for_policy(policy: ApprovalPolicy) -> ApprovalScope:
    if policy is ApprovalPolicy.APPROVE_TURN:
        return ApprovalScope.TURN
    if policy is ApprovalPolicy.APPROVE_SESSION:
        return ApprovalScope.SESSION
    return ApprovalScope.ONCE


def approval_prompt_label(policy: ApprovalPolicy) -> str:
    if policy is ApprovalPolicy.APPROVE_TURN:
        return "(y)es for this turn / (N)o:"
    if policy is ApprovalPolicy.APPROVE_SESSION:
        return "(y)es for this session / (N)o:"
    return "(y)es once / yes (t)urn / (N)o:"


def approval_policy_for_request(request: ConfirmationRequest) -> ApprovalPolicy:
    return ApprovalPolicy(str(request.payload.get("approval_policy", ApprovalPolicy.ON_REQUEST.value)))


def approval_response_from_answer(answer: str, policy: ApprovalPolicy) -> ConfirmationResponse:
    normalized = _normalize_approval_answer(answer)
    if normalized in {"y", "yes", "once", "yes once"}:
        return ConfirmationResponse(approved=True, scope=approval_scope_for_policy(policy).value)
    if policy is ApprovalPolicy.ON_REQUEST and normalized in {"t", "turn", "yes turn", "yes t"}:
        return ConfirmationResponse(approved=True, scope=ApprovalScope.TURN.value)
    return ConfirmationResponse()


def _normalize_approval_answer(answer: str) -> str:
    normalized = answer.strip().lower()
    for token in ("(", ")", "-", "_"):
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


def record_approval_response(
    state: ReplState,
    request: ConfirmationRequest,
    response: ConfirmationResponse,
) -> None:
    scope = ApprovalScope(response.scope or approval_scope_for_policy(state.approval_manager.policy).value)
    request_policy = approval_policy_for_request(request)
    if scope is ApprovalScope.TURN and request_policy is ApprovalPolicy.ON_REQUEST:
        if supports_turn_wide_approval(request):
            state.approval_manager.record_turn_wide_mutating_approval()
            return
        state.approval_manager.record_approval(
            request.tool_name,
            ApprovalScope.ONCE,
            arguments=request.arguments,
        )
        return
    state.approval_manager.record_approval(
        request.tool_name,
        scope,
        arguments=request.arguments,
    )


def supports_turn_wide_approval(request: ConfirmationRequest) -> bool:
    risk_level = str(request.payload.get("risk_level", "medium")).strip().lower().split(".")[-1]
    return not (request.tool_name == "bash" and risk_level in {"high", "dangerous"})


def _tool_call_for_confirmation(
    batch: list[AgentEvent],
    confirmation_index: int,
    request: ConfirmationRequest,
) -> ToolCall:
    for event in reversed(batch[:confirmation_index]):
        if event.kind != AgentEventType.MODEL_RESPONSE:
            continue
        payload = cast(RuntimeResponse, event.payload)
        for tool_call in payload.tool_calls or payload.message.tool_calls:
            if request.call_id and tool_call.call_id == request.call_id:
                return tool_call
            if not request.call_id and tool_call.tool_name == request.tool_name and tool_call.arguments == request.arguments:
                return tool_call
    return ToolCall(
        call_id=request.call_id,
        tool_name=request.tool_name,
        arguments=request.arguments,
    )


def _tool_calls_from_confirmation_model_response(
    batch: list[AgentEvent],
    confirmation_index: int,
) -> tuple[ToolCall, ...]:
    for event in reversed(batch[:confirmation_index]):
        if event.kind != AgentEventType.MODEL_RESPONSE:
            continue
        payload = cast(RuntimeResponse, event.payload)
        return payload.tool_calls or payload.message.tool_calls
    return ()


def _model_response_for_pending_tool_calls(
    batch: list[AgentEvent],
    confirmation_index: int,
    tool_calls: list[ToolCall],
) -> AgentEvent | None:
    call_ids = {tool_call.call_id for tool_call in tool_calls}
    for event in reversed(batch[:confirmation_index]):
        if event.kind != AgentEventType.MODEL_RESPONSE:
            continue
        payload = cast(RuntimeResponse, event.payload)
        source_message = payload.message
        if not any(call.call_id in call_ids for call in payload.tool_calls or source_message.tool_calls):
            continue
        message = Message(
            role=source_message.role,
            content=source_message.content,
            name=source_message.name,
            tool_calls=tuple(tool_calls),
            tool_call_id=source_message.tool_call_id,
        )
        return AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(
                message=message,
                tool_calls=tuple(tool_calls),
                usage=payload.usage,
                finish_reason="tool_calls",
            ),
        )
    return None


def _sync_paused_turn_state(state: ReplState, events: list[AgentEvent], *, prompt_text: str) -> None:
    if _turn_finished_with_resumable_pause(events):
        state.mark_paused_turn(prompt_text)
        return
    state.clear_paused_turn()


def _turn_finished_with_resumable_pause(events: list[AgentEvent]) -> bool:
    turn_completed = next(
        (event for event in reversed(events) if event.kind == AgentEventType.TURN_COMPLETED),
        None,
    )
    return bool(
        turn_completed
        and turn_completed.payload in {TOOL_CALL_LIMIT_FINISH_REASON, MAX_TURNS_FINISH_REASON}
    )


async def _emit_turn_end(
    state: ReplState,
    *,
    events: list[AgentEvent],
    turn_id: str,
    trace_id: str,
    started_at: float,
    status: str,
    error: str | None = None,
) -> None:
    if state.hooks is None:
        return
    await state.hooks.emit(
        HookEvent.TURN_END,
        _turn_lifecycle_payload(
            state,
            turn_id=turn_id,
            trace_id=trace_id,
            status=status,
            started_at=started_at,
            events=events,
            error=error,
        ),
    )


def _turn_lifecycle_payload(
    state: ReplState,
    *,
    turn_id: str,
    trace_id: str,
    status: str,
    started_at: float,
    events: list[AgentEvent] | None = None,
    error: str | None = None,
) -> dict:
    usage, tool_calls = _turn_usage_and_tool_calls(events or [])
    response = _turn_response_text(events or [])
    turn_steps = _turn_steps(events or [])
    payload = {
        "session_id": state.session.session_id,
        "turn_id": turn_id,
        "trace_id": trace_id,
        "mode": state.mode.value,
        "agent_mode": getattr(state.config, "agent_mode", "basic"),
        "provider": state.config.provider,
        "model": state.config.model_name,
        "status": status,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "tool_calls": tool_calls,
        "response": response,
        "turn_steps": turn_steps,
    }
    if usage is not None:
        payload["usage"] = {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "estimated_cost_usd": usage.estimated_cost_usd,
            "provider": usage.provider,
            "model": usage.model,
        }
    if error:
        payload["error"] = error
    return payload


def _turn_usage_and_tool_calls(events: list[AgentEvent]):
    usage = None
    tool_calls = 0
    for event in events:
        if event.kind == "model_response" and event.payload.usage is not None:
            usage = event.payload.usage
        elif event.kind == "tool_call_requested":
            tool_calls += 1
    return usage, tool_calls


def _turn_response_text(events: list[AgentEvent]) -> str:
    for event in reversed(events):
        if event.kind != AgentEventType.MODEL_RESPONSE:
            continue
        payload = cast(RuntimeResponse, event.payload)
        if payload.message.content:
            return payload.message.content
    return ""


def _turn_steps(events: list[AgentEvent]) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = []
    tool_inputs_by_call_id: dict[str, dict[str, object]] = {}
    for event in events:
        if event.kind == AgentEventType.MODEL_RESPONSE:
            payload = cast(RuntimeResponse, event.payload)
            serialized_tool_calls = [_serialize_tool_call(tool_call) for tool_call in payload.tool_calls]
            for tool_call in serialized_tool_calls:
                call_id = str(tool_call.get("call_id", "") or "")
                if call_id:
                    tool_inputs_by_call_id[call_id] = tool_call
            steps.append(
                {
                    "kind": "model_response",
                    "content": payload.message.content,
                    "finish_reason": payload.finish_reason,
                    "tool_calls": serialized_tool_calls,
                    "usage": _serialize_usage(payload.usage),
                }
            )
            continue
        if event.kind == AgentEventType.TOOL_RESULT:
            result = cast(ToolResult, event.payload)
            matched_input = tool_inputs_by_call_id.get(result.call_id)
            steps.append(
                {
                    "kind": "tool_execution",
                    "call_id": result.call_id,
                    "tool_name": result.tool_name,
                    "is_error": result.is_error,
                    "input": matched_input
                    or {
                        "call_id": result.call_id,
                        "tool_name": result.tool_name,
                        "arguments": {},
                    },
                    "output": {
                        "content": result.output,
                        "metadata": dict(result.metadata),
                    },
                    "is_subagent": result.tool_name == "delegate_task" or result.tool_name.startswith("subagent_"),
                }
            )
    return steps


def _serialize_tool_call(tool_call: ToolCall) -> dict[str, object]:
    return {
        "call_id": tool_call.call_id,
        "tool_name": tool_call.tool_name,
        "arguments": dict(tool_call.arguments),
    }


def _serialize_usage(usage) -> dict[str, object] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "estimated_cost_usd": usage.estimated_cost_usd,
        "provider": usage.provider,
        "model": usage.model,
    }


def _record_turn_telemetry(
    state: ReplState,
    events: list[AgentEvent],
    *,
    turn_id: str,
    trace_id: str,
    duration_ms: float,
    status: str,
) -> None:
    usage, tool_calls = _turn_usage_and_tool_calls(events)
    turns = state.session.metadata.setdefault("turns", [])
    turns.append(
        {
            "session_id": state.session.session_id,
            "turn_id": turn_id,
            "trace_id": trace_id,
            "tool_calls": tool_calls,
            "duration_ms": round(duration_ms, 2),
            "status": status,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "estimated_cost_usd": usage.estimated_cost_usd,
                "provider": usage.provider,
                "model": usage.model,
            }
            if usage is not None
            else None,
        }
    )
    if len(turns) > 20:
        del turns[:-20]


def _history_safe_completed_events(events: list[AgentEvent]) -> list[AgentEvent]:
    completed_tool_calls = {
        event.payload.call_id
        for event in events
        if event.kind == AgentEventType.TOOL_RESULT
    }
    if not completed_tool_calls:
        return []

    committed: list[AgentEvent] = []
    for event in events:
        if event.kind == AgentEventType.MODEL_RESPONSE:
            payload = cast(RuntimeResponse, event.payload)
            message = payload.message
            if not message.tool_calls:
                committed.append(event)
                continue
            completed_calls = tuple(
                tool_call for tool_call in message.tool_calls
                if tool_call.call_id in completed_tool_calls
            )
            if not completed_calls:
                continue
            committed.append(
                AgentEvent(
                    kind=event.kind,
                    payload=RuntimeResponse(
                        message=Message(
                            role=message.role,
                            content=message.content,
                            name=message.name,
                            tool_calls=completed_calls,
                            tool_call_id=message.tool_call_id,
                        ),
                        tool_calls=completed_calls,
                        usage=payload.usage,
                        finish_reason="tool_calls" if completed_calls else payload.finish_reason,
                    ),
                )
            )
            continue
        if event.kind == AgentEventType.TOOL_CALL_START:
            if event.payload.get("call_id") in completed_tool_calls:
                committed.append(event)
            continue
        if event.kind == AgentEventType.TOOL_CALL_REQUESTED:
            if event.payload.call_id in completed_tool_calls:
                committed.append(event)
            continue
        if event.kind in {AgentEventType.TOOL_CALL_COMPLETE, AgentEventType.TOOL_RESULT}:
            if event.payload.call_id in completed_tool_calls:
                committed.append(event)
            continue
        committed.append(event)
    return committed


def _first_unresolved_confirmation_index(events: list[AgentEvent]) -> int | None:
    """Return the first confirmation emitted by the agent event stream."""
    for index, event in enumerate(events):
        if event.kind == AgentEventType.CONFIRMATION_REQUESTED:
            return index
    return None
