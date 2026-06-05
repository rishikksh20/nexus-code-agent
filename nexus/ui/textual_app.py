"""Textual-powered interactive terminal UI for Nexus."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import io
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from rich.console import Console
from rich.console import Group
from rich.markdown import Markdown
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, OptionList, Static

from nexus.config.provider_profiles import usable_prompt_budget
from nexus.context import TokenEstimator
from nexus.hooks import HookEvent
from nexus.models import (
    ConfirmationKind,
    ConfirmationRequest,
    ConfirmationResponse,
    Message,
    ToolResult,
)
from nexus.runtime.clarifications import (
    ask_user_input_prompt,
    is_ask_user_confirmation,
    parse_ask_user_response,
)
from nexus.runtime.orchestration import run_orchestrated_turn
from nexus.runtime.turn_runner import (
    ConfirmationCallback,
    approval_policy_for_request,
    approval_prompt_label,
    approval_response_from_answer,
)
from nexus.security.policy import ApprovalPolicy
from nexus.ui.terminal import NEXUS_THEME
from nexus.ui.textual_rendering import (
    TextualTerminalUI,
    _MUTATING_FILE_TOOLS,
    _TOOL_ROW_INDENT,
    _VERIFY_TOOL_NAMES,
    _DiffRow,
    _FileChangePreview,
    _ResponsiveDiff,
    _bash_command_block,
    _bash_output_block,
    _command_tool_body,
    _first_lines,
    _line_count,
    _markdown_code_fence,
    _parse_json_object,
    _should_collapse_text,
    _subagent_completion_header,
    _subagent_completion_summary,
    _subagent_result_body,
    _subagent_result_json_row,
    _subagent_result_preview,
    _subagent_result_summary_block,
    _subagent_title,
    _with_inline_toggle,
)
from nexus.ui.textual_utils import (
    _approval_request_key,
    _approval_resolution_summary,
    _compact_workspace_label,
    _context_pie_icon,
    _context_style,
    _copy_to_system_clipboard as _utils_copy_to_system_clipboard,
    _clipboard_commands as _utils_clipboard_commands,
    _input_response_block,
    _is_explicit_denial_answer,
    _renderable_plain_text,
    _slash_command_suggestion_options,
    _strip_mouse_escape_sequences,
    _user_prompt_block,
)
from nexus.ui.textual_widgets import FileChangePreviewScreen, PromptInput, TranscriptLog

if TYPE_CHECKING:
    from rich.console import RenderableType

    from nexus.runtime.agent import Agent
    from nexus.runtime.repl_state import ReplState
    from nexus.runtime.slash_commands import SlashCommandRouter


__all__ = [
    "FileChangePreviewScreen",
    "NexusTextualApp",
    "PromptInput",
    "TextualTerminalUI",
    "TranscriptLog",
    "_DiffRow",
    "_ResponsiveDiff",
    "_bash_command_block",
    "_bash_output_block",
    "_clipboard_commands",
    "_context_pie_icon",
    "_copy_to_system_clipboard",
    "_markdown_code_fence",
    "_renderable_plain_text",
    "_strip_mouse_escape_sequences",
    "_user_prompt_block",
    "can_use_textual_ui",
    "run_textual_repl",
    "shutil",
    "subprocess",
    "sys",
    "time",
]


@dataclass
class _PendingApproval:
    request: ConfirmationRequest
    policy: ApprovalPolicy
    future: asyncio.Future[ConfirmationResponse]


def _clipboard_commands() -> list[list[str]]:
    return _utils_clipboard_commands()


def _copy_to_system_clipboard(text: str) -> bool:
    return _utils_copy_to_system_clipboard(
        text, commands=_clipboard_commands(), run=subprocess.run
    )


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


class NexusTextualApp(App[None]):
    ALLOW_SELECT = True
    CSS = """
    NexusTextualApp {
        layers: base overlay;
    }

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

    #slash-suggestions {
        display: none;
        layer: overlay;
        dock: bottom;
        width: 84;
        max-width: 92%;
        height: 12;
        margin: 0 2 6 4;
        padding: 0 1;
        border: round #666666;
        background: #151515;
        color: $text;
        overflow-y: auto;
    }

    #slash-suggestions > .option-list--option {
        padding: 0;
    }

    #slash-suggestions > .option-list--option-highlighted {
        background: #3b3350;
        color: white;
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
        self._pending_approval: _PendingApproval | None = None
        self._queued_preview_approvals: dict[str, ConfirmationResponse] = {}
        self._active_file_preview_screen: FileChangePreviewScreen | None = None
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
        self._streaming_tool_output_text: dict[str, str] = {}
        self._streaming_tool_output_entries: dict[str, dict[str, Any]] = {}
        self._command_tool_entries: dict[str, dict[str, Any]] = {}
        self._transcript_entries: list[dict[str, Any]] = []
        self._transcript_plain_parts: list[str] = []
        self._file_previews_by_call_id: dict[str, _FileChangePreview] = {}
        self._subagent_entries_by_actor: dict[str, dict[str, Any]] = {}
        self._subagent_entries_by_call_id: dict[str, dict[str, Any]] = {}
        self._subagent_command_entries_by_call_id: dict[str, dict[str, Any]] = {}
        self._supervisor_entry: dict[str, Any] | None = None
        self._supervisor_entries_by_call_id: dict[str, dict[str, Any]] = {}
        self._turn_had_tool_calls = False
        self._transcript: Any = None
        self._status: Any = None
        self._input: Any = None
        self._slash_suggestions: OptionList | None = None
        self._slash_suggestion_commands: tuple[Any, ...] = ()
        self._slash_suggestion_suppressed_value = ""
        self._footer: Any = None
        self._turn_started_at = 0.0
        self._active_transcript_turn_id = ""
        self._turn_tool_count = 0
        self._turn_edit_count = 0
        self._turn_error_count = 0
        self._turn_recovery_count = 0
        self._last_tool_failed = False
        self._turn_footer_written = False
        self._turn_completed_seen = False
        self._prompt_turn_index = 0
        self._prompt_history = [
            message.content
            for message in state.history
            if message.role == "user" and message.content.strip()
        ]
        self._prompt_history_limit = max(
            1, int(getattr(state.config, "prompt_history_max_entries", 200))
        )
        self._prompt_history = self._prompt_history[-self._prompt_history_limit :]
        self._prompt_history_index = len(self._prompt_history)
        self._prompt_history_draft = ""

    def compose(self) -> ComposeResult:
        max_lines = max(
            1, int(getattr(self.state.config, "textual_transcript_max_lines", 5000))
        )
        yield TranscriptLog(
            id="transcript",
            wrap=True,
            highlight=False,
            markup=False,
            max_lines=max_lines,
        )
        yield Static("", id="status")
        with Horizontal(id="input-bar"):
            yield Static("|\n|\n|", id="prompt-marker")
            yield PromptInput(placeholder="Message Nexus or type /help", id="prompt")
        yield OptionList(id="slash-suggestions", compact=True)
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self._transcript = self.query_one("#transcript", TranscriptLog)
        self._status = self.query_one("#status", Static)
        self._input = self.query_one("#prompt", Input)
        self._slash_suggestions = self.query_one("#slash-suggestions", OptionList)
        self._footer = self.query_one("#footer", Static)
        self.console.push_theme(NEXUS_THEME)
        self.state.console = self.ui
        self.state.provider_settings_opener = self.open_provider_settings
        self._render_startup()
        self.refresh_footer()
        self._input.focus()

    def on_unmount(self) -> None:
        self.state.provider_settings_opener = None
        self.state.console = self._original_console

    def open_provider_settings(self) -> None:
        from nexus.ui.provider_settings import ProviderSettingsScreen

        self.push_screen(
            ProviderSettingsScreen(self.state, on_reload=self._reload_provider_settings)
        )

    def _reload_provider_settings(self) -> None:
        from nexus.runtime.slash_commands import _reload_config

        _reload_config(self.state)
        self.refresh_footer()

    def write(self, renderable: RenderableType) -> dict[str, Any]:
        entry = {"type": "renderable", "renderable": renderable}
        self._transcript_entries.append(entry)
        self._render_transcript_entry(entry)
        return entry

    def write_collapsible(
        self,
        header: RenderableType,
        expanded: RenderableType,
        *,
        summary: str = "",
        initially_expanded: bool = False,
        preview: RenderableType | None = None,
    ) -> dict[str, Any]:
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
        return entry

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
            "expanded": Text(f"{_TOOL_ROW_INDENT}Starting sub-agent...", style="dim"),
            "preview": Text(f"{_TOOL_ROW_INDENT}Running...", style="dim"),
            "summary": "running",
            "expanded_state": True,
            "subagent_actor": actor,
            "subagent_call_id": call_id,
            "subagent_title": _subagent_title(args),
            "subagent_tool_order": [],
            "subagent_tool_rows": {},
            "subagent_command_details": {},
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
        rows = cast(
            "dict[str, RenderableType]", entry.setdefault("subagent_tool_rows", {})
        )
        if call_id not in rows:
            order.append(call_id)
        rows[call_id] = row
        self._refresh_subagent_task_entry(entry)
        self._rerender_transcript()

    def record_subagent_command_tool_start(
        self,
        actor: str,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        approval_required: bool = False,
    ) -> None:
        self._record_subagent_command_tool(
            actor,
            call_id,
            tool_name,
            args,
            result=None,
            approval_required=approval_required,
        )

    def record_subagent_command_tool_complete(
        self,
        actor: str,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: ToolResult,
    ) -> None:
        self._record_subagent_command_tool(
            actor, call_id, tool_name, args, result=result
        )

    def _record_subagent_command_tool(
        self,
        actor: str,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        result: ToolResult | None,
        approval_required: bool = False,
    ) -> None:
        entry = self._subagent_entries_by_actor.get(actor)
        if entry is None:
            self.write(
                self.ui._render_subagent_command_tool_row(
                    call_id,
                    tool_name,
                    args,
                    result,
                    expanded=False,
                    approval_required=approval_required,
                )
            )
            return
        details = cast(
            "dict[str, dict[str, Any]]",
            entry.setdefault("subagent_command_details", {}),
        )
        previous = details.get(call_id, {})
        details[call_id] = {
            "tool_name": tool_name,
            "args": dict(args),
            "result": result,
            "expanded": bool(previous.get("expanded", False)),
            "approval_required": approval_required if result is None else False,
            "live_output": ""
            if result is not None
            else str(previous.get("live_output") or ""),
        }
        self._subagent_command_entries_by_call_id[call_id] = entry
        rows = cast(
            "dict[str, RenderableType]", entry.setdefault("subagent_tool_rows", {})
        )
        order = cast("list[str]", entry.setdefault("subagent_tool_order", []))
        if call_id not in rows:
            order.append(call_id)
        rows[call_id] = self._render_subagent_command_detail_row(entry, call_id)
        self._refresh_subagent_task_entry(entry)
        self._rerender_transcript()

    def toggle_subagent_command_detail(self, call_id: str) -> None:
        entry = self._subagent_command_entries_by_call_id.get(call_id)
        if entry is None:
            return
        details = cast(
            "dict[str, dict[str, Any]]", entry.get("subagent_command_details", {})
        )
        detail = details.get(call_id)
        if detail is None:
            return
        detail["expanded"] = not bool(detail.get("expanded", False))
        rows = cast(
            "dict[str, RenderableType]", entry.setdefault("subagent_tool_rows", {})
        )
        rows[call_id] = self._render_subagent_command_detail_row(entry, call_id)
        self._refresh_subagent_task_entry(entry)
        self._rerender_transcript()

    def _render_subagent_command_detail_row(
        self, entry: dict[str, Any], call_id: str
    ) -> RenderableType:
        details = cast(
            "dict[str, dict[str, Any]]", entry.get("subagent_command_details", {})
        )
        detail = details.get(call_id, {})
        return self.ui._render_subagent_command_tool_row(
            call_id,
            str(detail.get("tool_name") or "bash"),
            cast("dict[str, Any]", detail.get("args") or {}),
            cast("ToolResult | None", detail.get("result")),
            expanded=bool(detail.get("expanded", False)),
            approval_required=bool(detail.get("approval_required", False)),
            live_output=str(detail.get("live_output") or ""),
        )

    def update_subagent_command_live_output(self, call_id: str, output: str) -> bool:
        entry = self._subagent_command_entries_by_call_id.get(call_id)
        if entry is None:
            return False
        details = cast(
            "dict[str, dict[str, Any]]", entry.get("subagent_command_details", {})
        )
        detail = details.get(call_id)
        if detail is None:
            return False
        detail["live_output"] = output
        rows = cast(
            "dict[str, RenderableType]", entry.setdefault("subagent_tool_rows", {})
        )
        rows[call_id] = self._render_subagent_command_detail_row(entry, call_id)
        self._refresh_subagent_task_entry(entry)
        self._rerender_transcript()
        return True

    def register_file_preview(self, preview: _FileChangePreview) -> None:
        call_id = preview.request.call_id
        if call_id:
            self._file_previews_by_call_id[call_id] = preview

    def has_file_preview(self, call_id: str) -> bool:
        return call_id in self._file_previews_by_call_id

    def open_file_change_preview_for_call(self, call_id: str) -> None:
        preview = self._file_previews_by_call_id.get(call_id)
        if preview is None:
            return
        self.open_file_change_preview(preview)

    def absorb_approval_entries_for_request(self, request: ConfirmationRequest) -> bool:
        key = _approval_request_key(request)
        kept_entries = [
            entry
            for entry in self._transcript_entries
            if str(entry.get("approval_request_key") or "") != key
        ]
        if len(kept_entries) == len(self._transcript_entries):
            return False
        self._transcript_entries = kept_entries
        self._rerender_transcript()
        return True

    def absorb_tool_start_entries(self, call_id: str) -> None:
        kept_entries = [
            entry
            for entry in self._transcript_entries
            if str(entry.get("tool_start_call_id") or "") != call_id
        ]
        if len(kept_entries) == len(self._transcript_entries):
            return
        self._transcript_entries = kept_entries
        self._rerender_transcript()

    def begin_command_tool_entry(
        self,
        call_id: str,
        *,
        header: RenderableType,
        command: str,
    ) -> None:
        if call_id in self._command_tool_entries:
            return
        body = _command_tool_body(command, "", running=True)
        entry = self.write_collapsible(
            header,
            body,
            summary="running",
            initially_expanded=False,
            preview=None,
        )
        entry["command_tool_call_id"] = call_id
        entry["command_tool_command"] = command
        self._command_tool_entries[call_id] = entry

    def has_command_tool_entry(self, call_id: str) -> bool:
        return call_id in self._command_tool_entries

    def update_command_tool_live_output(self, call_id: str, output: str) -> bool:
        entry = self._command_tool_entries.get(call_id)
        if entry is None:
            return False
        command = str(entry.get("command_tool_command") or "")
        entry["expanded"] = _command_tool_body(command, output, running=True)
        entry["preview"] = None
        entry["summary"] = f"running · {_line_count(output)} lines"
        self._rerender_transcript()
        return True

    def finish_command_tool_entry(
        self,
        call_id: str,
        *,
        header: RenderableType,
        command: str,
        output: str,
        summary: str,
    ) -> None:
        entry = self._command_tool_entries.get(call_id)
        expanded = _command_tool_body(command, output)
        if entry is None:
            entry = self.write_collapsible(
                header,
                expanded,
                summary=summary,
                initially_expanded=False,
                preview=None,
            )
            entry["command_tool_call_id"] = call_id
            self._command_tool_entries[call_id] = entry
            return
        entry["header"] = header
        entry["expanded"] = expanded
        entry["preview"] = None
        entry["summary"] = summary
        entry["expanded_state"] = False
        entry["command_tool_command"] = command
        self._rerender_transcript()

    def has_subagent_task(self, actor: str) -> bool:
        return actor in self._subagent_entries_by_actor

    # ------------------------------------------------------------------
    # Supervisor group – collapsible block for main-agent tool calls
    # ------------------------------------------------------------------

    def begin_supervisor_group(self, header: "RenderableType") -> None:
        entry: dict[str, Any] = {
            "type": "collapsible",
            "id": uuid4().hex[:8],
            "header": header,
            "expanded": Text(f"{_TOOL_ROW_INDENT}Working...", style="dim"),
            "preview": Text(f"{_TOOL_ROW_INDENT}Working...", style="dim"),
            "summary": "running",
            "expanded_state": True,
            "supervisor_tool_order": [],
            "supervisor_tool_rows": {},
        }
        self._supervisor_entry = entry
        self._transcript_entries.append(entry)
        self._render_transcript_entry(entry)

    def record_supervisor_row(self, call_id: str, row: "RenderableType") -> None:
        entry = self._supervisor_entry
        if entry is None:
            self.write(row)
            return
        order = cast("list[str]", entry.setdefault("supervisor_tool_order", []))
        rows = cast(
            "dict[str, RenderableType]", entry.setdefault("supervisor_tool_rows", {})
        )
        if call_id not in rows:
            order.append(call_id)
            self._supervisor_entries_by_call_id[call_id] = entry
        rows[call_id] = row
        self._refresh_supervisor_entry(entry)
        self._rerender_transcript()

    def update_supervisor_row(self, call_id: str, row: "RenderableType") -> None:
        entry = self._supervisor_entries_by_call_id.get(call_id)
        if entry is None:
            return
        rows = cast("dict[str, RenderableType]", entry.get("supervisor_tool_rows", {}))
        rows[call_id] = row
        self._refresh_supervisor_entry(entry)
        self._rerender_transcript()

    def close_supervisor_group(self) -> None:
        entry = self._supervisor_entry
        if entry is None:
            return
        order = cast("list[str]", entry.get("supervisor_tool_order", []))
        count = len(order)
        entry["summary"] = f"{count} call{'s' if count != 1 else ''}"
        entry["expanded_state"] = False
        self._refresh_supervisor_entry(entry)
        self._supervisor_entry = None
        self._rerender_transcript()

    def _refresh_supervisor_entry(self, entry: dict[str, Any]) -> None:
        order = cast("list[str]", entry.get("supervisor_tool_order", []))
        rows = cast("dict[str, RenderableType]", entry.get("supervisor_tool_rows", {}))
        body_parts: list[RenderableType] = [rows[cid] for cid in order if cid in rows]
        entry["expanded"] = (
            Group(*body_parts)
            if body_parts
            else Text(f"{_TOOL_ROW_INDENT}Working...", style="dim")
        )
        preview_rows: list[RenderableType] = [
            rows[cid] for cid in order[:5] if cid in rows
        ]
        entry["preview"] = (
            Group(*preview_rows)
            if preview_rows
            else Text(f"{_TOOL_ROW_INDENT}Working...", style="dim")
        )

    def finish_subagent_task(
        self,
        call_id: str,
        result: ToolResult,
        *,
        elapsed: str = "",
    ) -> None:
        entry = self._subagent_entries_by_call_id.get(
            call_id
        ) or self._subagent_entries_by_actor.get(result.tool_name)
        if entry is None:
            payload = _parse_json_object(result.output)
            header = _subagent_completion_header(result, payload, elapsed=elapsed)
            body = _subagent_result_body(result.output, payload)
            self.write_collapsible(
                header,
                body,
                summary=_subagent_completion_summary(
                    result, payload, elapsed=elapsed, tool_rows=0
                ),
                preview=_subagent_result_preview(result, payload),
            )
            return

        payload = _parse_json_object(result.output)
        tool_rows = len(cast("list[str]", entry.get("subagent_tool_order", [])))
        entry["header"] = _subagent_completion_header(result, payload, elapsed=elapsed)
        entry["summary"] = _subagent_completion_summary(
            result, payload, elapsed=elapsed, tool_rows=tool_rows
        )
        entry["subagent_result_output"] = result.output
        entry["subagent_result_payload"] = payload
        entry["subagent_result_status"] = str(
            payload.get("status")
            or result.metadata.get("status")
            or ("failed" if result.is_error else "completed")
        )
        entry["subagent_result_is_error"] = result.is_error
        entry["subagent_result_elapsed"] = elapsed
        entry["subagent_result_json_expanded"] = False
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
            body_parts.append(
                _subagent_result_summary_block(
                    payload, str(entry.get("subagent_result_status", ""))
                )
            )
        if order:
            body_parts.append(Text(f"{_TOOL_ROW_INDENT}Tool calls", style="dim"))
            body_parts.extend(rows[call_id] for call_id in order if call_id in rows)
        if isinstance(entry.get("subagent_result_output"), str):
            body_parts.append(_subagent_result_json_row(entry))
            if bool(entry.get("subagent_result_json_expanded")):
                body_parts.append(
                    _subagent_result_body(
                        str(entry.get("subagent_result_output") or ""),
                        payload if isinstance(payload, dict) else {},
                    )
                )
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
            entry["preview"] = Group(
                Text(f"{_TOOL_ROW_INDENT}Running tool calls", style="dim"),
                *[rows[call_id] for call_id in order[:3] if call_id in rows],
            )
        else:
            entry["preview"] = Text(f"{_TOOL_ROW_INDENT}Running...", style="dim")
        entry["expanded"] = (
            Group(*body_parts)
            if body_parts
            else Text(f"{_TOOL_ROW_INDENT}Starting sub-agent...", style="dim")
        )

    def toggle_subagent_result_json(self, call_id: str) -> None:
        entry = self._subagent_entries_by_call_id.get(call_id)
        if entry is None:
            return
        entry["subagent_result_json_expanded"] = not bool(
            entry.get("subagent_result_json_expanded")
        )
        self._refresh_subagent_task_entry(entry)
        self._rerender_transcript()

    def toggle_collapsible(self, toggle_id: str) -> None:
        for entry in self._transcript_entries:
            if entry.get("type") == "collapsible" and entry.get("id") == toggle_id:
                file_preview = entry.get("file_preview")
                if isinstance(file_preview, _FileChangePreview):
                    self.open_file_change_preview(file_preview)
                    return
                entry["expanded_state"] = not bool(entry.get("expanded_state"))
                self._rerender_transcript()
                return

    def open_file_change_preview(
        self, preview_info: _FileChangePreview | ConfirmationRequest
    ) -> None:
        if isinstance(preview_info, ConfirmationRequest):
            preview_info = _FileChangePreview(
                preview_info,
                actions_enabled=self._approval_actions_enabled(preview_info),
            )
        request = preview_info.request
        args = {str(key): value for key, value in request.arguments.items()}
        preview = {str(key): value for key, value in request.preview.items()}
        target = self.ui._tool_target(request.tool_name, args)
        title = self._file_preview_title(request, target)
        body = self.ui._render_file_change_editor_preview(
            request.tool_name,
            args,
            preview,
            path=target,
        )
        if self._active_file_preview_screen is not None:
            self._close_active_file_preview()
        screen = FileChangePreviewScreen(
            request,
            title=title,
            preview_renderable=body,
            on_accept=lambda: self._resolve_file_preview_approval(
                request,
                ConfirmationResponse(approved=True, scope="once"),
            ),
            on_reject=lambda: self._resolve_file_preview_approval(
                request, ConfirmationResponse()
            ),
            on_close=lambda: self._clear_active_file_preview_screen(screen),
            actions_enabled=preview_info.actions_enabled,
        )
        self._active_file_preview_screen = screen
        self.push_screen(screen)

    def _approval_actions_enabled(self, request: ConfirmationRequest) -> bool:
        entry = self._file_preview_entry_for_request(request)
        if entry is None:
            return request.kind is ConfirmationKind.APPROVAL and not bool(
                request.payload.get("preview_only")
            )
        if bool(entry.get("approval_resolved")) or bool(
            request.payload.get("preview_only")
        ):
            return False
        return bool(
            entry.get("approval_pending", request.kind is ConfirmationKind.APPROVAL)
        )

    def _file_preview_title(self, request: ConfirmationRequest, target: str) -> Text:
        action = self.ui._semantic_tool_label(request.tool_name)
        title = Text()
        title.append("File Preview", style="bold cyan")
        if action or target:
            title.append(" - ", style="dim")
            title.append(
                " ".join(part for part in (action, target) if part), style="bold white"
            )
        if request.call_id:
            title.append(f"  #{request.call_id[:8]}", style="dim")
        title.append("\nRead-only preview", style="dim")
        return title

    def _file_preview_entry_for_request(
        self, request: ConfirmationRequest
    ) -> dict[str, Any] | None:
        key = _approval_request_key(request)
        for entry in reversed(self._transcript_entries):
            file_preview = entry.get("file_preview")
            if not isinstance(file_preview, _FileChangePreview):
                continue
            preview_request = file_preview.request
            if (
                preview_request.call_id
                and request.call_id
                and preview_request.call_id == request.call_id
            ):
                return entry
            if _approval_request_key(preview_request) == key:
                return entry
        return None

    def _resolve_file_preview_approval(
        self,
        request: ConfirmationRequest,
        response: ConfirmationResponse,
    ) -> None:
        self._mark_approval_resolved(request, response)
        key = _approval_request_key(request)
        pending = self._pending_approval
        if pending is not None and _approval_request_key(pending.request) == key:
            if not pending.future.done():
                pending.future.set_result(response)
            return
        self._queued_preview_approvals[key] = response

    def _mark_approval_resolved(
        self,
        request: ConfirmationRequest,
        response: ConfirmationResponse,
    ) -> None:
        entry = self._file_preview_entry_for_request(request)
        if entry is None:
            return
        entry["approval_pending"] = False
        entry["approval_resolved"] = True
        entry["approval_response"] = response
        entry["expanded_state"] = False
        entry["preview"] = None
        entry["summary"] = _approval_resolution_summary(response)
        entry["header"] = self._resolved_approval_header(request, response)
        file_preview = entry.get("file_preview")
        if isinstance(file_preview, _FileChangePreview):
            entry["file_preview"] = _FileChangePreview(
                file_preview.request, actions_enabled=False
            )
        self._mark_active_file_preview_resolved(request)
        self._rerender_transcript()

    def _resolved_approval_header(
        self,
        request: ConfirmationRequest,
        response: ConfirmationResponse,
    ) -> Text:
        args = {str(key): value for key, value in request.arguments.items()}
        target = self.ui._tool_target(request.tool_name, args)
        action = self.ui._semantic_tool_label(request.tool_name)
        request_detail = (
            f"{action} {target}".strip()
            if request.tool_name in _MUTATING_FILE_TOOLS
            else request.tool_name
        )
        status = _approval_resolution_summary(response)
        detail_parts = [part for part in (request_detail, status) if part]
        call_id = request.call_id[:8] if request.call_id else "pending"
        return self.ui._inline_header(
            "✓ " if response.approved else "✗ ",
            "Approval Request",
            f"{' · '.join(detail_parts)}  #{call_id}",
            style="success" if response.approved else "error",
        )

    def _mark_active_file_preview_resolved(self, request: ConfirmationRequest) -> None:
        screen = self._active_file_preview_screen
        if screen is None:
            return
        if _approval_request_key(screen.request) != _approval_request_key(request):
            return
        screen.mark_resolved()

    def _close_active_file_preview(self) -> None:
        screen = self._active_file_preview_screen
        if screen is None:
            return
        self._active_file_preview_screen = None
        with suppress(Exception):
            screen.dismiss()

    def _clear_active_file_preview_screen(
        self, screen: FileChangePreviewScreen
    ) -> None:
        if self._active_file_preview_screen is screen:
            self._active_file_preview_screen = None

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
        self._scroll_transcript_to_end()

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
        toggle_style = Style(color="cyan", meta={"nexus_toggle": toggle_id})
        line.stylize(toggle_style, 0, min(3, len(line.plain)))
        clickable_path = str(entry.get("clickable_path") or "").strip()
        if clickable_path:
            path_start = line.plain.find(clickable_path)
            if path_start >= 0:
                line.stylize(
                    Style(
                        color="bright_cyan",
                        underline=True,
                        meta={"nexus_toggle": toggle_id},
                    ),
                    path_start,
                    path_start + len(clickable_path),
                )
        if not expanded:
            preview = entry.get("preview")
            if preview is not None:
                return Group(
                    line,
                    _with_inline_toggle(cast("RenderableType", preview), toggle_id),
                )
            return line
        return Group(line, cast("RenderableType", entry.get("expanded", Text(""))))

    def _rerender_transcript(self) -> None:
        if self._transcript is None:
            return
        self._transcript.clear()
        self._transcript_plain_parts.clear()
        for entry in self._transcript_entries:
            self._render_transcript_entry(entry)
        self._scroll_transcript_to_end()

    def _scroll_transcript_to_end(self) -> None:
        if self._transcript is None:
            return
        with suppress(Exception):
            self._transcript.scroll_end(animate=False)

    def begin_turn_transcript(self) -> None:
        turn_id = str(self.state.current_turn_id or "")
        if self._is_continuing_turn_transcript(turn_id):
            return
        self._active_transcript_turn_id = turn_id
        self._turn_started_at = time.perf_counter()
        self._turn_tool_count = 0
        self._turn_edit_count = 0
        self._turn_error_count = 0
        self._turn_recovery_count = 0
        self._last_tool_failed = False
        self._turn_footer_written = False
        self._turn_completed_seen = False
        self._supervisor_entry = None
        self._supervisor_entries_by_call_id.clear()
        self._command_tool_entries.clear()
        self._subagent_command_entries_by_call_id.clear()
        self._turn_had_tool_calls = False
        self._queued_preview_approvals.clear()
        self._close_active_file_preview()
        self.refresh_footer()

    def _is_continuing_turn_transcript(self, turn_id: str) -> bool:
        if self._turn_started_at <= 0 or self._turn_footer_written:
            return False
        if self._active_transcript_turn_id and turn_id:
            return self._active_transcript_turn_id == turn_id
        return True

    def record_tool_completion(self, result: ToolResult) -> None:
        self._turn_tool_count += 1
        if result.tool_name in _MUTATING_FILE_TOOLS:
            self._turn_edit_count += 1
        if result.is_error:
            self._turn_error_count += 1
            self._last_tool_failed = True
            return
        if self._last_tool_failed and (
            result.tool_name in _MUTATING_FILE_TOOLS
            or result.tool_name in _VERIFY_TOOL_NAMES
        ):
            self._turn_recovery_count += 1
        self._last_tool_failed = False

    def mark_turn_completed(self) -> None:
        self._turn_completed_seen = True

    def write_turn_footer_if_completed(self) -> None:
        if not self._turn_completed_seen:
            return
        self.write_turn_footer()

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
        history_tokens = sum(
            estimator.estimate(message.content) for message in self.state.history
        )
        system_tokens = estimator.estimate(self.state.current_system_prompt or "")
        total_tokens = history_tokens + system_tokens
        context_limit = usable_prompt_budget(self.state.config)
        pct = (
            min(100.0, round((total_tokens / context_limit * 100), 1))
            if context_limit
            else 0.0
        )
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
        text.append(
            str(getattr(self.state.config, "agent_mode", "basic")), style="bold magenta"
        )
        text.append("  ")
        text.append("thinking ", style="dim")
        text.append(
            "True" if thinking_enabled else "False",
            style="bold green" if thinking_enabled else "bold red",
        )
        text.append("  ")
        text.append("budget ", style="dim")
        text.append(
            str(self.state.config.llm_reasoning_effort or "none"), style="bold yellow"
        )
        text.append("  ")
        text.append("model ", style="dim")
        text.append(self.state.config.model_name, style="bold white")
        text.append("  ")
        text.append("workspace ", style="dim")
        text.append(workspace, style="white")
        text.append("  ")
        text.append("/provider manage", style="bold cyan")
        if self._status_text:
            text.append("  ")
            text.append("status ", style="dim")
            text.append(self._status_text, style="bold bright_cyan")
        self._footer.update(text)

    def _refresh_slash_command_suggestions(self, value: str) -> None:
        if self._slash_suggestions is None:
            return
        if self._pending_input is not None:
            self._hide_slash_command_suggestions()
            return
        raw = str(value or "").lstrip()
        if self._slash_suggestion_suppressed_value:
            if raw == self._slash_suggestion_suppressed_value:
                self._hide_slash_command_suggestions()
                return
            self._slash_suggestion_suppressed_value = ""
        if not raw.startswith("/"):
            self._hide_slash_command_suggestions()
            return
        query = raw[1:]
        if any(char.isspace() for char in query):
            self._hide_slash_command_suggestions()
            return
        suggestions = self.router.command_suggestions(query)
        if not suggestions:
            self._hide_slash_command_suggestions()
            return
        self._slash_suggestion_commands = suggestions
        self._slash_suggestions.set_options(
            _slash_command_suggestion_options(suggestions)
        )
        self._slash_suggestions.styles.height = min(10, len(suggestions)) + 2
        self._slash_suggestions.highlighted = 0
        self._slash_suggestions.display = True

    def _slash_command_suggestions_visible(self) -> bool:
        return self._slash_suggestions is not None and bool(
            self._slash_suggestions.display
        )

    def _hide_slash_command_suggestions(self) -> bool:
        if self._slash_suggestions is None:
            return False
        was_visible = bool(self._slash_suggestions.display)
        self._slash_suggestions.display = False
        self._slash_suggestions.clear_options()
        self._slash_suggestion_commands = ()
        return was_visible

    def hide_slash_command_suggestions(self) -> bool:
        return self._hide_slash_command_suggestions()

    def move_slash_command_selection(self, delta: int) -> bool:
        palette = self._slash_suggestions
        if palette is None or not palette.display or palette.option_count <= 0:
            return False
        current = palette.highlighted
        if current is None or current < 0:
            next_index = 0
        else:
            next_index = max(0, min(palette.option_count - 1, current + delta))
        palette.highlighted = next_index
        palette.scroll_to_highlight(top=False)
        return True

    def accept_slash_command_selection(self) -> bool:
        palette = self._slash_suggestions
        if palette is None or not palette.display or palette.option_count <= 0:
            return False
        index = palette.highlighted
        if index is None or index < 0:
            index = 0
        return self._accept_slash_command_at_index(index)

    def _accept_slash_command_at_index(self, index: int) -> bool:
        if index < 0 or index >= len(self._slash_suggestion_commands):
            return False
        command_name = str(self._slash_suggestion_commands[index].name).strip()
        if not command_name or self._input is None:
            return False
        value = f"/{command_name}"
        self._slash_suggestion_suppressed_value = value
        self._input.value = value
        self._input.cursor_position = len(value)
        self._hide_slash_command_suggestions()
        self._input.focus()
        return True

    def _render_transcript_plain_text(self, renderable: RenderableType) -> str:
        width = 100
        if self._transcript is not None:
            try:
                width = max(
                    width, int(self._transcript.scrollable_content_region.width or 0)
                )
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
        entry = self.write(Text(f"Input required: {label}", style="warning"))
        self._tag_pending_approval_entry(entry, prompt)

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
                Text("·", style="bold"),
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
        max_chars = max(
            1, int(getattr(self.state.config, "tool_output_max_chars", 100 * 1024))
        )
        current_chars = self._streaming_tool_output_chars.get(call_id, 0)
        if current_chars >= max_chars:
            if call_id not in self._streaming_tool_output_capped:
                self._streaming_tool_output_capped.add(call_id)
                self._append_streaming_tool_output(
                    call_id, f"\n[live output capped at {max_chars} chars]"
                )
            return
        remaining = max_chars - current_chars
        capped_now = False
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
            capped_now = True
        self._streaming_tool_output_chars[call_id] = current_chars + len(chunk)
        if call_id not in self._streaming_tool_outputs:
            self._streaming_tool_outputs.add(call_id)
        prefix = "[stderr] " if stream_name == "stderr" else ""
        self._append_streaming_tool_output(call_id, prefix + chunk)
        if capped_now and call_id not in self._streaming_tool_output_capped:
            self._streaming_tool_output_capped.add(call_id)
            self._append_streaming_tool_output(
                call_id, f"\n[live output capped at {max_chars} chars]"
            )

    def _append_streaming_tool_output(self, call_id: str, chunk: str) -> None:
        output = self._streaming_tool_output_text.get(call_id, "") + chunk
        self._streaming_tool_output_text[call_id] = output
        if self.update_subagent_command_live_output(call_id, output):
            return
        if self.update_command_tool_live_output(call_id, output):
            return
        entry = self._streaming_tool_output_entries.get(call_id)
        if entry is None:
            entry = self.write_collapsible(
                Text(f"> bash live output  #{call_id[:8]}", style="dim"),
                _bash_output_block(output),
                summary=f"streaming · {_line_count(output)} lines",
                preview=_bash_output_block(output, collapsed=True),
            )
            self._streaming_tool_output_entries[call_id] = entry
            return
        entry["expanded"] = _bash_output_block(output)
        entry["preview"] = _bash_output_block(output, collapsed=True)
        entry["summary"] = f"streaming · {_line_count(output)} lines"
        self._rerender_transcript()

    def finish_tool_output_stream(self, call_id: str) -> None:
        entry = self._streaming_tool_output_entries.pop(call_id, None)
        if entry is not None and entry in self._transcript_entries:
            self._transcript_entries.remove(entry)
            self._rerender_transcript()
        self._streaming_tool_outputs.discard(call_id)
        self._streaming_tool_output_chars.pop(call_id, None)
        self._streaming_tool_output_capped.discard(call_id)
        self._streaming_tool_output_text.pop(call_id, None)

    async def on_input_submitted(self, event: Any) -> None:
        raw = _strip_mouse_escape_sequences(str(event.value or "")).strip()
        self._input.value = ""
        self._hide_slash_command_suggestions()
        if raw == "/abort" or raw.startswith("/abort "):
            if await self.router.dispatch(self.state, raw):
                self._record_prompt_history(raw)
                return
        if self._pending_input is not None:
            pending = self._pending_input
            pending_prompt = self._pending_input_prompt
            self._pending_input = None
            if not pending.done():
                entry = self.write(_input_response_block(pending_prompt, raw))
                self._tag_pending_approval_entry(entry, pending_prompt)
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
        self._refresh_slash_command_suggestions(cleaned)
        if cleaned == event.value:
            return
        event.stop()
        self._input.value = cleaned

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._event_inside_slash_suggestions(event):
            return
        event.stop()
        if self._transcript is not None:
            self._transcript.scroll_down(animate=False)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._event_inside_slash_suggestions(event):
            return
        event.stop()
        if self._transcript is not None:
            self._transcript.scroll_up(animate=False)

    def _event_inside_slash_suggestions(self, event: events.MouseEvent) -> bool:
        palette = self._slash_suggestions
        if palette is None or not palette.display:
            return False
        screen_x = getattr(event, "screen_x", None)
        screen_y = getattr(event, "screen_y", None)
        if screen_x is None or screen_y is None:
            return False
        try:
            return palette.region.contains(int(screen_x), int(screen_y))
        except Exception:  # noqa: BLE001
            return False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is not self._slash_suggestions:
            return
        if self._accept_slash_command_at_index(event.option_index):
            event.stop()

    async def ask(self, prompt: str) -> str:
        self._hide_slash_command_suggestions()
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
                        selected_text = (
                            sel_result[0]
                            if isinstance(sel_result, tuple)
                            else sel_result
                        )
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

    def _tag_pending_approval_entry(self, entry: dict[str, Any], prompt: str) -> None:
        if not prompt.strip().lower().startswith("allow?"):
            return
        pending = self._pending_approval
        if pending is None:
            return
        entry["approval_request_key"] = _approval_request_key(pending.request)

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
            del self._prompt_history[
                : len(self._prompt_history) - self._prompt_history_limit
            ]
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
        self.ui.print_banner(
            cfg.provider,
            cfg.model_name,
            self.state.mode.value,
            workspace=cfg.workspace_root,
        )
        if self.session_resumed:
            self.ui.print_session_resumed(
                self.state.session.session_id, len(self.state.history)
            )
        if self.state.has_paused_turn():
            self.ui.print_muted(
                "A previous task was paused after hitting a turn limit. Type `continue` to resume it, or enter a new prompt to start something else."
            )
        self.ui.print_help_hint()
        self._print_provider_notice_or_warning()

    def _print_provider_notice_or_warning(self) -> None:
        from nexus.integrations.registry import provider_has_api_key

        cfg = self.state.config
        if cfg.provider == "fake":
            self.ui.print_fake_provider_notice()
            return
        if cfg.provider == "ollama":
            return
        if not provider_has_api_key(cfg):
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

            effective_prompt, resumed_paused_turn = self.state.consume_turn_prompt(
                raw_input
            )
            if resumed_paused_turn:
                self.ui.print_muted("Resuming paused task...")
            await self._emit_prompt_submit(
                raw_input,
                effective_prompt=effective_prompt,
                resumed_paused_turn=resumed_paused_turn,
            )
            self.state.history.append(Message(role="user", content=raw_input))
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
                self.ui.print_warning("Turn aborted. Type `continue` to resume the interrupted task.")
                if not self.state.has_paused_turn():
                    self.state.mark_paused_turn(effective_prompt, reason="aborted")
                if (
                    user_message_appended
                    and self.state.history
                    and self.state.history[-1].role == "user"
                    and self.state.history[-1].content == raw_input
                ):
                    self.state.history.pop()
                self.state.session.messages = list(self.state.history)
                self.state.session_store.save(self.state.session)
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

    async def _ask_for_approval_response(
        self,
        request: ConfirmationRequest,
        policy: ApprovalPolicy,
    ) -> ConfirmationResponse:
        key = _approval_request_key(request)
        queued = self._queued_preview_approvals.pop(key, None)
        if queued is not None:
            self._mark_approval_resolved(request, queued)
            return queued

        approval_future: asyncio.Future[ConfirmationResponse] = (
            asyncio.get_running_loop().create_future()
        )
        pending = _PendingApproval(
            request=request, policy=policy, future=approval_future
        )
        self._pending_approval = pending
        keyboard_task: asyncio.Task[str] | None = None
        try:
            while True:
                queued = self._queued_preview_approvals.pop(key, None)
                if queued is not None:
                    self._mark_approval_resolved(request, queued)
                    return queued
                keyboard_task = asyncio.create_task(
                    self.ask(f"Allow? {approval_prompt_label(policy)}")
                )
                done, _pending = await asyncio.wait(
                    {keyboard_task, approval_future},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if approval_future in done:
                    if not keyboard_task.done():
                        keyboard_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await keyboard_task
                    response = approval_future.result()
                    self._mark_approval_resolved(request, response)
                    return response

                answer = keyboard_task.result().strip().lower()
                response = approval_response_from_answer(answer, policy)
                if response.approved or _is_explicit_denial_answer(answer):
                    self._mark_approval_resolved(request, response)
                    return response
                self.ui.print_muted(
                    "Please answer with yes/y (or t/turn when offered), or no/n."
                )
        finally:
            if self._pending_approval is pending:
                self._pending_approval = None
            if keyboard_task is not None and not keyboard_task.done():
                keyboard_task.cancel()
                with suppress(asyncio.CancelledError):
                    await keyboard_task
            self._close_active_file_preview()

    def _approval_callback(self) -> ConfirmationCallback:
        async def ask_for_approval(
            request: ConfirmationRequest,
        ) -> ConfirmationResponse:
            if request.kind is ConfirmationKind.CLARIFICATION:
                if is_ask_user_confirmation(request):
                    while True:
                        answer = await self.ask(ask_user_input_prompt(request))
                        response, error = parse_ask_user_response(request, answer)
                        if response is not None:
                            return response
                        self.ui.print_muted(error or "A valid answer is required.")
                field = request.payload.get("field", "value")
                while True:
                    answer = await self.ask(f"Value for {field!r}:")
                    clarified = answer.strip()
                    if clarified:
                        return ConfirmationResponse(clarification=clarified)
                    self.ui.print_muted(
                        "A value is required. Provide input or cancel with Ctrl+C."
                    )

            try:
                policy = approval_policy_for_request(request)
            except Exception:
                # Tolerate malformed approval payloads and keep prompting.
                policy = ApprovalPolicy.ON_REQUEST

            return await self._ask_for_approval_response(request, policy)

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
