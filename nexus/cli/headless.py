from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from nexus.ui import TerminalUI

from nexus.models import ConfirmationKind, Message
from nexus.hooks import HookEvent
from nexus.runtime.repl import collect_turn_events, prompt_for_confirmation
from nexus.runtime.repl_state import ReplState


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_CONFIRM = 3


@dataclass(slots=True)
class HeadlessResult:
    exit_code: int
    response: str
    history: list[Message] = field(default_factory=list)
    error: str | None = None


async def run_headless(
    state: ReplState,
    agent,
    prompt: str,
    *,
    auto_confirm: bool,
    output_path: Path | None,
    output_format: str,
    quiet: bool,
) -> HeadlessResult:
    approval_callback = _headless_approval_callback() if _can_prompt_for_confirmation() else None
    state.current_turn_id = uuid4().hex[:12]
    state.current_trace_id = uuid4().hex
    if state.hooks is not None:
        await state.hooks.emit(
            HookEvent.USER_PROMPT_SUBMIT,
            {
                "prompt": prompt,
                "session_id": state.session.session_id,
                "turn_id": state.current_turn_id,
                "trace_id": state.current_trace_id,
                "mode": state.mode.value,
                "headless": True,
            },
        )
    state.history.append(Message(role="user", content=prompt))
    try:
        events = await collect_turn_events(
            state,
            agent,
            prompt_text=prompt,
            approval_callback=approval_callback,
            auto_confirm=auto_confirm,
        )
    except Exception as exc:  # noqa: BLE001
        from nexus.app import _provider_error_message
        state.console.print_error(_provider_error_message(exc, state.config))
        return HeadlessResult(
            exit_code=EXIT_ERROR,
            response="",
            history=list(state.history),
            error=str(exc),
        )
    confirmation = next((event for event in events if event.kind == "confirmation_requested"), None)
    if confirmation is not None and approval_callback is None and (
        confirmation.payload.kind is ConfirmationKind.CLARIFICATION or not auto_confirm
    ):
        return HeadlessResult(
            exit_code=EXIT_NEEDS_CONFIRM,
            response="",
            history=list(state.history),
            error=confirmation.payload.prompt,
        )

    if not quiet and output_format == "text":
        state.console.render_events(
            events,
            stream_output=state.config.stream_output,
            show_tool_calls=state.config.show_tool_calls,
        )
    state.apply_events(events)
    if state.hooks is not None:
        await state.hooks.emit(
            HookEvent.STOP,
            {
                "session_id": state.session.session_id,
                "turn_id": state.current_turn_id,
                "trace_id": state.current_trace_id,
                "message_count": len(state.history),
                "headless": True,
            },
        )
    state.current_turn_id = ""
    state.current_trace_id = ""
    response = _last_assistant_response(state.history)
    body = _format_output(response, state.history, output_format)
    _write_output(
        body,
        output_path,
        console=state.console,
        emit_to_stdout=output_format != "text" or quiet,
    )
    return HeadlessResult(exit_code=EXIT_OK, response=response, history=list(state.history))


def _format_output(
    response: str,
    history: list[Message],
    output_format: str,
) -> str:
    if output_format == "json":
        return json.dumps({"response": response}, indent=2)
    if output_format == "jsonl":
        return "\n".join(json.dumps({"role": item.role, "content": item.content}) for item in history)
    return response


def _write_output(
    body: str,
    output_path: Path | None,
    *,
    console: TerminalUI,
    emit_to_stdout: bool,
) -> None:
    if output_path is not None:
        output_path.write_text(body, encoding="utf-8")
        return
    if emit_to_stdout:
        console.print(body)


def _last_assistant_response(history: list[Message]) -> str:
    for message in reversed(history):
        if message.role == "assistant" and message.content:
            return message.content
    return ""


def _can_prompt_for_confirmation() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _headless_approval_callback():
    async def ask_for_approval(request):
        return prompt_for_confirmation(request)

    return ask_for_approval

