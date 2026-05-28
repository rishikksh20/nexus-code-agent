"""Textual-powered interactive terminal UI for Nexus."""

from __future__ import annotations

import asyncio
from difflib import unified_diff
import io
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from rich import box
from rich.console import Console
from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Input, RichLog, Static

from nexus.config.model_limits import get_model_context_limit
from nexus.context import TokenEstimator
from nexus.hooks import HookEvent
from nexus.models import (
    AgentEventType,
    ConfirmationKind,
    ConfirmationRequest,
    ConfirmationResponse,
    Message,
    ToolResult,
)
from nexus.runtime.orchestration import run_orchestrated_turn
from nexus.runtime.turn_runner import (
    ConfirmationCallback,
    approval_policy_for_request,
    approval_prompt_label,
    approval_response_from_answer,
)
from nexus.security.policy import ApprovalPolicy
from nexus.ui.terminal import NEXUS_THEME, TerminalUI, _solid_ascii_banner

if TYPE_CHECKING:
    from rich.console import RenderableType

    from nexus.models import AgentEvent
    from nexus.runtime.agent import Agent
    from nexus.runtime.repl_state import ReplState
    from nexus.runtime.slash_commands import SlashCommandRouter


_MOUSE_ESCAPE_RE = re.compile(
    r"(?:\x1b)?\[(?:<\d{1,4};\d{1,5};\d{1,5}[mM]|M.{3})"
)
_RIGHT_MOUSE_BUTTON = 3
_COLLAPSED_PREVIEW_LINES = 15
_COLLAPSE_LINE_LIMIT = 18
_COLLAPSE_CHAR_LIMIT = 2400
_ALERT_PREVIEW_CHARS = 150
_SIDE_BY_SIDE_DIFF_WIDTH = 112
_MUTATING_FILE_TOOLS = {"write_file", "edit", "insert_edit_into_file", "apply_patch"}
_VERIFY_TOOL_NAMES = {"bash", "run_tests", "run_python_check"}


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
        # Enable Textual mouse support so the transcript pane scrolls with the
        # mouse wheel and click-to-focus works. Text can be copied with Ctrl+C
        # (copies selected text via Textual's selection system, or full transcript).
        await app.run_async(mouse=True)
    finally:
        await app.finalize_session()


class TextualTerminalUI(TerminalUI):
    """TerminalUI-compatible adapter that writes Rich renderables to Textual."""

    def __init__(self, app: "NexusTextualApp") -> None:
        super().__init__(color=True)
        self._app = app
        self._tool_started_at: dict[str, float] = {}

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
        self._write_alert("Request failed", msg, title_style="bold red", body_style="red on #2a1717")

    def print_warning(self, msg: str) -> None:
        self._write_alert("Warning", msg, title_style="bold yellow", body_style="yellow on #2b2516")

    def print_success(self, msg: str) -> None:
        self._write(Text(msg, style="bold green"))

    def print_info(self, msg: str) -> None:
        self._write(Text(msg, style="cyan"))

    def print_muted(self, msg: str) -> None:
        self._write(Text(msg, style="dim"))

    def _write_alert(
        self,
        title: str,
        msg: str,
        *,
        title_style: str,
        body_style: str,
    ) -> None:
        header = Text()
        header.append(title, style=title_style)
        header.append(":", style=title_style)
        body = Text(str(msg or ""), style=body_style)
        if _should_collapse_alert(msg):
            self._app.write_collapsible(
                header,
                body,
                summary=f"{len(str(msg or ''))} chars",
                preview=Text(_alert_preview(msg), style=body_style),
            )
            return
        self._write(Group(header, body))

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
        self._write(Text("Type /help for commands, /skills for skill control, /abort to stop a running turn, or /quit to exit.", style="dim"))

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
        self._write(_assistant_header())
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

    def _semantic_tool_label(self, tool_name: str, *, completed: bool = False, failed: bool = False) -> str:
        if failed:
            return "Failed"
        labels = {
            "bash": "Ran" if completed else "Run",
            "write_file": "Wrote" if completed else "Write",
            "edit": "Edited" if completed else "Edit",
            "insert_edit_into_file": "Edited" if completed else "Edit",
            "apply_patch": "Patched" if completed else "Patch",
            "read_file": "Read" if completed else "Read",
            "list_dir": "Listed" if completed else "List",
            "grep": "Searched" if completed else "Search",
            "glob": "Found" if completed else "Find",
            "run_tests": "Tested" if completed else "Test",
            "run_python_check": "Checked" if completed else "Check",
        }
        if tool_name.startswith("subagent_"):
            return "Delegated" if completed else "Delegate"
        return labels.get(tool_name, tool_name)

    def _elapsed_label(self, call_id: str, result: ToolResult | None = None) -> str:
        duration = None
        if result is not None:
            raw_duration = result.metadata.get("duration_ms") if isinstance(result.metadata, dict) else None
            if isinstance(raw_duration, (int, float)):
                duration = float(raw_duration) / 1000
        if duration is None and call_id in self._tool_started_at:
            duration = max(0.0, time.perf_counter() - self._tool_started_at[call_id])
        if duration is None:
            return ""
        if duration < 1:
            return f"{duration * 1000:.0f}ms"
        return f"{duration:.1f}s"

    def _tool_target(self, tool_name: str, args: dict[str, Any], result: ToolResult | None = None) -> str:
        metadata = result.metadata if result is not None and isinstance(result.metadata, dict) else {}
        path = metadata.get("path") or args.get("path") or args.get("cwd")
        if isinstance(path, str) and path.strip():
            return self._relative_path(path)
        if tool_name == "bash":
            command = str(args.get("command", "") or "").strip()
            return self._truncate_preview(command, limit=100)
        if tool_name.startswith("subagent_"):
            return self._compact_tool_detail(tool_name, args)
        return self._compact_tool_detail(tool_name, args)

    def _block_text(self, *lines: str, style: str = "default") -> Text:
        text = Text()
        for index, line in enumerate(lines):
            if index:
                text.append("\n")
            text.append(line, style=style)
        return text

    def _inline_header(self, prefix: str, title: str, detail: str = "", *, style: str = "tool") -> Text:
        text = Text(prefix, style="dim")
        text.append(title, style=style)
        if detail:
            text.append(f" {detail}", style="dim")
        return text

    def _append_elapsed(self, text: Text, elapsed: str) -> None:
        if not elapsed:
            return
        text.append(" · ", style="dim")
        text.append(elapsed, style="bold bright_cyan")

    def _render_inline_tool_start(
        self,
        call_id: str,
        tool_name: str,
        actor: str,
        args: dict[str, Any],
        display: dict[str, Any],
    ) -> Text | Group:
        del actor, display
        self._tool_started_at[call_id] = time.perf_counter()
        if _is_subagent_tool(tool_name):
            return self._render_subagent_header(tool_name, args)
        label = self._semantic_tool_label(tool_name)
        if tool_name == "bash":
            command = str(args.get("command", "") or "").strip()
            return Group(
                self._inline_header("> ", f"{label} command", f"#{call_id[:8]}", style="tool.shell"),
                self._block_text(f"$ {command}", style="dim on #1f1f1f"),
            )
        target = self._tool_target(tool_name, args)
        style = self._tool_border_style(tool_name)
        return self._inline_header("> ", label, f"{target}  #{call_id[:8]}", style=style)

    def _write_inline_tool_start(
        self,
        call_id: str,
        tool_name: str,
        actor: str,
        args: dict[str, Any],
        display: dict[str, Any],
    ) -> None:
        if tool_name == "bash":
            self._tool_started_at[call_id] = time.perf_counter()
            label = self._semantic_tool_label(tool_name)
            command = str(args.get("command", "") or "").strip()
            header = self._inline_header("> ", f"{label} command", f"#{call_id[:8]}", style="tool.shell")
            if _should_collapse_text(command):
                self._app.write_collapsible(
                    header,
                    Syntax(command, "bash", theme="monokai", word_wrap=True),
                    summary=f"{_line_count(command)} lines",
                    initially_expanded=False,
                    preview=_preview_text_block(command, style="dim on #1f1f1f"),
                )
                return
            self._write(Group(header, self._block_text(f"$ {command}", style="dim on #1f1f1f")))
            return
        self._write(self._render_inline_tool_start(call_id, tool_name, actor, args, display))

    def _render_subagent_header(self, tool_name: str, args: dict[str, Any]) -> Text:
        title = _subagent_title(args)
        header = Text("| ", style="dim")
        header.append(_subagent_task_label(tool_name), style="bold magenta")
        if title:
            header.append(" - ", style="dim")
            header.append(title, style="bold white")
        return header

    def _render_subagent_tool_row(
        self,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: ToolResult | None = None,
    ) -> Text:
        label = self._semantic_tool_label(
            tool_name,
            completed=result is not None,
            failed=bool(result.is_error) if result is not None else False,
        )
        target = self._tool_target(tool_name, args, result)
        style = "error" if result is not None and result.is_error else self._tool_border_style(tool_name)
        row = Text("|--> ", style="dim")
        row.append(label, style=style)
        if target:
            row.append(f" {target}", style="dim")
        row.append(f"  #{call_id[:8]}", style="dim")
        if result is None:
            row.append(" · running", style="dim")
            return row
        if result.is_error:
            row.append(" · failed", style="bold red")
        else:
            row.append(" · done", style="dim")
        self._append_elapsed(row, self._elapsed_label(call_id, result))
        return row

    def _render_bash_complete(self, result: ToolResult, args: dict[str, Any]) -> None:
        elapsed = self._elapsed_label(result.call_id, result)
        status = "failed" if result.is_error else "done"
        target = self._tool_target(result.tool_name, args, result)
        header = self._inline_header(
            "x " if result.is_error else "< ",
            self._semantic_tool_label(result.tool_name, completed=True, failed=result.is_error),
            " · ".join(part for part in (target, status) if part),
            style="error" if result.is_error else "tool.shell",
        )
        self._append_elapsed(header, elapsed)
        output = (result.output or "").strip()
        if not output:
            self._write(header)
            return
        output_style = "red on #1f1f1f" if result.is_error else "dim on #1f1f1f"
        output_block = self._block_text(output, style=output_style)
        output_preview = _preview_text_block(output, style=output_style)
        if _should_collapse_text(output):
            self._app.write_collapsible(header, output_block, summary=f"{_line_count(output)} lines", preview=output_preview)
        else:
            self._write(Group(header, output_block))

    def _render_confirmation_preview(
        self,
        tool_name: str,
        args: dict[str, Any],
        preview: dict[str, Any],
    ) -> tuple[RenderableType, RenderableType, str] | None:
        diff = _diff_from_preview(preview) or _diff_from_arguments(tool_name, args)
        if not diff and tool_name == "apply_patch":
            diff = str(args.get("patch", "") or "")
        if diff:
            target = self._tool_target(tool_name, args)
            return (
                self._render_diff_editor(diff, path=target),
                self._render_diff_editor_preview(diff, path=target),
                _diff_summary(diff),
            )
        if tool_name == "bash":
            command = str(args.get("command", "") or "").strip()
            if command:
                return (
                    Syntax(command, "bash", theme="monokai", word_wrap=True),
                    _preview_text_block(command, style="dim on #1f1f1f"),
                    f"{_line_count(command)} line{'s' if _line_count(command) != 1 else ''}",
                )
        return None

    def _write_confirmation_request(
        self,
        req: ConfirmationRequest,
        *,
        actor: str,
        display_name: str,
        policy: str,
    ) -> None:
        del actor
        args = {str(key): value for key, value in req.arguments.items()}
        preview = {str(key): value for key, value in req.preview.items()}
        header = self._inline_header(
            "? ",
            "Approval required",
            f"{display_name}  #{req.call_id[:8] or 'pending'}",
            style="warning",
        )
        detail = Group(
            self._render_tool_argument_summary(req.tool_name, args),
            Text(req.reason, style="dim"),
            Text(self._approval_choices(policy), style="dim"),
        )
        rendered_preview = self._render_confirmation_preview(req.tool_name, args, preview)
        if rendered_preview is None:
            self._write(Group(header, detail))
            return
        expanded, collapsed_preview, summary = rendered_preview
        self._app.write_collapsible(
            header,
            Group(detail, expanded),
            summary=summary,
            initially_expanded=False,
            preview=Group(collapsed_preview, detail),
        )

    def _render_file_change_complete(
        self,
        result: ToolResult,
        args: dict[str, Any],
        preview: dict[str, Any],
    ) -> None:
        elapsed = self._elapsed_label(result.call_id, result)
        target = self._tool_target(result.tool_name, args, result)
        label = self._semantic_tool_label(result.tool_name, completed=True, failed=result.is_error)
        detail = " · ".join(part for part in (target, "failed" if result.is_error else "done") if part)
        header = self._inline_header(
            "x " if result.is_error else "< ",
            label,
            detail,
            style="error" if result.is_error else "tool.write",
        )
        self._append_elapsed(header, elapsed)
        if result.is_error:
            body_text = result.output or "Tool failed."
            body = self._block_text(body_text, style="red on #1f1f1f")
            if _should_collapse_text(result.output):
                self._app.write_collapsible(
                    header,
                    body,
                    summary=f"{_line_count(result.output)} lines",
                    preview=_preview_text_block(body_text, style="red on #1f1f1f"),
                )
            else:
                self._write(Group(header, body))
            return

        diff = _diff_from_preview(preview)
        if not diff:
            self._write(header)
            return

        diff_renderable = self._render_diff_editor(diff, path=target)
        self._app.write_collapsible(
            header,
            diff_renderable,
            summary=_diff_summary(diff),
            initially_expanded=False,
            preview=_diff_preview_block(diff, language=self._guess_language(target)),
        )

    def _render_diff_editor(self, diff_text: str, *, path: str = "") -> Group | Table:
        before, after = _changed_sides_from_unified_diff(diff_text)
        return self._render_diff_sides(before, after, path=path)

    def _render_diff_editor_preview(self, diff_text: str, *, path: str = "") -> Group | Table:
        before, after = _changed_sides_from_unified_diff(diff_text)
        truncated = len(before) > _COLLAPSED_PREVIEW_LINES or len(after) > _COLLAPSED_PREVIEW_LINES
        before = before[:_COLLAPSED_PREVIEW_LINES]
        after = after[:_COLLAPSED_PREVIEW_LINES]
        if truncated:
            before.append("...")
            after.append("click [+] to expand")
        return self._render_diff_sides(before, after, path=path)

    def _render_diff_sides(self, before: list[str], after: list[str], *, path: str = "") -> Group | Table:
        width = self._app.transcript_width
        language = self._guess_language(path)
        if width < _SIDE_BY_SIDE_DIFF_WIDTH:
            return Group(
                Text("Before", style="dim"),
                Syntax("\n".join(before).rstrip() or "(empty)", language, theme="monokai", word_wrap=True),
                Text("After", style="dim"),
                Syntax("\n".join(after).rstrip() or "(empty)", language, theme="monokai", word_wrap=True),
            )
        table = Table.grid(expand=True, padding=(0, 2))
        table.add_column(ratio=1)
        table.add_column(ratio=1)
        table.add_row(Text("Before", style="dim"), Text("After", style="dim"))
        table.add_row(
            Syntax("\n".join(before).rstrip() or "(empty)", language, theme="monokai", word_wrap=True),
            Syntax("\n".join(after).rstrip() or "(empty)", language, theme="monokai", word_wrap=True),
        )
        return table

    def _render_generic_complete(self, result: ToolResult, args: dict[str, Any]) -> None:
        elapsed = self._elapsed_label(result.call_id, result)
        label = self._semantic_tool_label(result.tool_name, completed=True, failed=result.is_error)
        detail = self._tool_target(result.tool_name, args, result)
        header = self._inline_header(
            "x " if result.is_error else "< ",
            label,
            detail,
            style="error" if result.is_error else self._tool_border_style(result.tool_name),
        )
        self._append_elapsed(header, elapsed)
        if not result.output or (not result.is_error and result.tool_name in {"read_file", "grep", "glob", "list_dir"}):
            self._write(header)
            return
        body = self._preview_block(result.output, path=str(result.metadata.get("path", "") if isinstance(result.metadata, dict) else ""))
        if _should_collapse_text(result.output):
            self._app.write_collapsible(
                header,
                body,
                summary=f"{_line_count(result.output)} lines",
                preview=_preview_text_block(result.output),
            )
        else:
            self._write(Group(header, body))

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
            self._app.begin_turn_transcript()
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
                if _should_collapse_text(content):
                    self._app.write_collapsible(
                        Text("< Assistant response", style="assistant.header"),
                        Markdown(content),
                        summary=f"{_line_count(content)} lines",
                        initially_expanded=True,
                        preview=Markdown(_first_lines(content)),
                    )
                else:
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
            arguments = payload.get("arguments", {}) if isinstance(payload.get("arguments", {}), dict) else {}
            preview = payload.get("preview", {}) if isinstance(payload.get("preview", {}), dict) else {}
            display = payload.get("display", {}) if isinstance(payload.get("display", {}), dict) else {}
            self._store_tool_call_state(call_id, arguments, preview, actor, display)
            if _is_subagent_tool(tool_name):
                self._tool_started_at[call_id] = time.perf_counter()
                self._app.begin_subagent_task(
                    call_id,
                    tool_name,
                    arguments,
                    header=self._render_subagent_header(tool_name, arguments),
                )
            elif _is_subagent_actor(actor) and self._app.has_subagent_task(actor):
                self._tool_started_at[call_id] = time.perf_counter()
                self._app.record_subagent_tool_row(
                    actor,
                    call_id,
                    self._render_subagent_tool_row(call_id, tool_name, arguments),
                )
            else:
                self._write_inline_tool_start(call_id, tool_name, actor, arguments, display)
            self.start_tool_wait(f"{self._tool_display_name(tool_name, actor)} running")
            return

        if event.kind == AgentEventType.TOOL_CALL_COMPLETE and show_tool_calls:
            self.end_assistant()
            result = cast("ToolResult", event.payload)
            if result is None:
                return
            if isinstance(result.metadata, dict) and result.metadata.get("tool_unavailable"):
                self._clear_tool_call_state(result.call_id)
                self._tool_started_at.pop(result.call_id, None)
                return
            preview = self._tool_preview_by_call_id.get(result.call_id, {})
            actor = str(result.metadata.get("actor") or self._tool_actor_by_call_id.get(result.call_id, "")).strip()
            display = self._tool_display_by_call_id.get(result.call_id, {})
            del display
            arguments = self._tool_args_by_call_id.get(result.call_id, {})
            self._app.record_tool_completion(result)
            if _is_subagent_tool(result.tool_name):
                self._app.finish_subagent_task(
                    result.call_id,
                    result,
                    elapsed=self._elapsed_label(result.call_id, result),
                )
            elif _is_subagent_actor(actor) and self._app.has_subagent_task(actor):
                self._app.record_subagent_tool_row(
                    actor,
                    result.call_id,
                    self._render_subagent_tool_row(result.call_id, result.tool_name, arguments, result),
                )
            elif result.tool_name == "bash":
                self._render_bash_complete(result, arguments)
            elif result.tool_name in _MUTATING_FILE_TOOLS:
                self._render_file_change_complete(result, arguments, preview)
            else:
                self._render_generic_complete(result, arguments)
            self._clear_tool_call_state(result.call_id)
            self._tool_started_at.pop(result.call_id, None)
            self._app._streaming_tool_outputs.discard(result.call_id)
            self._app._streaming_tool_output_chars.pop(result.call_id, None)
            self._app._streaming_tool_output_capped.discard(result.call_id)
            self.start_thinking()
            return

        if event.kind == AgentEventType.TOOL_DENIED:
            self.stop_tool_wait()
            self.end_assistant()
            reason = getattr(event.payload, "reason", str(event.payload))
            self._write_alert("Tool denied", str(reason), title_style="bold red", body_style="red on #2a1717")
            return

        if event.kind == AgentEventType.CONFIRMATION_REQUESTED:
            self.stop_tool_wait()
            self.stop_thinking()
            self.end_assistant()
            req = cast("ConfirmationRequest", event.payload)
            if req.kind is ConfirmationKind.APPROVAL:
                actor = str(req.payload.get("actor", "") or "").strip()
                self._store_tool_call_state(
                    req.call_id,
                    {str(key): value for key, value in req.arguments.items()},
                    {str(key): value for key, value in req.preview.items()},
                    actor,
                    {"is_mutating": True},
                )
                display_name = self._tool_display_name(req.tool_name, actor)
                self._write_confirmation_request(
                    req,
                    actor=actor,
                    display_name=display_name,
                    policy=str(req.payload.get("approval_policy", "on-request")),
                )
            else:
                actor = str(req.payload.get("actor", "") or "").strip()
                display_name = self._tool_display_name(req.tool_name, actor)
                self._write(
                    Group(
                        self._inline_header("? ", "Clarification needed", f"{display_name}  #{req.call_id[:8] or 'pending'}", style="info"),
                        self._render_tool_argument_summary(req.tool_name, req.arguments),
                        Text(req.prompt, style="info"),
                        Text(req.reason, style="dim"),
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
            if event.kind == AgentEventType.AGENT_STOP:
                self._app.write_turn_footer()
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


class PromptInput(Input):
    """Input widget with terminal-style prompt history navigation."""

    BINDINGS = [
        ("up", "history_previous", "Previous prompt"),
        ("down", "history_next", "Next prompt"),
    ]

    def action_history_previous(self) -> None:
        cast("NexusTextualApp", self.app).action_prompt_history_previous()

    def action_history_next(self) -> None:
        cast("NexusTextualApp", self.app).action_prompt_history_next()

    def key_tab(self, event: events.Key) -> None:
        # Let Tab bubble up to cycle focus to the transcript pane instead of
        # inserting a literal tab character into the prompt.
        event.prevent_default()

    def render_line(self, y: int) -> Strip:
        if y != 0:
            return Strip.blank(self.size.width, self.rich_style)

        console = self.app.console
        console_options = self.app.console_options
        max_content_width = self.scrollable_content_region.width
        cursor_visible = self._cursor_visible if self.has_focus else True

        if not self.value:
            placeholder = Text(self.placeholder, justify="left", end="")
            placeholder.stylize(self.get_component_rich_style("input--placeholder"))
            if cursor_visible:
                cursor_style = self.get_component_rich_style("input--cursor")
                if len(placeholder) == 0:
                    placeholder = Text(" ", end="")
                placeholder.stylize(cursor_style, 0, 1)

            strip = Strip(
                console.render(
                    placeholder, console_options.update_width(max_content_width + 1)
                )
            )
        else:
            result = self._value
            value = self.value
            value_length = len(value)
            suggestion = self._suggestion
            show_suggestion = len(suggestion) > value_length and self.has_focus
            if show_suggestion:
                result += Text(
                    suggestion[value_length:],
                    self.get_component_rich_style("input--suggestion"),
                    end="",
                )

            if self.has_focus and not self.selection.is_empty:
                start, end = self.selection
                start, end = sorted((start, end))
                selection_style = self.get_component_rich_style("input--selection")
                result.stylize_before(selection_style, start, end)

            if cursor_visible:
                cursor_style = self.get_component_rich_style("input--cursor")
                cursor = self.cursor_position
                if not show_suggestion and self.cursor_at_end:
                    result.pad_right(1)
                result.stylize(cursor_style, cursor, cursor + 1)

            segments = list(
                console.render(result, console_options.update_width(self.content_width))
            )

            strip = Strip(segments)
            scroll_x, _ = self.scroll_offset
            strip = strip.crop(scroll_x, scroll_x + max_content_width + 1)
            strip = strip.extend_cell_length(max_content_width + 1)

        return strip.apply_style(self.rich_style)


class TranscriptLog(RichLog):
    """Focusable transcript view with keyboard scrolling."""

    can_focus = True
    ALLOW_SELECT = True
    BINDINGS = [
        ("ctrl+a", "select_all", "Select transcript"),
        ("up", "scroll_line_up", "Scroll up"),
        ("down", "scroll_line_down", "Scroll down"),
        ("pageup", "scroll_page_up", "Scroll page up"),
        ("pagedown", "scroll_page_down", "Scroll page down"),
        ("home", "scroll_home_key", "Scroll home"),
        ("end", "scroll_end_key", "Scroll end"),
    ]

    def on_click(self, event: events.Click) -> None:
        if event.button == _RIGHT_MOUSE_BUTTON:
            event.stop()
            return
        toggle_id = _toggle_id_from_click(event)
        if toggle_id:
            cast("NexusTextualApp", self.app).toggle_collapsible(toggle_id)
            event.stop()
            return
        del event
        self.focus()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != _RIGHT_MOUSE_BUTTON:
            return
        event.stop()
        event.prevent_default()
        self.focus()
        cast("NexusTextualApp", self.app).copy_selection_or_transcript()

    def action_scroll_line_up(self) -> None:
        self.scroll_up(animate=False)

    def action_scroll_line_down(self) -> None:
        self.scroll_down(animate=False)

    def action_scroll_page_up(self) -> None:
        self.scroll_page_up(animate=False)

    def action_scroll_page_down(self) -> None:
        self.scroll_page_down(animate=False)

    def action_scroll_home_key(self) -> None:
        self.scroll_home(animate=False)

    def action_scroll_end_key(self) -> None:
        self.scroll_end(animate=False)

    def action_select_all(self) -> None:
        self.text_select_all()

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(line.text.rstrip() for line in self.lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self._line_cache.clear()
        self.refresh()

    def _render_line(self, y: int, scroll_x: int, width: int) -> Strip:
        if y >= len(self.lines):
            return Strip.blank(width, self.rich_style).apply_offsets(scroll_x, y)

        key = (y + self._start_line, scroll_x, width, self._widest_line_width)
        if key in self._line_cache:
            line = self._line_cache[key]
        else:
            line = self.lines[y].crop_extend(scroll_x, scroll_x + width, self.rich_style)
            self._line_cache[key] = line

        selection = self.text_selection
        if selection is not None:
            line = self._apply_selection_style(line, selection, y, scroll_x, width)
        return line.apply_offsets(scroll_x, y)

    def _apply_selection_style(
        self,
        line: Strip,
        selection: Selection,
        y: int,
        scroll_x: int,
        width: int,
    ) -> Strip:
        span = selection.get_span(y)
        if span is None:
            return line
        start, end = span
        if end == -1:
            end = self.lines[y].cell_length
        visible_start = max(start, scroll_x)
        visible_end = min(end, scroll_x + width)
        if visible_end <= visible_start:
            return line
        selected_start = visible_start - scroll_x
        selected_end = visible_end - scroll_x
        selection_style = self.screen.get_component_rich_style("screen--selection")
        return Strip.join(
            [
                line.crop(0, selected_start),
                line.crop(selected_start, selected_end).apply_style(selection_style),
                line.crop(selected_end),
            ]
        )


class NexusTextualApp(App[None]):
    ALLOW_SELECT = True
    CSS = """
    Screen {
        background: $surface;
    }

    #transcript {
        height: 1fr;
        width: 100%;
        margin: 1 1 0 1;
        padding: 1 2;
        border: round transparent;
        background: transparent;
        scrollbar-size: 0 0;
    }

    #transcript:focus {
        border: round transparent;
    }

    #status {
        height: auto;
        min-height: 1;
        padding: 0 2;
        color: $text-muted;
        background: transparent;
    }

    #input-bar {
        height: 3;
        margin: 0 1 0 1;
        padding: 0;
        border: none;
        background: #1f1f1f;
    }

    #prompt-marker {
        width: 3;
        height: 3;
        padding: 0 0 0 1;
        color: #4ea1ff;
        text-style: bold;
        background: #1f1f1f;
    }

    #prompt {
        height: 3;
        width: 1fr;
        margin: 0;
        padding: 0 1 0 0;
        border: none;
        background: #2e2e2e;
    }

    #prompt:focus {
        border: none;
        background: #2e2e2e;
        background-tint: transparent;
    }

    #footer {
        height: 1;
        margin: 0 1 1 1;
        padding: 0 2;
        color: $text-muted;
        background: transparent;
    }
    """
    TITLE = "Nexus"
    SUB_TITLE = "Coding Agent"
    BINDINGS = [
        ("ctrl+c", "copy_or_quit", "Copy selected text or quit"),
        ("ctrl+d", "quit", "Quit"),
        ("ctrl+q", "quit", "Quit"),
        ("tab", "focus_cycle", "Switch panel focus"),
        ("shift+tab", "focus_cycle", "Switch panel focus"),
        ("pageup", "scroll_transcript_page_up", "Scroll up"),
        ("pagedown", "scroll_transcript_page_down", "Scroll down"),
        ("home", "scroll_transcript_home", "Scroll home"),
        ("end", "scroll_transcript_end", "Scroll end"),
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
        self._pending_input_prompt = ""
        self._busy = False
        self._assistant_buffer = ""
        self.has_open_assistant_stream = False
        self._session_finalized = False
        self._spinner_timer: Any = None
        self._spinner_frame = 0
        self._status_text = ""
        self._streaming_tool_outputs: set[str] = set()
        self._streaming_tool_output_chars: dict[str, int] = {}
        self._streaming_tool_output_capped: set[str] = set()
        self._transcript_entries: list[dict[str, Any]] = []
        self._transcript_plain_parts: list[str] = []
        self._subagent_entries_by_actor: dict[str, dict[str, Any]] = {}
        self._subagent_entries_by_call_id: dict[str, dict[str, Any]] = {}
        self._transcript: Any = None
        self._status: Any = None
        self._input: Any = None
        self._footer: Any = None
        self._turn_started_at = 0.0
        self._turn_tool_count = 0
        self._turn_edit_count = 0
        self._turn_error_count = 0
        self._turn_recovery_count = 0
        self._last_tool_failed = False
        self._turn_footer_written = False
        self._prompt_turn_index = 0
        self._prompt_history = [
            message.content
            for message in state.history
            if message.role == "user" and message.content.strip()
        ]
        self._prompt_history_limit = max(1, int(getattr(state.config, "prompt_history_max_entries", 200)))
        self._prompt_history = self._prompt_history[-self._prompt_history_limit :]
        self._prompt_history_index = len(self._prompt_history)
        self._prompt_history_draft = ""

    def compose(self) -> ComposeResult:
        max_lines = max(1, int(getattr(self.state.config, "textual_transcript_max_lines", 5000)))
        yield TranscriptLog(id="transcript", wrap=True, highlight=False, markup=False, max_lines=max_lines)
        yield Static("", id="status")
        with Horizontal(id="input-bar"):
            yield Static("|\n|\n|", id="prompt-marker")
            yield PromptInput(placeholder="Message Nexus or type /help", id="prompt")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self._transcript = self.query_one("#transcript", TranscriptLog)
        self._status = self.query_one("#status", Static)
        self._input = self.query_one("#prompt", Input)
        self._footer = self.query_one("#footer", Static)
        self.console.push_theme(NEXUS_THEME)
        self.state.console = self.ui
        self._render_startup()
        self.refresh_footer()
        self._input.focus()

    def on_unmount(self) -> None:
        self.state.console = self._original_console

    def write(self, renderable: RenderableType) -> None:
        self._transcript_entries.append({"type": "renderable", "renderable": renderable})
        self._render_transcript_entry(self._transcript_entries[-1])

    def write_collapsible(
        self,
        header: RenderableType,
        expanded: RenderableType,
        *,
        summary: str = "",
        initially_expanded: bool = False,
        preview: RenderableType | None = None,
    ) -> None:
        entry = {
            "type": "collapsible",
            "id": uuid4().hex[:8],
            "header": header,
            "expanded": expanded,
            "preview": preview,
            "summary": summary,
            "expanded_state": initially_expanded,
        }
        self._transcript_entries.append(entry)
        self._render_transcript_entry(entry)

    def begin_subagent_task(
        self,
        call_id: str,
        actor: str,
        args: dict[str, Any],
        *,
        header: RenderableType,
    ) -> None:
        entry = {
            "type": "collapsible",
            "id": uuid4().hex[:8],
            "header": header,
            "expanded": Text("| Starting sub-agent...", style="dim"),
            "preview": Text("| Running...", style="dim"),
            "summary": "running",
            "expanded_state": True,
            "subagent_actor": actor,
            "subagent_call_id": call_id,
            "subagent_title": _subagent_title(args),
            "subagent_tool_order": [],
            "subagent_tool_rows": {},
        }
        self._refresh_subagent_task_entry(entry)
        self._transcript_entries.append(entry)
        self._subagent_entries_by_actor[actor] = entry
        self._subagent_entries_by_call_id[call_id] = entry
        self._render_transcript_entry(entry)

    def record_subagent_tool_row(
        self,
        actor: str,
        call_id: str,
        row: RenderableType,
    ) -> None:
        entry = self._subagent_entries_by_actor.get(actor)
        if entry is None:
            self.write(row)
            return
        order = cast("list[str]", entry.setdefault("subagent_tool_order", []))
        rows = cast("dict[str, RenderableType]", entry.setdefault("subagent_tool_rows", {}))
        if call_id not in rows:
            order.append(call_id)
        rows[call_id] = row
        self._refresh_subagent_task_entry(entry)
        self._rerender_transcript()

    def has_subagent_task(self, actor: str) -> bool:
        return actor in self._subagent_entries_by_actor

    def finish_subagent_task(
        self,
        call_id: str,
        result: ToolResult,
        *,
        elapsed: str = "",
    ) -> None:
        entry = self._subagent_entries_by_call_id.get(call_id) or self._subagent_entries_by_actor.get(result.tool_name)
        if entry is None:
            payload = _parse_json_object(result.output)
            header = _subagent_completion_header(result, payload, elapsed=elapsed)
            body = _subagent_result_body(result.output, payload)
            self.write_collapsible(
                header,
                body,
                summary=_subagent_completion_summary(result, payload, elapsed=elapsed, tool_rows=0),
                preview=_subagent_result_preview(result, payload),
            )
            return

        payload = _parse_json_object(result.output)
        tool_rows = len(cast("list[str]", entry.get("subagent_tool_order", [])))
        entry["header"] = _subagent_completion_header(result, payload, elapsed=elapsed)
        entry["summary"] = _subagent_completion_summary(result, payload, elapsed=elapsed, tool_rows=tool_rows)
        entry["subagent_result_output"] = result.output
        entry["subagent_result_payload"] = payload
        entry["subagent_result_status"] = str(payload.get("status") or result.metadata.get("status") or ("failed" if result.is_error else "completed"))
        entry["subagent_result_is_error"] = result.is_error
        entry["expanded_state"] = False
        self._refresh_subagent_task_entry(entry)
        self._subagent_entries_by_actor.pop(str(entry.get("subagent_actor", "")), None)
        self._rerender_transcript()

    def _refresh_subagent_task_entry(self, entry: dict[str, Any]) -> None:
        order = cast("list[str]", entry.get("subagent_tool_order", []))
        rows = cast("dict[str, RenderableType]", entry.get("subagent_tool_rows", {}))
        body_parts: list[RenderableType] = []
        payload = entry.get("subagent_result_payload")
        if isinstance(payload, dict):
            body_parts.append(_subagent_result_summary_block(payload, str(entry.get("subagent_result_status", ""))))
        if order:
            body_parts.append(Text("| Tool calls", style="dim"))
            body_parts.extend(rows[call_id] for call_id in order if call_id in rows)
        if isinstance(entry.get("subagent_result_output"), str):
            body_parts.append(Text("| Result JSON", style="dim"))
            body_parts.append(_subagent_result_body(str(entry.get("subagent_result_output") or ""), payload if isinstance(payload, dict) else {}))
            entry["preview"] = _subagent_result_preview(
                ToolResult(
                    call_id=str(entry.get("subagent_call_id", "")),
                    tool_name=str(entry.get("subagent_actor", "subagent")),
                    output=str(entry.get("subagent_result_output") or ""),
                    is_error=bool(entry.get("subagent_result_is_error")),
                ),
                payload if isinstance(payload, dict) else {},
            )
        elif order:
            entry["preview"] = Group(Text("| Running tool calls", style="dim"), *[rows[call_id] for call_id in order[:3] if call_id in rows])
        else:
            entry["preview"] = Text("| Running...", style="dim")
        entry["expanded"] = Group(*body_parts) if body_parts else Text("| Starting sub-agent...", style="dim")

    def toggle_collapsible(self, toggle_id: str) -> None:
        for entry in self._transcript_entries:
            if entry.get("type") == "collapsible" and entry.get("id") == toggle_id:
                entry["expanded_state"] = not bool(entry.get("expanded_state"))
                self._rerender_transcript()
                return

    @property
    def transcript_width(self) -> int:
        if self._transcript is None:
            return 100
        try:
            return max(1, int(self._transcript.scrollable_content_region.width or 0))
        except Exception:  # noqa: BLE001
            return 100

    def _render_transcript_entry(self, entry: dict[str, Any]) -> None:
        if self._transcript is None:
            return
        renderable = self._entry_renderable(entry)
        self._transcript.write(renderable)
        plain_text = self._render_transcript_plain_text(renderable)
        if plain_text:
            self._transcript_plain_parts.append(plain_text)

    def _entry_renderable(self, entry: dict[str, Any]) -> RenderableType:
        if entry.get("type") != "collapsible":
            return cast("RenderableType", entry.get("renderable", Text("")))
        toggle_id = str(entry.get("id", ""))
        expanded = bool(entry.get("expanded_state"))
        marker = "[-]" if expanded else "[+]"
        header = cast("RenderableType", entry.get("header", ""))
        summary = str(entry.get("summary", "") or "").strip()
        line = Text(f"{marker} ", style="default")
        if isinstance(header, Text):
            line.append(header.copy())
        else:
            line.append(_renderable_plain_text(header).strip(), style="default")
        if summary:
            line.append(f" ({summary})", style="dim")
        line.stylize(Style(color="cyan", meta={"nexus_toggle": toggle_id}), 0, min(3, len(line.plain)))
        if not expanded:
            preview = entry.get("preview")
            if preview is not None:
                return Group(line, cast("RenderableType", preview))
            return line
        return Group(line, cast("RenderableType", entry.get("expanded", Text(""))))

    def _rerender_transcript(self) -> None:
        if self._transcript is None:
            return
        self._transcript.clear()
        self._transcript_plain_parts.clear()
        for entry in self._transcript_entries:
            self._render_transcript_entry(entry)

    def begin_turn_transcript(self) -> None:
        self._turn_started_at = time.perf_counter()
        self._turn_tool_count = 0
        self._turn_edit_count = 0
        self._turn_error_count = 0
        self._turn_recovery_count = 0
        self._last_tool_failed = False
        self._turn_footer_written = False
        self.refresh_footer()

    def record_tool_completion(self, result: ToolResult) -> None:
        self._turn_tool_count += 1
        if result.tool_name in _MUTATING_FILE_TOOLS:
            self._turn_edit_count += 1
        if result.is_error:
            self._turn_error_count += 1
            self._last_tool_failed = True
            return
        if self._last_tool_failed and (result.tool_name in _MUTATING_FILE_TOOLS or result.tool_name in _VERIFY_TOOL_NAMES):
            self._turn_recovery_count += 1
        self._last_tool_failed = False

    def write_turn_footer(self) -> None:
        if self._turn_footer_written or self._turn_started_at <= 0:
            return
        self._turn_footer_written = True
        elapsed = max(0.0, time.perf_counter() - self._turn_started_at)
        parts = [
            "Done",
            f"{self._turn_tool_count} tool{'s' if self._turn_tool_count != 1 else ''}",
            f"{self._turn_edit_count} edit{'s' if self._turn_edit_count != 1 else ''}",
        ]
        if self._turn_error_count:
            parts.append(f"{self._turn_error_count} failed")
        if self._turn_recovery_count:
            parts.append(f"{self._turn_recovery_count} recovered")
        footer = Text(" · ".join(parts), style="dim")
        footer.append(" · ", style="dim")
        footer.append(f"{elapsed:.1f}s", style="bold bright_cyan")
        self.write(footer)
        self.write(Text(""))
        self.refresh_footer()

    def refresh_footer(self) -> None:
        if self._footer is None:
            return
        estimator = TokenEstimator()
        history_tokens = sum(estimator.estimate(message.content) for message in self.state.history)
        system_tokens = estimator.estimate(self.state.current_system_prompt or "")
        total_tokens = history_tokens + system_tokens
        context_limit = get_model_context_limit(self.state.config.model_name)
        pct = min(100.0, round((total_tokens / context_limit * 100), 1)) if context_limit else 0.0
        thinking_enabled = self.state.config.llm_thinking_mode != "disabled"
        workspace = _compact_workspace_label(self.state.config.workspace_root)

        text = Text()
        pie_style = _context_style(pct)
        text.append(_context_pie_icon(pct), style=f"bold {pie_style}")
        text.append(" ctx ", style="dim")
        text.append(f"{pct:.1f}%", style=f"bold {pie_style}")
        text.append("  ")
        text.append("mode ", style="dim")
        text.append(self.state.mode.value, style="bold cyan")
        text.append("  ")
        text.append("agent ", style="dim")
        text.append(str(getattr(self.state.config, "agent_mode", "basic")), style="bold magenta")
        text.append("  ")
        text.append("thinking ", style="dim")
        text.append("True" if thinking_enabled else "False", style="bold green" if thinking_enabled else "bold red")
        text.append("  ")
        text.append("budget ", style="dim")
        text.append(str(self.state.config.llm_reasoning_effort or "none"), style="bold yellow")
        text.append("  ")
        text.append("model ", style="dim")
        text.append(self.state.config.model_name, style="bold white")
        text.append("  ")
        text.append("workspace ", style="dim")
        text.append(workspace, style="white")
        if self._status_text:
            text.append("  ")
            text.append("status ", style="dim")
            text.append(self._status_text, style="bold bright_cyan")
        self._footer.update(text)

    def _render_transcript_plain_text(self, renderable: RenderableType) -> str:
        width = 100
        if self._transcript is not None:
            try:
                width = max(width, int(self._transcript.scrollable_content_region.width or 0))
            except Exception:  # noqa: BLE001
                pass
        buffer = io.StringIO()
        console = Console(
            file=buffer,
            theme=NEXUS_THEME,
            no_color=True,
            force_terminal=False,
            highlight=False,
            width=width,
        )
        console.print(renderable)
        return buffer.getvalue()

    def _transcript_text(self) -> str:
        return "".join(self._transcript_plain_parts).strip()

    def _transcript_is_focused(self) -> bool:
        if self._transcript is None:
            return False
        if self.focused is self._transcript:
            return True
        if getattr(self.screen, "focused", None) is self._transcript:
            return True
        return bool(getattr(self._transcript, "has_focus", False))

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
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            frame = frames[self._spinner_frame % len(frames)]
            self._spinner_frame += 1
            status = Text()
            status.append(frame, style="bold bright_cyan")
            status.append(" ")
            status.append(self._status_text, style="bold")
            status.append(" ...", style="dim")
            self._status.update(status)
        self.refresh_footer()

    def clear_status(self, expected: str | None = None) -> None:
        if expected is not None and self._status_text != expected:
            return
        self._status_text = ""
        if self._status is not None:
            self._status.update("")
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self.refresh_footer()

    def _flash_status(self, message: str, *, seconds: float = 1.5) -> None:
        self._status_text = ""
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        if self._status is not None:
            self._status.update(message)
        self.set_timer(seconds, lambda: self.clear_status(expected=""))
        self.refresh_footer()

    def _echo_input_prompt(self, prompt: str) -> None:
        label = prompt.strip()
        if not label:
            return
        self.write(Text(f"Input required: {label}", style="warning"))

    def append_assistant_delta(self, content: str) -> None:
        if not self.has_open_assistant_stream:
            self.ui.begin_assistant()
            self.has_open_assistant_stream = True
            self._assistant_buffer = ""
        self._assistant_buffer += content

    def close_assistant_stream(self) -> None:
        if not self.has_open_assistant_stream:
            return
        content = self._assistant_buffer
        if _should_collapse_text(content):
            self.write_collapsible(
                Text("< Assistant response", style="assistant.header"),
                Markdown(content),
                summary=f"{_line_count(content)} lines",
                initially_expanded=True,
                preview=Markdown(_first_lines(content)),
            )
        else:
            self.write(Markdown(content))
        self.write(Text(""))
        self._assistant_buffer = ""
        self.has_open_assistant_stream = False

    def append_tool_output(self, call_id: str, stream_name: str, chunk: str) -> None:
        max_chars = max(1, int(getattr(self.state.config, "tool_output_max_chars", 100 * 1024)))
        current_chars = self._streaming_tool_output_chars.get(call_id, 0)
        if current_chars >= max_chars:
            if call_id not in self._streaming_tool_output_capped:
                self._streaming_tool_output_capped.add(call_id)
                self.write(Text(f"[live output capped at {max_chars} chars]", style="dim"))
            return
        remaining = max_chars - current_chars
        capped_now = False
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
            capped_now = True
        self._streaming_tool_output_chars[call_id] = current_chars + len(chunk)
        if call_id not in self._streaming_tool_outputs:
            self._streaming_tool_outputs.add(call_id)
            self.write(Text(f"> bash live output  #{call_id[:8]}", style="dim"))
        style = "red" if stream_name == "stderr" else "default"
        prefix = "[stderr] " if stream_name == "stderr" else ""
        self.write(Text(prefix + chunk.rstrip("\n"), style=f"{style} on #1f1f1f"))
        if capped_now and call_id not in self._streaming_tool_output_capped:
            self._streaming_tool_output_capped.add(call_id)
            self.write(Text(f"[live output capped at {max_chars} chars]", style="dim"))

    async def on_input_submitted(self, event: Any) -> None:
        raw = _strip_mouse_escape_sequences(str(event.value or "")).strip()
        self._input.value = ""
        if raw == "/abort" or raw.startswith("/abort "):
            if await self.router.dispatch(self.state, raw):
                self._record_prompt_history(raw)
                return
        if self._pending_input is not None:
            pending = self._pending_input
            pending_prompt = self._pending_input_prompt
            self._pending_input = None
            if not pending.done():
                self.write(_input_response_block(pending_prompt, raw))
                pending.set_result(raw)
            self.clear_status()
            return
        if not raw:
            return
        self._record_prompt_history(raw)
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
        self._echo_input_prompt(prompt)
        self.set_status(prompt)
        self._input.placeholder = prompt
        self._input.focus()
        self._pending_input = asyncio.get_running_loop().create_future()
        self._pending_input_prompt = prompt
        try:
            return await self._pending_input
        finally:
            self._pending_input = None
            self._pending_input_prompt = ""
            self._input.placeholder = "Message Nexus or type /help"
            self.clear_status()

    async def action_quit(self) -> None:
        self.state.should_exit = True
        await self.finalize_session()
        self.exit()

    async def action_copy_or_quit(self) -> None:
        input_selected_text = self._focused_input_selected_text()
        if input_selected_text:
            self._copy_text_to_clipboard(input_selected_text)
            self._flash_status("Copied input selection")
            return
        if self.copy_selection_or_transcript():
            return
        await self.action_quit()

    def copy_selection_or_transcript(self) -> bool:
        selected_text = self.screen.get_selected_text()
        # Fallback: directly read selection from TranscriptLog (screen.get_selected_text()
        # may not pick up the custom RichLog subclass selection on some platforms).
        if not selected_text and self._transcript is not None:
            ts = getattr(self._transcript, "text_selection", None)
            if ts is not None:
                try:
                    sel_result = self._transcript.get_selection(ts)
                    if sel_result:
                        selected_text = sel_result[0] if isinstance(sel_result, tuple) else sel_result
                except Exception:  # noqa: BLE001
                    pass
        if selected_text:
            self._copy_text_to_clipboard(selected_text)
            self._flash_status("Copied selection")
            return True
        transcript_text = self._transcript_text()
        if transcript_text:
            self._copy_text_to_clipboard(transcript_text)
            self._flash_status("Copied transcript")
            return True
        return False

    def _copy_text_to_clipboard(self, text: str) -> None:
        self.copy_to_clipboard(text)
        _copy_to_system_clipboard(text)

    def _focused_input_selected_text(self) -> str:
        if self._input is None:
            return ""
        if (
            self.focused is not self._input
            and getattr(self.screen, "focused", None) is not self._input
            and not bool(getattr(self._input, "has_focus", False))
        ):
            return ""
        return str(getattr(self._input, "selected_text", "") or "")

    def action_focus_cycle(self) -> None:
        if self._input is None or self._transcript is None:
            return
        if self.focused is self._input:
            self._transcript.focus()
            return
        self._input.focus()

    def action_scroll_transcript_page_up(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_page_up(animate=False)

    def action_scroll_transcript_page_down(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_page_down(animate=False)

    def action_scroll_transcript_home(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_home(animate=False)

    def action_scroll_transcript_end(self) -> None:
        if self._transcript is not None:
            self._transcript.scroll_end(animate=False)

    def action_prompt_history_previous(self) -> None:
        if self._pending_input is not None or self.focused is not self._input:
            return
        self._show_prompt_history_delta(-1)

    def action_prompt_history_next(self) -> None:
        if self._pending_input is not None or self.focused is not self._input:
            return
        self._show_prompt_history_delta(1)

    def _record_prompt_history(self, value: str) -> None:
        if not value:
            return
        self._prompt_history.append(value)
        if len(self._prompt_history) > self._prompt_history_limit:
            del self._prompt_history[: len(self._prompt_history) - self._prompt_history_limit]
        self._prompt_history_index = len(self._prompt_history)
        self._prompt_history_draft = ""

    def _show_prompt_history_delta(self, delta: int) -> None:
        if self._input is None or not self._prompt_history:
            return
        if self._prompt_history_index == len(self._prompt_history):
            self._prompt_history_draft = str(self._input.value or "")

        if delta < 0:
            if self._prompt_history_index == 0:
                return
            self._prompt_history_index -= 1
            value = self._prompt_history[self._prompt_history_index]
        else:
            if self._prompt_history_index >= len(self._prompt_history):
                return
            self._prompt_history_index += 1
            value = (
                self._prompt_history[self._prompt_history_index]
                if self._prompt_history_index < len(self._prompt_history)
                else self._prompt_history_draft
            )

        self._input.value = value
        self._input.cursor_position = len(value)

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
            self.ui.print_muted("A previous task was paused after hitting a turn limit. Type `continue` to resume it, or enter a new prompt to start something else.")
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
        elif cfg.provider == "cohere":
            has_key = bool(
                cfg.api_key
                or environ.get("COHERE_API_KEY")
                or environ.get("CO_API_KEY")
                or environ.get("NEXUS_API_KEY")
                or environ.get("API_KEY")
            )
        else:
            has_key = True
        if not has_key:
            self.ui.print_no_api_key_warning(cfg.provider)

    async def _handle_prompt(self, raw_input: str) -> None:
        self._busy = True
        user_message_appended = False
        self.set_status("Thinking")
        self.ui.print(Text(""))
        self.ui.print(_user_prompt_block(raw_input))
        self.ui.print(Text(""))
        self._prompt_turn_index += 1
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
            self.state.history.append(Message(role="user", content=effective_prompt if resumed_paused_turn else raw_input))
            user_message_appended = True
            if not resumed_paused_turn:
                self.state.approval_manager.begin_turn()
            try:
                self.state.begin_running_turn()
                events = await run_orchestrated_turn(
                    self.state,
                    self.agent,
                    prompt_text=effective_prompt,
                    ui=self.ui,
                    approval_callback=self._approval_callback(),
                )
            except asyncio.CancelledError:
                self.ui.print_warning("Turn aborted.")
                if user_message_appended:
                    self.state.history.pop()
                return
            except Exception as exc:  # noqa: BLE001
                from nexus.app import provider_error_message
                from nexus.observability import capture_exception_from_hooks

                capture_exception_from_hooks(
                    self.state.hooks,
                    exc,
                    context={
                        "session_id": self.state.session.session_id,
                        "turn_id": self.state.current_turn_id,
                        "trace_id": self.state.current_trace_id,
                        "provider": self.state.config.provider,
                        "model": self.state.config.model_name,
                        "textual": True,
                    },
                )
                self.ui.print_error(provider_error_message(exc, self.state.config))
                if user_message_appended:
                    self.state.history.pop()
                return
            finally:
                self.state.clear_running_turn()
            self.state.apply_events(events)
            self.state.current_turn_id = ""
            self.state.current_trace_id = ""
            self.refresh_footer()
        finally:
            self._busy = False
            self.clear_status()
            self._input.focus()

    def _approval_callback(self) -> ConfirmationCallback:
        async def ask_for_approval(request: ConfirmationRequest) -> ConfirmationResponse:
            if request.kind is ConfirmationKind.CLARIFICATION:
                field = request.payload.get("field", "value")
                while True:
                    answer = await self.ask(f"Value for {field!r}:")
                    clarified = answer.strip()
                    if clarified:
                        return ConfirmationResponse(clarification=clarified)
                    self.ui.print_muted("A value is required. Provide input or cancel with Ctrl+C.")

            try:
                policy = approval_policy_for_request(request)
            except Exception:
                # Tolerate malformed approval payloads and keep prompting.
                policy = ApprovalPolicy.ON_REQUEST

            while True:
                answer = (await self.ask(f"Allow? {approval_prompt_label(policy)}")).strip().lower()
                response = approval_response_from_answer(answer, policy)
                if response.approved or _is_explicit_denial_answer(answer):
                    return response
                self.ui.print_muted("Please answer with yes/y (or t/turn when offered), or no/n.")

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


def _is_explicit_denial_answer(answer: str) -> bool:
    normalized = " ".join(
        answer.strip().lower().replace("(", " ").replace(")", " ").replace("-", " ").replace("_", " ").split()
    )
    return normalized in {"n", "no"}


def _copy_to_system_clipboard(text: str) -> bool:
    for command in _clipboard_commands():
        try:
            subprocess.run(
                command,
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
                check=True,
            )
            return True
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            continue
    return False


def _clipboard_commands() -> list[list[str]]:
    if sys.platform == "darwin":
        return [["pbcopy"]] if shutil.which("pbcopy") else []
    if not sys.platform.startswith("linux"):
        return []

    commands: list[list[str]] = []
    if shutil.which("wl-copy"):
        commands.append(["wl-copy"])
    if shutil.which("xclip"):
        commands.append(["xclip", "-selection", "clipboard"])
    if shutil.which("xsel"):
        commands.append(["xsel", "--clipboard", "--input"])
    return commands


def _toggle_id_from_click(event: events.Click) -> str:
    style = getattr(event, "style", None)
    meta = getattr(style, "meta", None)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("nexus_toggle") or "")


def _renderable_plain_text(renderable: RenderableType, *, width: int = 120) -> str:
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        theme=NEXUS_THEME,
        no_color=True,
        force_terminal=False,
        highlight=False,
        width=width,
    )
    console.print(renderable)
    return buffer.getvalue()


def _assistant_header() -> Text:
    header = Text()
    header.append("Assistant", style="bold green")
    header.append(":", style="green")
    return header


def _user_prompt_block(raw_input: str) -> Padding:
    text = Text()
    text.append("You", style="bold green")
    text.append(": ", style="green")
    lines = str(raw_input or "").splitlines() or [""]
    for index, line in enumerate(lines):
        if index:
            text.append("\n  ", style="dim")
        text.append(line, style="white")
    return Padding(text, pad=(0, 0), style="on #1d2b3e")


def _input_response_block(prompt: str, raw_input: str) -> Text:
    is_approval = prompt.strip().lower().startswith("allow?")
    title = "Approval response" if is_approval else "Input response"
    text = Text()
    text.append(title, style="bold dark_green")
    text.append(": ", style="bold dark_green")
    text.append(str(raw_input or ""), style="white on #252525")
    return text


def _is_subagent_tool(tool_name: str) -> bool:
    return tool_name == "delegate_task" or tool_name.startswith("subagent_")


def _is_subagent_actor(actor: str) -> bool:
    return bool(actor) and _is_subagent_tool(actor)


def _subagent_title(args: dict[str, Any]) -> str:
    title = str(args.get("title") or args.get("task") or args.get("instructions") or "").strip()
    return _single_line(title, limit=120)


def _subagent_task_label(tool_name: str) -> str:
    role = tool_name
    if role.startswith("subagent_"):
        role = role[len("subagent_") :]
    elif role == "delegate_task":
        role = "delegate"
    labels = {
        "explorer": "Explore Task",
        "explore": "Explore Task",
        "planning_analysis": "Planning Task",
        "execution": "Execution Task",
        "coding": "Coding Task",
        "code_reviewer": "Review Task",
        "review": "Review Task",
        "impact_analyzer": "Impact Task",
        "verification": "Verification Task",
        "delegate": "Sub-agent Task",
    }
    if role in labels:
        return labels[role]
    pretty = role.replace("_", " ").replace("-", " ").strip().title()
    return f"{pretty} Task" if pretty else "Sub-agent Task"


def _parse_json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _subagent_completion_header(result: ToolResult, payload: dict[str, Any], *, elapsed: str = "") -> Text:
    del elapsed
    title = _single_line(str(payload.get("title") or result.metadata.get("title") or result.tool_name), limit=120)
    header = Text("| ", style="dim")
    header.append(_subagent_task_label(result.tool_name), style="bold magenta")
    if title:
        header.append(" - ", style="dim")
        header.append(title, style="bold white")
    return header


def _subagent_completion_summary(
    result: ToolResult,
    payload: dict[str, Any],
    *,
    elapsed: str = "",
    tool_rows: int = 0,
) -> str:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    count = context.get("tool_call_count") if isinstance(context, dict) else None
    if not isinstance(count, int):
        count = tool_rows
    status = str(payload.get("status") or result.metadata.get("status") or ("failed" if result.is_error else "completed"))
    parts = [status]
    if elapsed:
        parts.append(elapsed)
    parts.append(f"{count} tool{'s' if count != 1 else ''}")
    return " · ".join(parts)


def _subagent_result_preview(result: ToolResult, payload: dict[str, Any]) -> Text:
    status = str(payload.get("status") or result.metadata.get("status") or ("failed" if result.is_error else "completed"))
    summary = _single_line(str(payload.get("summary") or payload.get("raw_result") or result.output or ""), limit=160)
    text = Text("| Status ", style="dim")
    text.append(status, style=_subagent_status_style(status, is_error=result.is_error))
    if summary:
        text.append("\n| Summary - ", style="dim")
        text.append(summary, style="white")
    text.append("\n| Expand to view sub-agent tool calls and JSON.", style="dim")
    return text


def _subagent_result_summary_block(payload: dict[str, Any], status: str) -> Text:
    summary = _single_line(str(payload.get("summary") or payload.get("raw_result") or ""), limit=180)
    text = Text("| Status ", style="dim")
    rendered_status = status or str(payload.get("status") or "completed")
    text.append(rendered_status, style=_subagent_status_style(rendered_status))
    if summary:
        text.append("\n| Summary - ", style="dim")
        text.append(summary, style="white")
    next_action = _single_line(str(payload.get("recommended_next_action") or ""), limit=100)
    if next_action:
        text.append("\n| Next - ", style="dim")
        text.append(next_action, style="white")
    return text


def _subagent_result_body(output: str, payload: dict[str, Any]) -> RenderableType:
    if payload:
        return Syntax(output, "json", theme="monokai", word_wrap=True)
    return _preview_text_block(output)


def _subagent_status_style(status: str, *, is_error: bool = False) -> str:
    normalized = status.strip().lower()
    if is_error or normalized in {"failed", "needs_approval", "needs_clarification", "blocked", "failed_verification"}:
        return "bold red"
    if normalized in {"issues_found"}:
        return "bold yellow"
    return "bold green"


def _single_line(value: str, *, limit: int) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _first_lines(value: str, *, limit: int = _COLLAPSED_PREVIEW_LINES) -> str:
    lines = str(value or "").splitlines()
    return "\n".join(lines[:limit])


def _preview_text_block(value: str, *, style: str = "dim on #1f1f1f") -> Text:
    preview = _first_lines(value)
    if _line_count(value) > _COLLAPSED_PREVIEW_LINES:
        preview = f"{preview}\n... click [+] to expand"
    return Text(preview, style=style)


def _line_count(value: str) -> int:
    return len(str(value or "").splitlines()) or 1


def _should_collapse_text(value: str) -> bool:
    text = str(value or "")
    return len(text) > _COLLAPSE_CHAR_LIMIT or _line_count(text) > min(_COLLAPSE_LINE_LIMIT, _COLLAPSED_PREVIEW_LINES)


def _should_collapse_alert(value: str) -> bool:
    text = str(value or "")
    return len(text) > _ALERT_PREVIEW_CHARS or _line_count(text) > 3


def _alert_preview(value: str) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= _ALERT_PREVIEW_CHARS:
        return compact
    return compact[: max(0, _ALERT_PREVIEW_CHARS - 3)].rstrip() + "..."


def _diff_from_preview(preview: dict[str, Any]) -> str:
    diff = preview.get("diff") if isinstance(preview.get("diff"), dict) else {}
    return str(diff.get("unified_diff", "") or "")


def _diff_from_arguments(tool_name: str, args: dict[str, Any]) -> str:
    path = str(args.get("path") or "file").strip() or "file"
    if tool_name == "write_file":
        return _unified_diff_text("", str(args.get("content", "") or ""), path=path)
    if tool_name == "edit":
        return _unified_diff_text(
            str(args.get("old_string", "") or ""),
            str(args.get("new_string", "") or ""),
            path=path,
        )
    if tool_name == "insert_edit_into_file":
        return _unified_diff_text("", str(args.get("code", "") or ""), path=path)
    return ""


def _unified_diff_text(old: str, new: str, *, path: str) -> str:
    old_lines = str(old or "").splitlines()
    new_lines = str(new or "").splitlines()
    if old_lines == new_lines:
        return ""
    lines = unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return "\n".join(lines)


def _diff_summary(diff_text: str) -> str:
    added = 0
    removed = 0
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    parts = []
    if removed:
        parts.append(f"-{removed}")
    if added:
        parts.append(f"+{added}")
    return " ".join(parts) if parts else "diff"


def _diff_preview_block(diff_text: str, *, language: str = "text") -> Syntax:
    lines: list[str] = []
    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("+++", "---", "@@")):
            continue
        if raw_line.startswith(("+", "-")):
            lines.append(raw_line)
        if len(lines) >= _COLLAPSED_PREVIEW_LINES:
            break
    preview = "\n".join(lines) or "(no changed lines)"
    if _line_count(diff_text) > _COLLAPSED_PREVIEW_LINES:
        preview = f"{preview}\n... click [+] to expand"
    return Syntax(preview, language, theme="monokai", word_wrap=True)


def _changed_sides_from_unified_diff(diff_text: str) -> tuple[list[str], list[str]]:
    before: list[str] = []
    after: list[str] = []
    pending_removed: list[str] = []

    def flush_removed() -> None:
        nonlocal pending_removed
        if not pending_removed:
            return
        before.extend(pending_removed)
        after.extend([""] * len(pending_removed))
        pending_removed = []

    for raw_line in diff_text.splitlines():
        if not raw_line or raw_line.startswith(("+++", "---", "@@")):
            continue
        marker = raw_line[0]
        value = raw_line[1:]
        if marker == "-":
            pending_removed.append(value)
            continue
        if marker == "+":
            if pending_removed:
                before.append(pending_removed.pop(0))
                after.append(value)
            else:
                before.append("")
                after.append(value)
            continue
        flush_removed()
    flush_removed()
    return before, after


def _context_style(percent: float) -> str:
    if percent >= 85:
        return "red"
    if percent >= 65:
        return "yellow"
    return "green"


def _context_pie_icon(percent: float) -> str:
    if percent <= 0:
        return "○"
    if percent < 25:
        return "◔"
    if percent < 50:
        return "◑"
    if percent < 75:
        return "◕"
    return "●"


def _compact_workspace_label(path: Path) -> str:
    try:
        resolved = path.resolve()
    except Exception:  # noqa: BLE001
        resolved = path
    name = resolved.name or str(resolved)
    parent = resolved.parent.name
    return f"{parent}/{name}" if parent else name


def _thinking_label(event: Any) -> str:
    payload = getattr(event, "payload", None)
    actor = payload.get("actor") if isinstance(payload, dict) else ""
    actor = str(actor).strip() if actor else ""
    return f"{actor} - Thinking" if actor else "Thinking"
