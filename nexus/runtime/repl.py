from __future__ import annotations

import time
import readline  # noqa: F401 — activates arrow-key / history line editing for input() on macOS
from uuid import uuid4

from collections.abc import Awaitable, Callable
from typing import cast

from nexus.models import AgentEvent, AgentEventType, ConfirmationKind, ConfirmationRequest, ConfirmationResponse, Message, RuntimeResponse
from nexus.runtime.agent import Agent
from nexus.context import ContextCompactor, TokenEstimator
from nexus.hooks import HookEvent
from nexus.runtime.repl_state import ReplState, apply_events_to_messages
from nexus.security.manager import ApprovalScope
from nexus.security.policy import ApprovalPolicy
from nexus.ui import TerminalUI


ConfirmationCallback = Callable[[ConfirmationRequest], Awaitable[ConfirmationResponse]]


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
        approval_policy = _approval_policy_for_request(request)
        answer = input_reader(f"{request.prompt} {_approval_prompt_label(approval_policy)} ").strip().lower()
    except EOFError:
        return ConfirmationResponse()
    return _approval_response_from_answer(answer, approval_policy)


async def run_agent_turn(
    state: ReplState,
    agent: Agent,
    *,
    prompt_text: str,
    ui: TerminalUI | None = None,
    approval_callback: ConfirmationCallback | None = None,
    auto_confirm: bool = False,
) -> list[AgentEvent]:
    """Run one user turn through the agentic loop and return all events.

    **User turn** ends when this function is called (the interactive loop in
    :func:`run_repl` already consumed the raw input).  Everything that happens
    inside — model streaming, tool calls, approvals — is the **AI turn** driven
    by :meth:`Agent.run`.

    Parameters
    ----------
    ui:
        When provided each event is rendered live as it arrives (interactive
        REPL).  When ``None`` events are silently collected (headless mode).
    """
    committed_events: list[AgentEvent] = []
    working_history = list(state.history)
    initial_prompt_text = prompt_text
    turn_id = state.current_turn_id or uuid4().hex[:12]
    trace_id = state.current_trace_id or uuid4().hex
    started_at = time.perf_counter()

    while True:
        prepared_turn = state.prepare_turn(
            prompt_text,
            turn_id=turn_id,
            trace_id=trace_id,
            history=working_history,
            compactor_factory=ContextCompactor,
            estimator_factory=TokenEstimator,
        )

        batch: list[AgentEvent] = []

        # ── AI turn: stream model responses and tool calls ──────────────────
        async for event in agent.run(
            prepared_turn.model_messages,
            prepared_turn.context,
            system_prompt=prepared_turn.system_prompt,
            model_name=state.config.model_name,
            mode=state.mode,
            approval_manager=state.approval_manager,
            approval_callback=approval_callback,
            auto_confirm=auto_confirm,
            auto_confirm_read_only=state.config.auto_confirm_read_only,
            temperature=state.config.temperature,
            max_output_tokens=state.config.max_output_tokens,
            max_turns=state.config.max_loop_iterations,
            max_tool_calls_per_turn=state.config.max_tool_calls_per_turn,
        ):
            if ui is not None:
                ui.render_event(
                    event,
                    stream_output=state.config.stream_output,
                    show_tool_calls=state.config.show_tool_calls,
                )
            batch.append(event)

        # ── confirmation / clarification handling ────────────────────────────
        confirmation_index = next(
            (index for index, event in enumerate(batch) if event.kind == "confirmation_requested"),
            None,
        )

        if confirmation_index is not None:
            committed_prefix = _history_safe_completed_events(batch[:confirmation_index])
            if committed_prefix:
                apply_events_to_messages(working_history, committed_prefix)
                committed_events.extend(committed_prefix)

        confirmation = None if confirmation_index is None else batch[confirmation_index]

        if confirmation is None:
            # Normal completion — no confirmation needed.
            committed_events.extend(batch)
            _sync_paused_turn_state(state, committed_events, prompt_text=initial_prompt_text)
            _record_turn_telemetry(
                state, committed_events, turn_id=turn_id, trace_id=trace_id,
                duration_ms=(time.perf_counter() - started_at) * 1000, status="completed",
            )
            return committed_events

        confirmation_request = cast(ConfirmationRequest, confirmation.payload)

        if auto_confirm and confirmation_request.kind is ConfirmationKind.APPROVAL:
            state.approval_manager.record_approval(
                confirmation_request.tool_name,
                _approval_scope_for_policy(state.approval_manager.policy),
                arguments=confirmation_request.arguments,
            )
            continue

        if approval_callback is None:
            confirmation_event = cast(AgentEvent, confirmation)
            pending_events = [*committed_events, confirmation_event]
            _record_turn_telemetry(
                state, pending_events, turn_id=turn_id, trace_id=trace_id,
                duration_ms=(time.perf_counter() - started_at) * 1000, status="awaiting_confirmation",
            )
            return pending_events

        response = await approval_callback(confirmation_request)
        if confirmation_request.kind is ConfirmationKind.APPROVAL and response.approved:
            _record_approval_response(state, confirmation_request, response)
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

        confirmation_event = cast(AgentEvent, confirmation)
        pending_events = [*committed_events, confirmation_event]
        _sync_paused_turn_state(state, pending_events, prompt_text=initial_prompt_text)
        _record_turn_telemetry(
            state, pending_events, turn_id=turn_id, trace_id=trace_id,
            duration_ms=(time.perf_counter() - started_at) * 1000, status="stopped",
        )
        return pending_events

    _sync_paused_turn_state(state, committed_events, prompt_text=initial_prompt_text)
    return []


async def run_repl(state: ReplState, agent: Agent, router, *, session_resumed: bool = False) -> None:
    """Interactive REPL — the outer **user-turn** loop.

    Each iteration is one *user turn*: read a line, dispatch slash commands or
    hand the input to :func:`run_agent_turn` which drives the *AI turn* (model
    streaming + tool execution) via :meth:`Agent.run`.
    """
    ui: TerminalUI = state.console
    cfg = state.config
    ui.print_banner(cfg.provider, cfg.model_name, state.mode.value, workspace=cfg.workspace_root)
    if session_resumed:
        ui.print_session_resumed(state.session.session_id, len(state.history))
    if state.has_paused_turn():
        ui.print_muted("A previous task was paused after hitting the tool-call limit. Type `continue` to resume it, or enter a new prompt to start something else.")
    ui.print_help_hint()
    if cfg.provider == "fake":
        ui.print_fake_provider_notice()
    elif cfg.provider == "ollama":
        pass  # local provider — no API key required
    elif cfg.provider in {"mistral", "openai", "openai-compatible"}:
        from os import environ
        has_key = bool(
            cfg.api_key
            or environ.get("MISTRAL_API_KEY")
            or environ.get("NEXUS_API_KEY")
            or environ.get("OPENAI_API_KEY")
            or environ.get("API_KEY")
        )
        if not has_key:
            ui.print_no_api_key_warning(cfg.provider)

    async def ask_for_approval(request: ConfirmationRequest) -> ConfirmationResponse:
        # The confirmation panel is already printed by render_event above;
        # here we only collect the user's answer using Rich's console.input()
        # so the prompt renders correctly alongside Rich markup.
        try:
            if request.kind is ConfirmationKind.CLARIFICATION:
                field = request.payload.get("field", "value")
                answer = ui.input(f"  [bold]Value for [cyan]{field!r}[/cyan]:[/bold] ").strip()
                return ConfirmationResponse(clarification=answer) if answer else ConfirmationResponse()
            prompt_label = _approval_prompt_label(_approval_policy_for_request(request))
            answer = ui.input(f"[bold yellow]  Allow? {prompt_label}[/bold yellow] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ConfirmationResponse()

        return _approval_response_from_answer(answer, _approval_policy_for_request(request))

    # ── User-turn loop ───────────────────────────────────────────────────────
    while not state.should_exit:
        try:
            raw_input = ui.prompt_user().strip()
        except KeyboardInterrupt:
            ui.print_muted("Use /quit to exit.")
            continue
        except EOFError:
            break
        if not raw_input:
            continue
        if await router.dispatch(state, raw_input):
            continue

        effective_prompt, resumed_paused_turn = state.consume_turn_prompt(raw_input)
        if resumed_paused_turn:
            ui.print_muted("Resuming paused task…")

        if state.hooks is not None:
            state.current_turn_id = uuid4().hex[:12]
            state.current_trace_id = uuid4().hex
            await state.hooks.emit(
                HookEvent.USER_PROMPT_SUBMIT,
                {
                    "prompt": raw_input,
                    "session_id": state.session.session_id,
                    "turn_id": state.current_turn_id,
                    "trace_id": state.current_trace_id,
                    "mode": state.mode.value,
                    "effective_prompt": effective_prompt,
                    "resumed_paused_turn": resumed_paused_turn,
                },
            )
        state.history.append(Message(role="user", content=raw_input))
        if not resumed_paused_turn:
            state.approval_manager.begin_turn()
        try:
            # Hand off to the AI turn — model + tools run inside run_agent_turn.
            events = await run_agent_turn(
                state,
                agent,
                prompt_text=effective_prompt,
                ui=ui,
                approval_callback=ask_for_approval,
            )
        except Exception as exc:  # noqa: BLE001
            from nexus.app import provider_error_message

            ui.print_error(provider_error_message(exc, state.config))
            # Remove the user message we just added so it doesn't corrupt history
            state.history.pop()
            continue
        state.apply_events(events)
        state.current_turn_id = ""
        state.current_trace_id = ""

    if state.hooks is not None:
        await state.hooks.emit(
            HookEvent.STOP,
            {
                "session_id": state.session.session_id,
                "turn_id": state.current_turn_id,
                "trace_id": state.current_trace_id,
                "message_count": len(state.history),
            },
        )
    state.session_store.save(state.session)


# ---------------------------------------------------------------------------
# Approval helpers
# ---------------------------------------------------------------------------

def _approval_scope_for_policy(policy: ApprovalPolicy) -> ApprovalScope:
    if policy is ApprovalPolicy.APPROVE_TURN:
        return ApprovalScope.TURN
    if policy is ApprovalPolicy.APPROVE_SESSION:
        return ApprovalScope.SESSION
    return ApprovalScope.ONCE


def _approval_prompt_label(policy: ApprovalPolicy) -> str:
    if policy is ApprovalPolicy.APPROVE_TURN:
        return "(y)es for this turn / (N)o:"
    if policy is ApprovalPolicy.APPROVE_SESSION:
        return "(y)es for this session / (N)o:"
    return "(y)es once / yes (t)urn / (N)o:"


def _approval_policy_for_request(request: ConfirmationRequest) -> ApprovalPolicy:
    return ApprovalPolicy(str(request.payload.get("approval_policy", ApprovalPolicy.ON_REQUEST.value)))


def _approval_response_from_answer(answer: str, policy: ApprovalPolicy) -> ConfirmationResponse:
    normalized = _normalize_approval_answer(answer)
    if normalized in {"y", "yes", "once", "yes once"}:
        return ConfirmationResponse(approved=True, scope=_approval_scope_for_policy(policy).value)
    if policy is ApprovalPolicy.ON_REQUEST and normalized in {"t", "turn", "yes turn", "yes t"}:
        return ConfirmationResponse(approved=True, scope=ApprovalScope.TURN.value)
    return ConfirmationResponse()


def _normalize_approval_answer(answer: str) -> str:
    normalized = answer.strip().lower()
    for token in ("(", ")", "-", "_"):
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


def _record_approval_response(
    state: ReplState,
    request: ConfirmationRequest,
    response: ConfirmationResponse,
) -> None:
    scope = ApprovalScope(response.scope or _approval_scope_for_policy(state.approval_manager.policy).value)
    request_policy = _approval_policy_for_request(request)
    if scope is ApprovalScope.TURN and request_policy is ApprovalPolicy.ON_REQUEST:
        if _supports_turn_wide_approval(request):
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


def _supports_turn_wide_approval(request: ConfirmationRequest) -> bool:
    risk_level = str(request.payload.get("risk_level", "medium")).strip().lower().split(".")[-1]
    return not (request.tool_name == "bash" and risk_level in {"high", "dangerous"})


# ---------------------------------------------------------------------------
# Paused-turn and telemetry helpers
# ---------------------------------------------------------------------------

def _sync_paused_turn_state(state: ReplState, events: list[AgentEvent], *, prompt_text: str) -> None:
    if _turn_finished_with_tool_call_limit(events):
        state.mark_paused_turn(prompt_text)
        return
    state.clear_paused_turn()


def _turn_finished_with_tool_call_limit(events: list[AgentEvent]) -> bool:
    turn_completed = next(
        (event for event in reversed(events) if event.kind == AgentEventType.TURN_COMPLETED),
        None,
    )
    return bool(turn_completed and turn_completed.payload == "tool_call_limit")


def _record_turn_telemetry(state: ReplState, events: list, *, turn_id: str, trace_id: str, duration_ms: float, status: str) -> None:
    usage = None
    tool_calls = 0
    for event in events:
        if event.kind == "model_response" and event.payload.usage is not None:
            usage = event.payload.usage
        elif event.kind == "tool_call_requested":
            tool_calls += 1
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


# ---------------------------------------------------------------------------
# History helpers (used by confirmation retry logic)
# ---------------------------------------------------------------------------

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

