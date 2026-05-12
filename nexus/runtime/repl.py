from __future__ import annotations

import time
import readline  # noqa: F401 — activates arrow-key / history line editing for input() on macOS
from uuid import uuid4

from collections.abc import Awaitable, Callable
from typing import cast

from nexus.models import AgentEvent, ConfirmationKind, ConfirmationRequest, ConfirmationResponse, Message
from nexus.runtime.agent import Agent
from nexus.context import ContextCompactor, TokenEstimator, prune_tool_outputs
from nexus.hooks import HookEvent
from nexus.runtime.repl_state import ReplState
from nexus.security.manager import ApprovalScope
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
        answer = input_reader(f"{request.prompt} [y/N]: ").strip().lower()
    except EOFError:
        return ConfirmationResponse()
    return ConfirmationResponse(approved=answer in {"y", "yes"})


async def collect_turn_events(
    state: ReplState,
    agent: Agent,
    *,
    prompt_text: str,
    approval_callback: ConfirmationCallback | None = None,
    auto_confirm: bool = False,
) -> list[AgentEvent]:
    committed_events: list[AgentEvent] = []
    turn_id = state.current_turn_id or uuid4().hex[:12]
    trace_id = state.current_trace_id or uuid4().hex
    started_at = time.perf_counter()

    while True:
        prepared_turn = state.prepare_turn(
            prompt_text,
            turn_id=turn_id,
            trace_id=trace_id,
            compactor_factory=ContextCompactor,
            estimator_factory=TokenEstimator,
            prune_outputs=prune_tool_outputs,
        )
        events: list[AgentEvent] = [
            event
            async for event in agent.run(
                prepared_turn.model_messages,
                prepared_turn.context,
                system_prompt=prepared_turn.system_prompt,
                model_name=state.config.model_name,
                mode=state.mode,
                approved_tools=state.approval_manager.get_approved_set(),
                auto_confirm=auto_confirm,
                auto_confirm_read_only=state.config.auto_confirm_read_only,
                temperature=state.config.temperature,
                max_output_tokens=state.config.max_output_tokens,
                max_turns=state.config.max_loop_iterations,
            )
        ]
        confirmation = next((event for event in events if event.kind == "confirmation_requested"), None)
        if confirmation is None:
            committed_events.extend(events)
            _record_turn_telemetry(state, committed_events, turn_id=turn_id, trace_id=trace_id, duration_ms=(time.perf_counter() - started_at) * 1000, status="completed")
            return committed_events
        confirmation_request = cast(ConfirmationRequest, confirmation.payload)

        if auto_confirm and confirmation_request.kind is ConfirmationKind.APPROVAL:
            state.approval_manager.record_approval(confirmation_request.tool_name, ApprovalScope.TURN)
            continue

        if approval_callback is None:
            pending_events = [*committed_events, *events]
            _record_turn_telemetry(state, pending_events, turn_id=turn_id, trace_id=trace_id, duration_ms=(time.perf_counter() - started_at) * 1000, status="awaiting_confirmation")
            return pending_events

        response = await approval_callback(confirmation_request)
        if confirmation_request.kind is ConfirmationKind.APPROVAL and response.approved:
            state.approval_manager.record_approval(confirmation_request.tool_name, ApprovalScope.ONCE)
            continue
        if confirmation_request.kind is ConfirmationKind.CLARIFICATION and response.clarification:
            clarification_text = (
                f"Clarification for {confirmation_request.tool_name} "
                f"({confirmation_request.payload.get('field', 'value')}): {response.clarification}"
            )
            state.history.append(Message(role="user", content=clarification_text))
            prompt_text = clarification_text
            continue
        pending_events = [*committed_events, *events]
        _record_turn_telemetry(state, pending_events, turn_id=turn_id, trace_id=trace_id, duration_ms=(time.perf_counter() - started_at) * 1000, status="stopped")
        return pending_events

    return committed_events


def apply_events_to_history(state: ReplState, events: list) -> None:
    state.apply_events(events)


def _render_event(ui: TerminalUI, event: AgentEvent, *, stream_output: bool, show_tool_calls: bool) -> None:
    """Render a single agent event — delegates to :class:`TerminalUI`."""
    ui.render_event(event, stream_output=stream_output, show_tool_calls=show_tool_calls)


def render_events(ui: TerminalUI, events: list, *, stream_output: bool, show_tool_calls: bool) -> None:
    """Render a list of agent events — delegates to :class:`TerminalUI`."""
    ui.render_events(events, stream_output=stream_output, show_tool_calls=show_tool_calls)


async def _stream_turn_live(
    state: ReplState,
    agent: Agent,
    prompt_text: str,
    ui: TerminalUI,
    *,
    approval_callback: ConfirmationCallback | None = None,
    auto_confirm: bool = False,
) -> list[AgentEvent]:
    """Run the agent turn, rendering each event via *ui* as it arrives.

    Returns all collected events so the caller can apply them to history.
    """
    committed_events: list[AgentEvent] = []
    turn_id = state.current_turn_id or uuid4().hex[:12]
    trace_id = state.current_trace_id or uuid4().hex
    started_at = time.perf_counter()

    while True:
        prepared_turn = state.prepare_turn(
            prompt_text,
            turn_id=turn_id,
            trace_id=trace_id,
            compactor_factory=ContextCompactor,
            estimator_factory=TokenEstimator,
            prune_outputs=prune_tool_outputs,
        )

        batch: list[AgentEvent] = []
        async for event in agent.run(
            prepared_turn.model_messages,
            prepared_turn.context,
            system_prompt=prepared_turn.system_prompt,
            model_name=state.config.model_name,
            mode=state.mode,
            approved_tools=state.approval_manager.get_approved_set(),
            auto_confirm=auto_confirm,
            auto_confirm_read_only=state.config.auto_confirm_read_only,
            temperature=state.config.temperature,
            max_output_tokens=state.config.max_output_tokens,
            max_turns=state.config.max_loop_iterations,
        ):
            _render_event(
                ui,
                event,
                stream_output=state.config.stream_output,
                show_tool_calls=state.config.show_tool_calls,
            )
            batch.append(event)

        confirmation = next((e for e in batch if e.kind == "confirmation_requested"), None)

        if confirmation is None:
            committed_events.extend(batch)
            _record_turn_telemetry(
                state, committed_events, turn_id=turn_id, trace_id=trace_id,
                duration_ms=(time.perf_counter() - started_at) * 1000, status="completed",
            )
            return committed_events

        confirmation_request = cast(ConfirmationRequest, confirmation.payload)

        if auto_confirm and confirmation_request.kind is ConfirmationKind.APPROVAL:
            state.approval_manager.record_approval(confirmation_request.tool_name, ApprovalScope.TURN)
            continue

        if approval_callback is None:
            pending_events = [*committed_events, *batch]
            _record_turn_telemetry(
                state, pending_events, turn_id=turn_id, trace_id=trace_id,
                duration_ms=(time.perf_counter() - started_at) * 1000, status="awaiting_confirmation",
            )
            return pending_events

        response = await approval_callback(confirmation_request)
        if confirmation_request.kind is ConfirmationKind.APPROVAL and response.approved:
            # scope is already recorded inside ask_for_approval; ONCE is the fallback.
            if not state.approval_manager.is_pre_approved(confirmation_request.tool_name):
                state.approval_manager.record_approval(confirmation_request.tool_name, ApprovalScope.ONCE)
            continue
        if confirmation_request.kind is ConfirmationKind.CLARIFICATION and response.clarification:
            clarification_text = (
                f"Clarification for {confirmation_request.tool_name} "
                f"({confirmation_request.payload.get('field', 'value')}): {response.clarification}"
            )
            state.history.append(Message(role="user", content=clarification_text))
            prompt_text = clarification_text
            continue
        pending_events = [*committed_events, *batch]
        _record_turn_telemetry(
            state, pending_events, turn_id=turn_id, trace_id=trace_id,
            duration_ms=(time.perf_counter() - started_at) * 1000, status="stopped",
        )
        return pending_events


async def run_repl(state: ReplState, agent: Agent, router, *, session_resumed: bool = False) -> None:
    ui: TerminalUI = state.console
    cfg = state.config
    ui.print_banner(cfg.provider, cfg.model_name, state.mode.value, workspace=cfg.workspace_root)
    if session_resumed:
        ui.print_session_resumed(state.session.session_id, len(state.history))
    ui.print_help_hint()
    if cfg.provider == "fake":
        ui.print_fake_provider_notice()
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
            # Scoped approval prompt: once / turn / session / no
            answer = ui.input(
                "[bold yellow]  Allow? \\[y]es (once) / \\[t]urn / \\[s]ession / \\[N]o:[/bold yellow] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ConfirmationResponse()

        if answer in {"y", "yes"}:
            state.approval_manager.record_approval(request.tool_name, ApprovalScope.ONCE)
            return ConfirmationResponse(approved=True)
        if answer in {"t", "turn"}:
            state.approval_manager.record_approval(request.tool_name, ApprovalScope.TURN)
            return ConfirmationResponse(approved=True)
        if answer in {"s", "session"}:
            state.approval_manager.record_approval(request.tool_name, ApprovalScope.SESSION)
            return ConfirmationResponse(approved=True)
        return ConfirmationResponse()

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
                },
            )
        state.history.append(Message(role="user", content=raw_input))
        state.approval_manager.begin_turn()
        try:
            events = await _stream_turn_live(
                state,
                agent,
                raw_input,
                ui,
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


def _build_system_prompt(state: ReplState, prompt_text: str) -> str:
    return state.build_system_prompt(prompt_text)


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
