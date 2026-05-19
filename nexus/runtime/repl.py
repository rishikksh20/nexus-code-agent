from __future__ import annotations

import readline  # noqa: F401 - activates arrow-key / history line editing for input()
from os import environ
from uuid import uuid4

from nexus.hooks import HookEvent
from nexus.cli.init import _local_config_toml
from nexus.config import load_config
from nexus.config.upgrade import inspect_config_upgrade, upgrade_config_file
from nexus.models import ConfirmationKind, ConfirmationRequest, ConfirmationResponse, Message
from nexus.runtime.agent import Agent
from nexus.runtime.repl_state import ReplState
from nexus.runtime.turn_runner import (
    ConfirmationCallback,
    approval_policy_for_request,
    approval_prompt_label,
    approval_response_from_answer,
    collect_turn_events,
    prompt_for_confirmation,
    run_agent_turn,
)
from nexus.runtime.orchestration import run_orchestrated_turn
from nexus.ui import TerminalUI


__all__ = ["collect_turn_events", "prompt_for_confirmation", "run_agent_turn", "run_repl"]


async def run_repl(state: ReplState, agent: Agent, router, *, session_resumed: bool = False) -> None:
    """Interactive user-turn loop.

    The REPL owns terminal setup, prompt reading, slash-command dispatch, and
    interactive approval input. Shared model/tool turn execution lives in
    :mod:`nexus.runtime.turn_runner`.
    """
    ui: TerminalUI = state.console
    if _should_run_textual_ui(state):
        # Keep one-off migration prompts line-oriented so config writes happen before
        # Textual takes over the terminal. The main chat loop runs in the TUI.
        _maybe_prompt_config_upgrade(state)
        from nexus.ui.textual_app import run_textual_repl

        await run_textual_repl(state, agent, router, session_resumed=session_resumed)
        return

    cfg = state.config
    ui.print_banner(cfg.provider, cfg.model_name, state.mode.value, workspace=cfg.workspace_root)
    if session_resumed:
        ui.print_session_resumed(state.session.session_id, len(state.history))
    if state.has_paused_turn():
        ui.print_muted("A previous task was paused after hitting the tool-call limit. Type `continue` to resume it, or enter a new prompt to start something else.")
    ui.print_help_hint()
    _print_provider_notice_or_warning(state)
    _maybe_prompt_config_upgrade(state)

    approval_callback = _interactive_approval_callback(ui)

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
            ui.print_muted("Resuming paused task...")

        await _emit_prompt_submit(
            state,
            raw_input,
            effective_prompt=effective_prompt,
            resumed_paused_turn=resumed_paused_turn,
        )
        state.history.append(Message(role="user", content=raw_input))
        if not resumed_paused_turn:
            state.approval_manager.begin_turn()
        try:
            events = await run_orchestrated_turn(
                state,
                agent,
                prompt_text=effective_prompt,
                ui=ui,
                approval_callback=approval_callback,
            )
        except Exception as exc:  # noqa: BLE001
            from nexus.app import provider_error_message

            ui.print_error(provider_error_message(exc, state.config))
            state.history.pop()
            continue
        state.apply_events(events)
        state.current_turn_id = ""
        state.current_trace_id = ""

    await _emit_stop(state)
    state.session_store.save(state.session)


def _should_run_textual_ui(state: ReplState) -> bool:
    try:
        from nexus.ui.textual_app import can_use_textual_ui
    except Exception:  # noqa: BLE001
        return False
    return can_use_textual_ui(state.config)


def _interactive_approval_callback(ui: TerminalUI) -> ConfirmationCallback:
    async def ask_for_approval(request: ConfirmationRequest) -> ConfirmationResponse:
        try:
            if request.kind is ConfirmationKind.CLARIFICATION:
                field = request.payload.get("field", "value")
                answer = ui.input(f"  [bold]Value for [cyan]{field!r}[/cyan]:[/bold] ").strip()
                return ConfirmationResponse(clarification=answer) if answer else ConfirmationResponse()
            policy = approval_policy_for_request(request)
            answer = ui.input(f"[bold yellow]  Allow? {approval_prompt_label(policy)}[/bold yellow] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ConfirmationResponse()

        return approval_response_from_answer(answer, approval_policy_for_request(request))

    return ask_for_approval


def _print_provider_notice_or_warning(state: ReplState) -> None:
    cfg = state.config
    if cfg.provider == "fake":
        state.console.print_fake_provider_notice()
        return
    if cfg.provider == "ollama":
        return
    if cfg.provider in {"mistral", "openai", "openai-compatible"}:
        has_key = bool(
            cfg.api_key
            or environ.get("MISTRAL_API_KEY")
            or environ.get("NEXUS_API_KEY")
            or environ.get("OPENAI_API_KEY")
            or environ.get("API_KEY")
        )
    elif cfg.provider in {"anthropic", "gemini"}:
        has_key = bool(
            cfg.api_key
            or environ.get("ANTHROPIC_API_KEY")
            or environ.get("GEMINI_API_KEY")
            or environ.get("GOOGLE_API_KEY")
            or environ.get("API_KEY")
        )
    else:
        has_key = True
    if not has_key:
        state.console.print_no_api_key_warning(cfg.provider)


def _maybe_prompt_config_upgrade(state: ReplState) -> None:
    cfg = state.config
    template = _local_config_toml(
        workspace_root=cfg.workspace_root,
        project_name=cfg.project_name or cfg.workspace_root.name,
        project_description=cfg.project_description,
    )
    report = inspect_config_upgrade(cfg.local_config_file, template)
    if not report.needs_upgrade:
        return

    state.console.print("[bold yellow]Workspace Nexus config can be upgraded for this build.[/bold yellow]")
    if report.current_version != report.target_version:
        state.console.print(f"  version: {report.current_version or 'missing'} -> {report.target_version}")
    for key in report.deprecated_keys:
        state.console.print(f"  remove deprecated key: {key}")
    if report.missing_keys:
        state.console.print(f"  add missing key(s): {', '.join(report.missing_keys)}")
    state.console.print("Options: [bold]yes[/bold]/[bold]y[/bold], [bold]no[/bold]/[bold]n[/bold], or [bold]later[/bold].")
    try:
        answer = state.console.input("Upgrade workspace .nexus/config.toml now? ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        state.console.print_muted("Skipping config upgrade prompt.")
        return
    if answer in {"", "n", "no", "later"}:
        state.console.print_muted("Skipping config upgrade. You can run /config upgrade local later.")
        return
    if answer not in {"y", "yes"}:
        state.console.print_muted("Unrecognized answer. Skipping config upgrade. You can run /config upgrade local later.")
        return
    upgrade_config_file(cfg.local_config_file, template)
    state.config = load_config(
        cfg.workspace_root,
        global_root=cfg.global_root,
        local_config_path=cfg.local_config_file,
        global_config_path=cfg.global_config_file,
        strict=False,
    )
    for warning in getattr(state.config, "config_warnings", []) or []:
        state.console.print_warning(warning)
    state.console.print("[green]Workspace config upgraded and reloaded.[/green]")


async def _emit_prompt_submit(
    state: ReplState,
    raw_input: str,
    *,
    effective_prompt: str,
    resumed_paused_turn: bool,
) -> None:
    if state.hooks is None:
        return
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


async def _emit_stop(state: ReplState) -> None:
    if state.hooks is None:
        return
    await state.hooks.emit(
        HookEvent.STOP,
        {
            "session_id": state.session.session_id,
            "turn_id": state.current_turn_id,
            "trace_id": state.current_trace_id,
            "message_count": len(state.history),
        },
    )
