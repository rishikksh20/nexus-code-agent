from __future__ import annotations

import time
import readline  # noqa: F401 — activates arrow-key / history line editing for input() on macOS
from uuid import uuid4

from collections.abc import Awaitable, Callable
from typing import cast

from nexus.models import AgentEvent, ConfirmationKind, ConfirmationRequest, ConfirmationResponse, Message, ToolExecutionContext
from nexus.prompts import build_context_sections
from nexus.runtime.agent import Agent
from nexus.runtime.context import ContextBuilder, ContextCompactor, TokenEstimator, prune_tool_outputs
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
    collected_events: list[AgentEvent] = []
    turn_id = state.current_turn_id or uuid4().hex[:12]
    trace_id = state.current_trace_id or uuid4().hex
    started_at = time.perf_counter()

    while True:
        state.current_system_prompt = _build_system_prompt(state, prompt_text)
        compactor = ContextCompactor(
            TokenEstimator(),
            state.config.compaction_soft_limit,
            state.config.compaction_hard_limit,
        )
        model_messages = list(state.history)
        if state.config.context_prune_enabled:
            prune_tool_outputs(
                model_messages,
                protect_tokens=state.config.context_prune_protect_tokens,
                minimum_tokens=state.config.context_prune_minimum_tokens,
            )
        if compactor.should_compact(model_messages):
            model_messages, state.carry_over = compactor.compact(
                model_messages,
                state.carry_over,
                keep_recent=state.config.compaction_keep_recent,
            )
        context = ToolExecutionContext(
            session_id=state.session.session_id,
            working_directory=state.config.workspace_root,
            metadata={
                "turn_id": turn_id,
                "trace_id": trace_id,
                "active_skills": list(state.active_skills),
            },
        )
        events: list[AgentEvent] = [
            event
            async for event in agent.run(
                model_messages,
                context,
                system_prompt=state.current_system_prompt,
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
        collected_events.extend(events)

        confirmation = next((event for event in events if event.kind == "confirmation_requested"), None)
        if confirmation is None:
            _record_turn_telemetry(state, events, turn_id=turn_id, trace_id=trace_id, duration_ms=(time.perf_counter() - started_at) * 1000, status="completed")
            return collected_events
        confirmation_request = cast(ConfirmationRequest, confirmation.payload)

        if auto_confirm and confirmation_request.kind is ConfirmationKind.APPROVAL:
            state.approval_manager.record_approval(confirmation_request.tool_name, ApprovalScope.TURN)
            continue

        if approval_callback is None:
            _record_turn_telemetry(state, collected_events, turn_id=turn_id, trace_id=trace_id, duration_ms=(time.perf_counter() - started_at) * 1000, status="awaiting_confirmation")
            return collected_events

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
        _record_turn_telemetry(state, collected_events, turn_id=turn_id, trace_id=trace_id, duration_ms=(time.perf_counter() - started_at) * 1000, status="stopped")
        return collected_events

    return collected_events


def apply_events_to_history(state: ReplState, events: list) -> None:
    for event in events:
        if event.kind == "model_response":
            state.history.append(event.payload.message)
            if event.payload.usage is not None:
                _accumulate_usage(state, event.payload.usage)
        elif event.kind == "tool_result":
            state.history.append(
                Message(
                    role="tool",
                    content=event.payload.output,
                    name=event.payload.tool_name,
                    tool_call_id=event.payload.call_id,
                )
            )
    state.session.messages = list(state.history)
    if not state.session.summary:
        first_user = next((message.content for message in state.history if message.role == "user"), "")
        state.session.summary = first_user
    if state.config.save_on_every_turn:
        state.session_store.save(state.session)


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
    all_events: list[AgentEvent] = []
    turn_id = state.current_turn_id or uuid4().hex[:12]
    trace_id = state.current_trace_id or uuid4().hex
    started_at = time.perf_counter()

    while True:
        state.current_system_prompt = _build_system_prompt(state, prompt_text)
        compactor = ContextCompactor(
            TokenEstimator(),
            state.config.compaction_soft_limit,
            state.config.compaction_hard_limit,
        )
        model_messages = list(state.history)
        if state.config.context_prune_enabled:
            prune_tool_outputs(
                model_messages,
                protect_tokens=state.config.context_prune_protect_tokens,
                minimum_tokens=state.config.context_prune_minimum_tokens,
            )
        if compactor.should_compact(model_messages):
            model_messages, state.carry_over = compactor.compact(
                model_messages,
                state.carry_over,
                keep_recent=state.config.compaction_keep_recent,
            )
        context = ToolExecutionContext(
            session_id=state.session.session_id,
            working_directory=state.config.workspace_root,
            metadata={
                "turn_id": turn_id,
                "trace_id": trace_id,
                "active_skills": list(state.active_skills),
            },
        )

        batch: list[AgentEvent] = []
        async for event in agent.run(
            model_messages,
            context,
            system_prompt=state.current_system_prompt,
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

        all_events.extend(batch)
        confirmation = next((e for e in batch if e.kind == "confirmation_requested"), None)

        if confirmation is None:
            _record_turn_telemetry(
                state, all_events, turn_id=turn_id, trace_id=trace_id,
                duration_ms=(time.perf_counter() - started_at) * 1000, status="completed",
            )
            return all_events

        confirmation_request = cast(ConfirmationRequest, confirmation.payload)

        if auto_confirm and confirmation_request.kind is ConfirmationKind.APPROVAL:
            state.approval_manager.record_approval(confirmation_request.tool_name, ApprovalScope.TURN)
            continue

        if approval_callback is None:
            _record_turn_telemetry(
                state, all_events, turn_id=turn_id, trace_id=trace_id,
                duration_ms=(time.perf_counter() - started_at) * 1000, status="awaiting_confirmation",
            )
            return all_events

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
        _record_turn_telemetry(
            state, all_events, turn_id=turn_id, trace_id=trace_id,
            duration_ms=(time.perf_counter() - started_at) * 1000, status="stopped",
        )
        return all_events


async def run_repl(state: ReplState, agent: Agent, router, *, session_resumed: bool = False) -> None:
    ui: TerminalUI = state.console
    cfg = state.config
    ui.print_banner(cfg.provider, cfg.model_name, cfg.default_mode)
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
        raw_input = input("> ").strip()
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
            from nexus.app import _provider_error_message
            ui.print_error(_provider_error_message(exc, state.config))
            # Remove the user message we just added so it doesn't corrupt history
            state.history.pop()
            continue
        apply_events_to_history(state, events)
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
    sections = build_context_sections(
        state.config,
        state.tool_registry,
        task_input=prompt_text,
        execution_mode=state.mode.value,
        skill_registry=state.skill_registry,
        active_skills=state.active_skills,
        carry_over=state.carry_over,
    )
    memory_matches = state.memory_store.search(prompt_text)
    if memory_matches:
        sections.project_notes.extend(entry.content for entry in memory_matches[:3])
    return ContextBuilder().build(sections)


def _accumulate_usage(state: ReplState, usage) -> None:
    summary = state.session.metadata.setdefault(
        "usage",
        {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
        },
    )
    summary["prompt_tokens"] += usage.prompt_tokens
    summary["completion_tokens"] += usage.completion_tokens
    summary["total_tokens"] += usage.total_tokens
    summary["estimated_cost_usd"] = round(
        summary["estimated_cost_usd"] + usage.estimated_cost_usd,
        6,
    )


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
