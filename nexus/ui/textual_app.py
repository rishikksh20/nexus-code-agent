"""Textual-powered interactive terminal UI for Nexus."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from rich import box
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog, Static

from nexus.hooks import HookEvent
from nexus.models import (
    AgentEventType,
    ConfirmationKind,
    ConfirmationRequest,
    ConfirmationResponse,
    Message,
)
from nexus.runtime.orchestration import run_orchestrated_turn
from nexus.runtime.turn_runner import (
    ConfirmationCallback,
    approval_policy_for_request,
    approval_prompt_label,
    approval_response_from_answer,
)
from nexus.ui.terminal import NEXUS_THEME, TerminalUI, _solid_ascii_banner

if TYPE_CHECKING:
    from rich.console import RenderableType

    from nexus.models import AgentEvent, ToolResult
    from nexus.runtime.agent import Agent
    from nexus.runtime.repl_state import ReplState
    from nexus.runtime.slash_commands import SlashCommandRouter


_MOUSE_ESCAPE_RE = re.compile(
    r"(?:\x1b)?\[(?:<\d{1,4};\d{1,5};\d{1,5}[mM]|M.{3})"
)


def _strip_mouse_escape_sequences(value: str) -> str:
    """Remove leaked terminal mouse reports from input text."""
    return _MOUSE_ESCAPE_RE.sub("", value)


def can_use_textual_ui(config: Any) -> bool:
    """Return True when the Textual app should own the interactive session."""
    if not bool(getattr(config, "textual_ui", True)):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


async def run_textual_repl(
    state: ReplState,
    agent: Agent,
    router: SlashCommandRouter,
    *,
    session_resumed: bool = False,
) -> None:
    """Run the interactive REPL inside a Textual application."""
    app = NexusTextualApp(
        state,
        agent,
        router,
        session_resumed=session_resumed,
    )
    try:
        await app.run_async(mouse=True)
    finally:
        await app.finalize_session()


class TextualTerminalUI(TerminalUI):
    """TerminalUI-compatible adapter that writes Rich renderables to Textual."""

    def __init__(self, app: "NexusTextualApp") -> None:
        super().__init__(color=True)
        self._app = app

    def _write(self, renderable: RenderableType) -> None:
        self._app.write(renderable)

    def print(self, *args: Any, **kwargs: Any) -> None:
        if not args:
            self._write(Text(""))
            return
        if len(args) == 1 and not isinstance(args[0], str):
            self._write(cast("RenderableType", args[0]))
            return
        sep = str(kwargs.get("sep", " "))
        markup = bool(kwargs.get("markup", True))
        text = sep.join(str(arg) for arg in args)
        self._write(Text.from_markup(text) if markup else Text(text))

    def input(self, prompt: str = "") -> str:
        raise RuntimeError("TextualTerminalUI input is asynchronous; use NexusTextualApp.ask().")

    def prompt_user(self) -> str:
        raise RuntimeError("TextualTerminalUI prompt_user is handled by the Textual input widget.")

    def print_error(self, msg: str) -> None:
        self.end_assistant()
        self._write(
            Panel(
                Text(msg, style="error"),
                title=Text("Request failed", style="error"),
                title_align="left",
                border_style="red",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

    def print_warning(self, msg: str) -> None:
        self._write(Text.from_markup(f"[bold yellow]Warning:[/bold yellow] {msg}"))

    def print_success(self, msg: str) -> None:
        self._write(Text(msg, style="bold green"))

    def print_info(self, msg: str) -> None:
        self._write(Text(msg, style="cyan"))

    def print_muted(self, msg: str) -> None:
        self._write(Text(msg, style="dim"))

    def print_rule(self, title: str = "", *, style: str = "border") -> None:
        del style
        self._write(Rule(title))

    def print_markdown(self, content: str) -> None:
        self._write(Markdown(content))

    def print_banner(
        self,
        provider: str,
        model: str,
        mode: str,
        *,
        workspace: str | Path | None = None,
    ) -> None:
        self._workspace_root = Path(workspace).resolve() if workspace is not None else None
        body = Table.grid(expand=True)
        body.add_column(style="dim", width=12)
        body.add_column(style="bold cyan")
        body.add_row("Provider", provider)
        body.add_row("Model", model)
        body.add_row("Mode", mode)
        if workspace is not None:
            body.add_row("Workspace", str(Path(workspace).resolve()))
        body.add_row("Quick help", "/help  |  /skills  |  /session  |  /quit")
        self._write(
            Panel(
                Group(_solid_ascii_banner(), "", body),
                title=Text("Nexus Coding Agent", style="bold white"),
                title_align="left",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

    def print_session_resumed(self, session_id: str, msg_count: int) -> None:
        noun = "message" if msg_count == 1 else "messages"
        self._write(
            Panel(
                Text(
                    f"Resumed session {session_id} with {msg_count} {noun}. "
                    "Use /session new to start fresh or /session list to switch.",
                    style="dim",
                ),
                title=Text("Session", style="cyan"),
                title_align="left",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )

    def print_help_hint(self) -> None:
        self._write(Text("Type /help for commands, /skills for skill control, or /quit to exit.", style="dim"))

    def print_fake_provider_notice(self) -> None:
        self._write(
            Panel(
                Text(
                    "Using the fake provider. Responses are mocked; set a real provider, API_KEY, and BASE_URL in .env for live coding-agent responses.",
                    style="yellow",
                ),
                title=Text("Provider notice", style="bold yellow"),
                title_align="left",
                border_style="yellow",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )

    def print_no_api_key_warning(self, provider: str) -> None:
        self._write(
            Panel(
                Text(
                    f"No API key found for provider {provider}. Add API_KEY to .env or configure a provider-specific key before starting a live session.",
                    style="yellow",
                ),
                title=Text("Provider setup required", style="bold yellow"),
                title_align="left",
                border_style="yellow",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )

    def begin_assistant(self) -> None:
        if self._assistant_stream_open:
            return
        self._write(Rule(Text("Assistant", style="bold white")))
        self._assistant_stream_open = True

    def end_assistant(self) -> None:
        self._assistant_stream_open = False

    def start_thinking(self, label: str = "Thinking") -> None:
        self._app.set_status(label)

    def stop_thinking(self) -> None:
        self._app.clear_status()

    def start_tool_wait(self, label: str) -> None:
        self._app.set_status(label)

    def stop_tool_wait(self) -> None:
        self._app.clear_status()

    def render_event(
        self,
        event: AgentEvent,
        *,
        stream_output: bool,
        show_tool_calls: bool,
        show_thinking_indicator: bool = True,
    ) -> None:
        del stream_output

        if event.kind == AgentEventType.AGENT_START:
            return

        if event.kind == AgentEventType.THINKING_STARTED and show_thinking_indicator:
            self.end_assistant()
            self.start_thinking(_thinking_label(event))
            return

        if event.kind == AgentEventType.TEXT_DELTA:
            if event.payload:
                self._app.append_assistant_delta(str(event.payload))
            return

        if event.kind == AgentEventType.TEXT_COMPLETE:
            content = str(event.payload or "")
            if content and not self._app.has_open_assistant_stream:
                self.begin_assistant()
                self._write(Markdown(content))
            self._app.close_assistant_stream()
            self.end_assistant()
            return

        if event.kind == AgentEventType.TOOL_CALL_START and show_tool_calls:
            self.end_assistant()
            payload = event.payload or {}
            call_id = str(payload.get("call_id", ""))
            tool_name = str(payload.get("name", "tool"))
            actor = str(payload.get("actor", "") or "").strip()
            display_name = self._tool_display_name(tool_name, actor)
            arguments = payload.get("arguments", {}) if isinstance(payload.get("arguments", {}), dict) else {}
            preview = payload.get("preview", {}) if isinstance(payload.get("preview", {}), dict) else {}
            self._tool_args_by_call_id[call_id] = dict(arguments)
            self._tool_preview_by_call_id[call_id] = dict(preview)
            if actor:
                self._tool_actor_by_call_id[call_id] = actor
            self._write(
                Panel(
                    self._render_tool_panel_body(display_name, arguments, preview=preview),
                    title=Text(f"{display_name}  #{call_id[:8] or 'pending'}", style="bold magenta"),
                    title_align="left",
                    subtitle=Text("running", style="dim"),
                    subtitle_align="right",
                    border_style=self._tool_border_style(tool_name),
                    box=box.ROUNDED,
                    padding=(1, 2),
                )
            )
            self.start_tool_wait(f"{display_name} running")
            return

        if event.kind == AgentEventType.TOOL_CALL_COMPLETE and show_tool_calls:
            self.end_assistant()
            result = cast("ToolResult", event.payload)
            if result is None:
                return
            preview = self._tool_preview_by_call_id.get(result.call_id, {})
            actor = str(result.metadata.get("actor") or self._tool_actor_by_call_id.get(result.call_id, "")).strip()
            display_name = self._tool_display_name(result.tool_name, actor)
            self._write(
                Panel(
                    self._render_tool_result_body(result, preview=preview),
                    title=Text(f"{display_name}  #{result.call_id[:8]}", style="bold magenta"),
                    title_align="left",
                    subtitle=Text("failed" if result.is_error else "done", style="red" if result.is_error else "green"),
                    subtitle_align="right",
                    border_style=self._tool_border_style(result.tool_name),
                    box=box.ROUNDED,
                    padding=(1, 2),
                )
            )
            self._tool_args_by_call_id.pop(result.call_id, None)
            self._tool_preview_by_call_id.pop(result.call_id, None)
            self._tool_actor_by_call_id.pop(result.call_id, None)
            self.start_thinking()
            return

        if event.kind == AgentEventType.TOOL_DENIED:
            self.stop_tool_wait()
            self.end_assistant()
            reason = getattr(event.payload, "reason", str(event.payload))
            self._write(
                Panel(
                    Text(reason, style="bold red"),
                    title=Text("Tool denied", style="bold red"),
                    title_align="left",
                    border_style="red",
                    box=box.ROUNDED,
                    padding=(0, 2),
                )
            )
            return

        if event.kind == AgentEventType.CONFIRMATION_REQUESTED:
            self.stop_tool_wait()
            self.stop_thinking()
            self.end_assistant()
            req = cast("ConfirmationRequest", event.payload)
            if req.kind is ConfirmationKind.APPROVAL:
                self._tool_args_by_call_id[req.call_id] = {str(key): value for key, value in req.arguments.items()}
                self._tool_preview_by_call_id[req.call_id] = {str(key): value for key, value in req.preview.items()}
                actor = str(req.payload.get("actor", "") or "").strip()
                if actor:
                    self._tool_actor_by_call_id[req.call_id] = actor
                display_name = self._tool_display_name(req.tool_name, actor)
                self._write(
                    Panel(
                        self._render_tool_panel_body(
                            display_name,
                            req.arguments,
                            preview=req.preview,
                            reason=req.reason,
                            approval_policy=str(req.payload.get("approval_policy", "on-request")),
                        ),
                        title=Text(f"{display_name}  #{req.call_id[:8] or 'pending'}", style="bold magenta"),
                        title_align="left",
                        subtitle=Text("approval required", style="yellow"),
                        subtitle_align="right",
                        border_style=self._tool_border_style(req.tool_name),
                        box=box.ROUNDED,
                        padding=(1, 2),
                    )
                )
            else:
                actor = str(req.payload.get("actor", "") or "").strip()
                display_name = self._tool_display_name(req.tool_name, actor)
                self._write(
                    Panel(
                        self._render_tool_panel_body(
                            display_name,
                            req.arguments,
                            preview=req.preview,
                            clarification_prompt=req.prompt,
                            reason=req.reason,
                        ),
                        title=Text(f"{display_name}  #{req.call_id[:8] or 'pending'}", style="bold magenta"),
                        title_align="left",
                        subtitle=Text("clarification needed", style="cyan"),
                        subtitle_align="right",
                        border_style=self._tool_border_style(req.tool_name),
                        box=box.ROUNDED,
                        padding=(1, 2),
                    )
                )
            return

        if event.kind == AgentEventType.AGENT_ERROR:
            self.stop_thinking()
            self.stop_tool_wait()
            payload = event.payload or {}
            error = payload.get("error") if isinstance(payload, dict) else str(payload)
            self.print_error(str(error or "Unknown provider error."))
            return

        if event.kind in {
            AgentEventType.MODEL_RESPONSE,
            AgentEventType.TOOL_CALL_REQUESTED,
            AgentEventType.TOOL_RESULT,
        }:
            return

        if event.kind in {AgentEventType.TURN_COMPLETED, AgentEventType.AGENT_STOP}:
            self.stop_thinking()
            self.stop_tool_wait()
            return

    def stream_tool_output(
        self,
        call_id: str,
        tool_name: str,
        stream_name: str,
        chunk: str,
    ) -> None:
        if tool_name != "bash" or not chunk:
            return
        self._app.append_tool_output(call_id, stream_name, chunk)


class NexusTextualApp(App[None]):
    CSS = """
    Screen {
        background: $surface;
    }

    #transcript {
        height: 1fr;
        width: 100%;
        padding: 1 2;
        background: transparent;
        scrollbar-size: 1 1;
    }

    #status {
        height: auto;
        min-height: 1;
        padding: 0 2;
        color: $text-muted;
        background: transparent;
    }

    #prompt {
        height: 3;
        margin: 0 1 1 1;
        padding: 0 1;
        border: solid $primary;
        background: transparent;
    }

    #prompt:focus {
        border: solid $accent;
    }
    """
    TITLE = "Nexus"
    SUB_TITLE = "Coding Agent"
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+d", "quit", "Quit"),
    ]

    def __init__(
        self,
        state: ReplState,
        agent: Agent,
        router: SlashCommandRouter,
        *,
        session_resumed: bool = False,
    ) -> None:
        super().__init__()
        self.state = state
        self.agent = agent
        self.router = router
        self.session_resumed = session_resumed
        self.ui = TextualTerminalUI(self)
        self._original_console = state.console
        self._pending_input: asyncio.Future[str] | None = None
        self._busy = False
        self._assistant_buffer = ""
        self.has_open_assistant_stream = False
        self._session_finalized = False
        self._spinner_timer: Any = None
        self._spinner_frame = 0
        self._status_text = ""
        self._streaming_tool_outputs: set[str] = set()
        self._transcript: Any = None
        self._status: Any = None
        self._input: Any = None

    def compose(self) -> ComposeResult:
        yield RichLog(id="transcript", wrap=True, highlight=False, markup=False)
        yield Static("", id="status")
        yield Input(placeholder="Message Nexus or type /help", id="prompt")

    def on_mount(self) -> None:
        self._transcript = self.query_one("#transcript", RichLog)
        self._status = self.query_one("#status", Static)
        self._input = self.query_one("#prompt", Input)
        self.console.push_theme(NEXUS_THEME)
        self.state.console = self.ui
        self._render_startup()
        self._input.focus()

    def on_unmount(self) -> None:
        self.state.console = self._original_console

    def write(self, renderable: RenderableType) -> None:
        if self._transcript is None:
            return
        self._transcript.write(renderable)

    def set_status(self, message: str) -> None:
        self._status_text = message
        self._ensure_spinner()
        self._refresh_status()

    def _ensure_spinner(self) -> None:
        if self._spinner_timer is None:
            self._spinner_timer = self.set_interval(0.12, self._refresh_status)

    def _refresh_status(self) -> None:
        if self._status is not None:
            if not self._status_text:
                self._status.update("")
                return
            frames = "|/-\\"
            frame = frames[self._spinner_frame % len(frames)]
            self._spinner_frame += 1
            self._status.update(f"{frame} {self._status_text}...")

    def clear_status(self, expected: str | None = None) -> None:
        if expected is not None and self._status_text != expected:
            return
        self._status_text = ""
        if self._status is not None:
            self._status.update("")
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def append_assistant_delta(self, content: str) -> None:
        if not self.has_open_assistant_stream:
            self.ui.begin_assistant()
            self.has_open_assistant_stream = True
            self._assistant_buffer = ""
        self._assistant_buffer += content

    def close_assistant_stream(self) -> None:
        if not self.has_open_assistant_stream:
            return
        self.write(Markdown(self._assistant_buffer))
        self._assistant_buffer = ""
        self.has_open_assistant_stream = False

    def append_tool_output(self, call_id: str, stream_name: str, chunk: str) -> None:
        if call_id not in self._streaming_tool_outputs:
            self._streaming_tool_outputs.add(call_id)
            self.write(
                Panel(
                    Text("Live bash output", style="dim"),
                    title=Text(f"bash output  #{call_id[:8]}", style="bold magenta"),
                    title_align="left",
                    border_style="magenta",
                    box=box.ROUNDED,
                    padding=(0, 2),
                )
            )
        style = "red" if stream_name == "stderr" else "default"
        prefix = "[stderr] " if stream_name == "stderr" else ""
        self.write(Text(prefix + chunk.rstrip("\n"), style=style))

    async def on_input_submitted(self, event: Any) -> None:
        raw = _strip_mouse_escape_sequences(str(event.value or "")).strip()
        self._input.value = ""
        if self._pending_input is not None:
            pending = self._pending_input
            self._pending_input = None
            if not pending.done():
                pending.set_result(raw)
            self.clear_status()
            return
        if not raw:
            return
        if self._busy:
            self.ui.print_warning("A turn is already running.")
            return
        asyncio.create_task(self._handle_prompt(raw))

    def on_input_changed(self, event: Input.Changed) -> None:
        cleaned = _strip_mouse_escape_sequences(event.value)
        if cleaned == event.value:
            return
        event.stop()
        self._input.value = cleaned

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        if self._transcript is not None:
            self._transcript.scroll_down(animate=False)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        if self._transcript is not None:
            self._transcript.scroll_up(animate=False)

    async def ask(self, prompt: str) -> str:
        self.set_status(prompt)
        self._input.placeholder = prompt
        self._input.focus()
        self._pending_input = asyncio.get_running_loop().create_future()
        try:
            return await self._pending_input
        finally:
            self._pending_input = None
            self._input.placeholder = "Message Nexus or type /help"
            self.clear_status()

    async def action_quit(self) -> None:
        self.state.should_exit = True
        await self.finalize_session()
        self.exit()

    async def finalize_session(self) -> None:
        if self._session_finalized:
            return
        self._session_finalized = True
        await self._emit_stop()
        self.state.session_store.save(self.state.session)

    def _render_startup(self) -> None:
        cfg = self.state.config
        self.ui.print_banner(cfg.provider, cfg.model_name, self.state.mode.value, workspace=cfg.workspace_root)
        if self.session_resumed:
            self.ui.print_session_resumed(self.state.session.session_id, len(self.state.history))
        if self.state.has_paused_turn():
            self.ui.print_muted("A previous task was paused after hitting the tool-call limit. Type `continue` to resume it, or enter a new prompt to start something else.")
        self.ui.print_help_hint()
        self._print_provider_notice_or_warning()

    def _print_provider_notice_or_warning(self) -> None:
        from os import environ

        cfg = self.state.config
        if cfg.provider == "fake":
            self.ui.print_fake_provider_notice()
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
            self.ui.print_no_api_key_warning(cfg.provider)

    async def _handle_prompt(self, raw_input: str) -> None:
        self._busy = True
        self.set_status("Thinking")
        self.ui.print(
            Panel(
                Text(raw_input),
                title=Text("You", style="bold cyan"),
                title_align="left",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )
        try:
            if await self.router.dispatch(self.state, raw_input):
                if self.state.should_exit:
                    await self.action_quit()
                return

            effective_prompt, resumed_paused_turn = self.state.consume_turn_prompt(raw_input)
            if resumed_paused_turn:
                self.ui.print_muted("Resuming paused task...")
            await self._emit_prompt_submit(
                raw_input,
                effective_prompt=effective_prompt,
                resumed_paused_turn=resumed_paused_turn,
            )
            self.state.history.append(Message(role="user", content=raw_input))
            if not resumed_paused_turn:
                self.state.approval_manager.begin_turn()
            try:
                events = await run_orchestrated_turn(
                    self.state,
                    self.agent,
                    prompt_text=effective_prompt,
                    ui=self.ui,
                    approval_callback=self._approval_callback(),
                )
            except Exception as exc:  # noqa: BLE001
                from nexus.app import provider_error_message

                self.ui.print_error(provider_error_message(exc, self.state.config))
                self.state.history.pop()
                return
            self.state.apply_events(events)
            self.state.current_turn_id = ""
            self.state.current_trace_id = ""
        finally:
            self._busy = False
            self.clear_status()
            self._input.focus()

    def _approval_callback(self) -> ConfirmationCallback:
        async def ask_for_approval(request: ConfirmationRequest) -> ConfirmationResponse:
            if request.kind is ConfirmationKind.CLARIFICATION:
                field = request.payload.get("field", "value")
                answer = (await self.ask(f"Value for {field!r}:")).strip()
                return ConfirmationResponse(clarification=answer) if answer else ConfirmationResponse()
            policy = approval_policy_for_request(request)
            answer = (await self.ask(f"Allow? {approval_prompt_label(policy)}")).strip().lower()
            return approval_response_from_answer(answer, policy)

        return ask_for_approval

    async def _emit_prompt_submit(
        self,
        raw_input: str,
        *,
        effective_prompt: str,
        resumed_paused_turn: bool,
    ) -> None:
        if self.state.hooks is None:
            return
        self.state.current_turn_id = uuid4().hex[:12]
        self.state.current_trace_id = uuid4().hex
        await self.state.hooks.emit(
            HookEvent.USER_PROMPT_SUBMIT,
            {
                "prompt": raw_input,
                "session_id": self.state.session.session_id,
                "turn_id": self.state.current_turn_id,
                "trace_id": self.state.current_trace_id,
                "mode": self.state.mode.value,
                "effective_prompt": effective_prompt,
                "resumed_paused_turn": resumed_paused_turn,
            },
        )

    async def _emit_stop(self) -> None:
        if self.state.hooks is None:
            return
        await self.state.hooks.emit(
            HookEvent.STOP,
            {
                "session_id": self.state.session.session_id,
                "turn_id": self.state.current_turn_id,
                "trace_id": self.state.current_trace_id,
                "message_count": len(self.state.history),
            },
        )


def _thinking_label(event: Any) -> str:
    payload = getattr(event, "payload", None)
    actor = payload.get("actor") if isinstance(payload, dict) else ""
    actor = str(actor).strip() if actor else ""
    return f"{actor} - Thinking" if actor else "Thinking"
