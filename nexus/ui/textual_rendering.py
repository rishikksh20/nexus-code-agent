"""Rich render helpers for the Textual Nexus UI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher, unified_diff
import json
import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich import box
from rich.console import Console
from rich.console import ConsoleOptions
from rich.console import Group
from rich.console import RenderResult
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from nexus.models import (
    AgentEventType,
    ConfirmationKind,
    ConfirmationRequest,
    ToolResult,
)
from nexus.runtime.clarifications import (
    ask_user_display_lines,
    is_ask_user_confirmation,
)
from nexus.ui.terminal import (
    TerminalUI,
    _MAX_TOOL_PARAM_SUMMARY_CHARS,
    _solid_ascii_banner,
    _tool_failure_reason,
)
from nexus.ui.textual_utils import _approval_request_key, _thinking_label

if TYPE_CHECKING:
    from rich.console import RenderableType

    from nexus.models import AgentEvent
    from nexus.ui.textual_app import NexusTextualApp

_COLLAPSED_PREVIEW_LINES = 15
_COLLAPSE_LINE_LIMIT = 18
_COLLAPSE_CHAR_LIMIT = 2400
_ALERT_PREVIEW_CHARS = 150
_COMMAND_PREVIEW_CHARS = 1600
_SIDE_BY_SIDE_DIFF_WIDTH = 104
_DIFF_EDITOR_BACKGROUND = "#272822"
_DIFF_DELETE_BACKGROUND = "#3a2020"
_DIFF_ADD_BACKGROUND = "#203a24"
_MUTATING_FILE_TOOLS = {"write_file", "edit", "insert_edit_into_file", "apply_patch"}
_VERIFY_TOOL_NAMES = {"bash", "run_tests", "run_python_check"}
_AGENT_BULLET = "● "
_TOOL_ROW_INDENT = "   "


@dataclass(frozen=True)
class _DiffRow:
    before_number: int | None
    after_number: int | None
    before: str
    after: str
    kind: str


@dataclass(frozen=True)
class _ResponsiveDiff:
    """Render a file diff using the width available at paint time."""

    rows: tuple[_DiffRow, ...]
    path: str = ""
    language: str = "text"
    new_file: bool = False
    toggle_id: str = ""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        del console
        yield _render_diff_layout(
            list(self.rows),
            path=self.path,
            language=self.language,
            width=max(1, options.max_width),
            new_file=self.new_file,
            toggle_id=self.toggle_id,
        )


@dataclass(frozen=True)
class _FencedCodeBlock:
    """Render command text or console output as a Markdown code fence."""

    value: str
    language: str = "text"
    label: str = ""
    label_style: str = "dim"
    collapsed: bool = False
    toggle_id: str = ""

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        del console, options
        value = self.value.rstrip() or "(empty)"
        truncated = False
        if self.collapsed:
            value, truncated = _bounded_command_preview(value)
        blocks: list[RenderableType] = []
        if self.label:
            blocks.append(Text(self.label, style=self.label_style))
        blocks.append(Markdown(_markdown_code_fence(value, language=self.language)))
        if truncated:
            hint = Text("... click [+] to expand", style="dim")
            if self.toggle_id:
                hint.stylize(Style(meta={"nexus_toggle": self.toggle_id}))
            blocks.append(hint)
        yield Group(*blocks)


@dataclass(frozen=True)
class _FileChangePreview:
    request: ConfirmationRequest
    actions_enabled: bool = True


class TextualTerminalUI(TerminalUI):
    """TerminalUI-compatible adapter that writes Rich renderables to Textual."""

    def __init__(self, app: "NexusTextualApp") -> None:
        super().__init__(color=True)
        self._app = app
        self._tool_started_at: dict[str, float] = {}

    def _write(self, renderable: RenderableType) -> dict[str, Any]:
        return self._app.write(renderable)

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
        raise RuntimeError(
            "TextualTerminalUI input is asynchronous; use NexusTextualApp.ask()."
        )

    def prompt_user(self) -> str:
        raise RuntimeError(
            "TextualTerminalUI prompt_user is handled by the Textual input widget."
        )

    def print_error(self, msg: str) -> None:
        self.end_assistant()
        self._write_alert(
            "Request failed", msg, title_style="bold red", body_style="red on #2a1717"
        )

    def print_warning(self, msg: str) -> None:
        self._write_alert(
            "Warning", msg, title_style="bold yellow", body_style="yellow on #2b2516"
        )

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
        self._workspace_root = (
            Path(workspace).resolve() if workspace is not None else None
        )
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
        self._write(
            Text(
                "Type /help for commands, /skills for skill control, /abort to stop a running turn, or /quit to exit.",
                style="dim",
            )
        )

    def print_fake_provider_notice(self) -> None:
        self._write(
            Panel(
                Text(
                    "Using the fake provider. Responses are mocked; run /setup to choose a real model, or set a real provider, API_KEY, and BASE_URL in .env.",
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
        self._app.close_supervisor_group()
        if self._app._turn_had_tool_calls:
            self._write(Text(""))
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

    def _semantic_tool_label(
        self, tool_name: str, *, completed: bool = False, failed: bool = False
    ) -> str:
        if failed:
            return "Failed"
        labels = {
            "bash": "Ran" if completed else "Run",
            "write_file": "Wrote file" if completed else "Write file",
            "edit": "Edited file" if completed else "Edit file",
            "insert_edit_into_file": "Edited file" if completed else "Edit file",
            "apply_patch": "Patched files" if completed else "Patch files",
            "read_file": "Read" if completed else "Read",
            "list_dir": "Listed" if completed else "List",
            "grep": "Searched" if completed else "Search",
            "glob": "Found" if completed else "Find",
            "run_tests": "Tested" if completed else "Test",
            "run_python_check": "Checked" if completed else "Check",
            "run_formatter": "Formatted" if completed else "Format",
            "git_status": "Git status" if completed else "Git status",
            "git_diff": "Git diff" if completed else "Git diff",
        }
        if tool_name.startswith("subagent_"):
            return "Delegated" if completed else "Delegate"
        return labels.get(tool_name, tool_name)

    def _elapsed_label(self, call_id: str, result: ToolResult | None = None) -> str:
        duration = None
        if result is not None:
            raw_duration = (
                result.metadata.get("duration_ms")
                if isinstance(result.metadata, dict)
                else None
            )
            if isinstance(raw_duration, (int, float)):
                duration = float(raw_duration) / 1000
        if duration is None and call_id in self._tool_started_at:
            duration = max(0.0, time.perf_counter() - self._tool_started_at[call_id])
        if duration is None:
            return ""
        if duration < 1:
            return f"{duration * 1000:.0f}ms"
        return f"{duration:.1f}s"

    def _tool_target(
        self, tool_name: str, args: dict[str, Any], result: ToolResult | None = None
    ) -> str:
        metadata = (
            result.metadata
            if result is not None and isinstance(result.metadata, dict)
            else {}
        )
        path = metadata.get("path") or args.get("path") or args.get("cwd")
        if isinstance(path, str) and path.strip():
            return self._relative_path(path)
        command = _command_from_arguments(args)
        if command:
            return self._truncate_preview(command, limit=100)
        if tool_name.startswith("subagent_"):
            return self._compact_tool_detail(tool_name, args)
        return self._compact_tool_detail(tool_name, args)

    def _tool_label_style(
        self, tool_name: str, result: ToolResult | None = None
    ) -> str:
        if result is not None and result.is_error:
            return "error"
        if result is not None and tool_name == "write_file":
            return "bold tool.write"
        return self._tool_border_style(tool_name)

    def _block_text(self, *lines: str, style: str = "default") -> Text:
        text = Text()
        for index, line in enumerate(lines):
            if index:
                text.append("\n")
            text.append(line, style=style)
        return text

    def _inline_header(
        self, prefix: str, title: str, detail: str = "", *, style: str = "tool"
    ) -> Text:
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

    def _command_start_header(self, call_id: str) -> Text:
        text = Text("\n. ", style="dim")
        text.append("Run Command :", style="bold tool.shell")
        text.append(f" #{call_id[:8]}", style="bold tool.shell")
        return text

    def _command_tool_start_header(
        self,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> Text:
        command = _command_for_tool(tool_name, args)
        detail = self._tool_target(tool_name, args) or self._truncate_preview(
            command, limit=100
        )
        label = _command_tool_title(tool_name)
        return self._inline_header(
            "· ",
            label,
            f"{detail}  #{call_id[:8]}",
            style=self._tool_border_style(tool_name),
        )

    def _command_tool_complete_header(
        self,
        result: ToolResult,
        args: dict[str, Any],
        command: str,
    ) -> Text:
        status = "failed" if result.is_error else "done"
        target = self._tool_target(
            result.tool_name, args, result
        ) or self._truncate_preview(command, limit=100)
        detail_parts = [target, status]
        if result.is_error:
            detail_parts[-1] = f"failed: {_tool_failure_reason(result)}"
        header = self._inline_header(
            "✗ " if result.is_error else "✓ ",
            _command_tool_title(result.tool_name),
            " · ".join(part for part in detail_parts if part),
            style="error"
            if result.is_error
            else self._tool_border_style(result.tool_name),
        )
        self._append_elapsed(header, self._elapsed_label(result.call_id, result))
        return header

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
        command = _command_from_arguments(args)
        if command:
            return Group(
                self._command_start_header(call_id),
                _bash_command_block(command),
            )
        target = self._tool_target(tool_name, args)
        style = self._tool_border_style(tool_name)
        return self._inline_header(
            "> ", label, f"{target}  #{call_id[:8]}", style=style
        )

    def _write_inline_tool_start(
        self,
        call_id: str,
        tool_name: str,
        actor: str,
        args: dict[str, Any],
        display: dict[str, Any],
    ) -> None:
        if _is_command_like_tool(tool_name, args):
            self._tool_started_at[call_id] = time.perf_counter()
            self._app.begin_command_tool_entry(
                call_id,
                header=self._command_tool_start_header(call_id, tool_name, args),
                command=_command_for_tool(tool_name, args),
            )
            return
        self._write(
            self._render_inline_tool_start(call_id, tool_name, actor, args, display)
        )

    def _render_subagent_header(self, tool_name: str, args: dict[str, Any]) -> Text:
        title = _subagent_title(args)
        header = Text(_AGENT_BULLET, style="bold magenta")
        header.append(_subagent_task_label(tool_name), style="bold magenta")
        if title:
            header.append(" - ", style="dim")
            header.append(title, style="bold white")
        return header

    def _render_supervisor_header(self) -> Text:
        return Text(f"{_AGENT_BULLET}Supervisor Agent", style="bold cyan")

    def _render_subagent_tool_row(
        self,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: ToolResult | None = None,
        *,
        file_preview_call_id: str = "",
    ) -> Text:
        label = self._semantic_tool_label(
            tool_name,
            completed=result is not None,
            failed=bool(result.is_error) if result is not None else False,
        )
        if result is not None and result.is_error:
            label = tool_name
        target = self._tool_target(tool_name, args, result)
        style = self._tool_label_style(tool_name, result)
        has_file_preview = bool(file_preview_call_id)
        if result is None:
            row = Text(f"{_TOOL_ROW_INDENT}· ", style="dim")
        elif result.is_error:
            row = Text(f"{_TOOL_ROW_INDENT}✗ ", style="bold red")
        elif has_file_preview:
            row = Text(f"{_TOOL_ROW_INDENT}", style="dim")
            marker_start = len(row.plain)
            row.append("[+] ", style="cyan")
            row.stylize(
                Style(
                    color="cyan",
                    meta={"nexus_file_preview_call_id": file_preview_call_id},
                ),
                marker_start,
                len(row.plain),
            )
            row.append("✓ ", style="bold green")
        else:
            row = Text(f"{_TOOL_ROW_INDENT}✓ ", style="bold green")
        row.append(label, style=style)
        if target:
            row.append(" ", style="dim")
            target_start = len(row.plain)
            row.append(target, style="dim")
            if has_file_preview:
                row.stylize(
                    Style(
                        color="bright_cyan",
                        underline=True,
                        meta={"nexus_file_preview_call_id": file_preview_call_id},
                    ),
                    target_start,
                    len(row.plain),
                )
        row.append(f"  #{call_id[:8]}", style="dim")
        if result is None:
            row.append(" · running", style="dim")
            return row
        if result.is_error:
            row.append(" · failed", style="bold red")
            reason = _tool_failure_reason(result)
            if reason:
                row.append(
                    f": {self._truncate_preview(reason, limit=120)}", style="dim red"
                )
        else:
            row.append(" · done", style="dim")
        self._append_elapsed(row, self._elapsed_label(call_id, result))
        return row

    def _render_subagent_command_tool_row(
        self,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: ToolResult | None = None,
        *,
        expanded: bool = False,
        approval_required: bool = False,
        live_output: str = "",
    ) -> RenderableType:
        marker = "[-]" if expanded else "[+]"
        marker_style = Style(
            color="cyan", meta={"nexus_subagent_command_call_id": call_id}
        )
        row = Text(f"{_TOOL_ROW_INDENT}", style="dim")
        marker_start = len(row.plain)
        row.append(f"{marker} ", style="cyan")
        row.stylize(marker_style, marker_start, len(row.plain))
        if result is None:
            row.append(
                "? " if approval_required else "· ",
                style="bold yellow" if approval_required else "dim",
            )
        elif result.is_error:
            row.append("✗ ", style="bold red")
        else:
            row.append("✓ ", style="bold green")
        label_start = len(row.plain)
        row.append(
            _command_tool_title(tool_name),
            style="error"
            if result is not None and result.is_error
            else self._tool_border_style(tool_name),
        )
        row.stylize(
            Style(
                color="bright_cyan",
                underline=True,
                meta={"nexus_subagent_command_call_id": call_id},
            ),
            label_start,
            len(row.plain),
        )
        command = _command_for_tool(tool_name, args, result)
        if command:
            row.append(" ", style="dim")
            row.append(self._truncate_preview(command, limit=100), style="dim")
        row.append(f"  #{call_id[:8]}", style="dim")
        if result is None:
            row.append(
                " · approval required" if approval_required else " · running",
                style="warning" if approval_required else "dim",
            )
            if not expanded:
                return row
            return Group(
                row,
                Padding(
                    _command_tool_body(command, live_output, running=True), (0, 0, 0, 6)
                ),
            )
        if result.is_error:
            row.append(
                f" · failed: {self._truncate_preview(_tool_failure_reason(result), limit=120)}",
                style="dim red",
            )
        else:
            row.append(" · done", style="dim")
        self._append_elapsed(row, self._elapsed_label(call_id, result))
        if not expanded:
            return row
        output = (
            _tool_failure_reason(result)
            if result.is_error
            else (result.output or "").strip()
        )
        if not output:
            output = "(no output)"
        return Group(row, Padding(_command_tool_body(command, output), (0, 0, 0, 6)))

    def _render_supervisor_row(
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
        if result is not None and result.is_error:
            label = tool_name
        target = self._tool_target(tool_name, args, result)
        style = self._tool_label_style(tool_name, result)
        if result is None:
            row = Text(f"{_TOOL_ROW_INDENT}· ", style="dim")
        elif result.is_error:
            row = Text(f"{_TOOL_ROW_INDENT}✗ ", style="bold red")
        else:
            row = Text(f"{_TOOL_ROW_INDENT}✓ ", style="bold green")
        row.append(label, style=style)
        if target:
            row.append(f" {target}", style="dim")
        if result is None:
            return row
        if result.is_error:
            row.append(" · failed", style="bold red")
            reason = _tool_failure_reason(result)
            if reason:
                row.append(
                    f": {self._truncate_preview(reason, limit=160)}", style="dim red"
                )
        else:
            self._append_elapsed(row, self._elapsed_label(call_id, result))
        return row

    def _render_bash_complete(self, result: ToolResult, args: dict[str, Any]) -> None:
        self._render_command_tool_complete(result, args)

    def _render_command_tool_complete(
        self, result: ToolResult, args: dict[str, Any]
    ) -> None:
        command = _command_for_tool(result.tool_name, args, result)
        approval_request = ConfirmationRequest(
            kind=ConfirmationKind.APPROVAL,
            tool_name=result.tool_name,
            prompt="",
            reason="Command has already run.",
            payload={"preview_only": True},
            call_id=result.call_id,
            arguments=dict(args),
            preview={"command": command} if command else {},
        )
        absorbed_approval = self._app.absorb_approval_entries_for_request(
            approval_request
        )
        if absorbed_approval:
            self._app.absorb_tool_start_entries(result.call_id)
        output = (
            _tool_failure_reason(result)
            if result.is_error
            else (result.output or "").strip()
        )
        if not output:
            output = "(no output)"
        self._app.finish_command_tool_entry(
            result.call_id,
            header=self._command_tool_complete_header(result, args, command),
            command=command,
            output=output,
            summary=_command_tool_summary(result, output),
        )

    def _render_confirmation_preview(
        self,
        tool_name: str,
        args: dict[str, Any],
        preview: dict[str, Any],
    ) -> tuple[RenderableType, RenderableType, str] | None:
        diff_data = _diff_data_from_preview(preview) or _diff_data_from_arguments(
            tool_name, args
        )
        diff = _diff_text_from_data(diff_data) or _diff_from_arguments(tool_name, args)
        if not diff and tool_name == "apply_patch":
            diff = str(args.get("patch", "") or "")
        if diff:
            target = self._tool_target(tool_name, args)
            return (
                self._render_file_diff_preview(
                    tool_name, diff_data, diff, path=target, collapsed=False
                ),
                self._render_file_diff_preview(
                    tool_name, diff_data, diff, path=target, collapsed=True
                ),
                _diff_summary(diff),
            )
        command = _command_from_preview_or_arguments(preview, args)
        if command:
            return (
                _bash_command_block(command),
                _bash_command_block(command, collapsed=True),
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
        target = self._tool_target(req.tool_name, args)
        action = self._semantic_tool_label(req.tool_name)
        request_detail = (
            f"{action} {target}".strip()
            if req.tool_name in _MUTATING_FILE_TOOLS
            else display_name
        )
        header = self._inline_header(
            "? ",
            "Approval required",
            f"{request_detail}  #{req.call_id[:8] or 'pending'}",
            style="warning",
        )
        detail = self._render_approval_detail(req.tool_name, args, req.reason, policy)
        rendered_preview = self._render_confirmation_preview(
            req.tool_name, args, preview
        )
        if rendered_preview is None:
            entry = self._write(Group(header, detail))
            if isinstance(entry, dict):
                entry["approval_request_key"] = _approval_request_key(req)
            return
        expanded, collapsed_preview, summary = rendered_preview
        entry = self._app.write_collapsible(
            header,
            Group(detail, expanded),
            summary=summary,
            initially_expanded=False,
            preview=Group(collapsed_preview, detail),
        )
        entry["approval_request_key"] = _approval_request_key(req)
        if req.tool_name in _MUTATING_FILE_TOOLS:
            entry["file_preview"] = _FileChangePreview(req, actions_enabled=True)
            entry["approval_pending"] = True
            entry["clickable_path"] = target

    def _render_file_change_complete(
        self,
        result: ToolResult,
        args: dict[str, Any],
        preview: dict[str, Any],
    ) -> None:
        elapsed = self._elapsed_label(result.call_id, result)
        target = self._tool_target(result.tool_name, args, result)
        label = self._semantic_tool_label(
            result.tool_name, completed=True, failed=result.is_error
        )
        if result.is_error:
            label = result.tool_name
        detail = " · ".join(
            part for part in (target, "failed" if result.is_error else "done") if part
        )
        header = self._inline_header(
            "✗ " if result.is_error else "✓ ",
            label,
            detail,
            style=self._tool_label_style(result.tool_name, result),
        )
        self._append_elapsed(header, elapsed)
        if result.is_error:
            body_text = f"{result.tool_name} failed: {_tool_failure_reason(result)}"
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

        diff_data = _diff_data_from_preview(preview)
        diff = _diff_text_from_data(diff_data) or _diff_from_arguments(
            result.tool_name, args
        )
        if not diff and ("old_content" in diff_data or "new_content" in diff_data):
            diff = _unified_diff_text(
                str(diff_data.get("old_content", "") or ""),
                str(diff_data.get("new_content", "") or ""),
                path=target or str(diff_data.get("path") or "file"),
            )
        if not diff:
            self._write(header)
            return

        diff_renderable = self._render_file_diff_preview(
            result.tool_name, diff_data, diff, path=target, collapsed=False
        )
        file_preview = self._file_change_preview_info(result, args, preview)
        if file_preview is not None:
            self._app.register_file_preview(file_preview)
            self._app.absorb_approval_entries_for_request(file_preview.request)
        entry = self._app.write_collapsible(
            header,
            diff_renderable,
            summary=_diff_summary(diff),
            initially_expanded=False,
            preview=self._render_file_diff_preview(
                result.tool_name, diff_data, diff, path=target, collapsed=True
            ),
        )
        if file_preview is not None:
            entry["file_preview"] = file_preview
            entry["clickable_path"] = target

    def _file_change_preview_info(
        self,
        result: ToolResult,
        args: dict[str, Any],
        preview: dict[str, Any],
    ) -> _FileChangePreview | None:
        if result.is_error or result.tool_name not in _MUTATING_FILE_TOOLS:
            return None
        target = self._tool_target(result.tool_name, args, result)
        diff_data = _diff_data_from_preview(preview)
        diff = _diff_text_from_data(diff_data) or _diff_from_arguments(
            result.tool_name, args
        )
        if not diff and result.tool_name == "apply_patch":
            diff = str(args.get("patch", "") or "")
        if not diff and ("old_content" in diff_data or "new_content" in diff_data):
            diff = _unified_diff_text(
                str(diff_data.get("old_content", "") or ""),
                str(diff_data.get("new_content", "") or ""),
                path=target or str(diff_data.get("path") or "file"),
            )
        if not diff:
            return None
        request = ConfirmationRequest(
            kind=ConfirmationKind.APPROVAL,
            tool_name=result.tool_name,
            prompt="",
            reason="File change has already been applied.",
            payload={"preview_only": True},
            call_id=result.call_id,
            arguments=dict(args),
            preview=dict(preview),
        )
        return _FileChangePreview(request, actions_enabled=False)

    def _render_diff_editor(self, diff_text: str, *, path: str = "") -> _ResponsiveDiff:
        return self._render_diff_rows(
            _diff_rows_from_unified_diff(diff_text), path=path
        )

    def _render_diff_editor_preview(
        self, diff_text: str, *, path: str = ""
    ) -> _ResponsiveDiff:
        rows = _collapsed_diff_rows(
            _diff_rows_from_unified_diff(diff_text), limit=_COLLAPSED_PREVIEW_LINES
        )
        return self._render_diff_rows(rows, path=path)

    def _render_diff_rows(
        self, rows: list[_DiffRow], *, path: str = ""
    ) -> _ResponsiveDiff:
        return _ResponsiveDiff(
            rows=tuple(rows),
            path=path,
            language=self._guess_language(path),
        )

    def _render_file_diff_preview(
        self,
        tool_name: str,
        diff_data: dict[str, Any],
        diff_text: str,
        *,
        path: str = "",
        collapsed: bool = False,
    ) -> _ResponsiveDiff:
        old_content = str(diff_data.get("old_content", "") or "")
        new_content = str(diff_data.get("new_content", "") or "")
        has_contents = "old_content" in diff_data or "new_content" in diff_data
        if _should_render_as_new_file(tool_name, diff_data):
            return _render_new_file_preview(
                new_content,
                path=path,
                language=self._guess_language(path),
                collapsed=collapsed,
            )
        if has_contents:
            rows = _diff_rows_from_contents(old_content, new_content)
        else:
            rows = _diff_rows_from_unified_diff(diff_text)
        if collapsed:
            rows = _collapsed_diff_rows(rows, limit=_COLLAPSED_PREVIEW_LINES)
        return self._render_diff_rows(rows, path=path)

    def _render_file_change_editor_preview(
        self,
        tool_name: str,
        args: dict[str, Any],
        preview: dict[str, Any],
        *,
        path: str = "",
    ) -> RenderableType:
        diff_data = _diff_data_from_preview(preview) or _diff_data_from_arguments(
            tool_name, args
        )
        diff_text = _diff_text_from_data(diff_data) or _diff_from_arguments(
            tool_name, args
        )
        if not diff_text and tool_name == "apply_patch":
            diff_text = str(args.get("patch", "") or "")

        old_content = str(diff_data.get("old_content", "") or "")
        new_content = str(diff_data.get("new_content", "") or "")
        if "old_content" in diff_data or "new_content" in diff_data:
            rows = _diff_rows_from_contents(old_content, new_content)
        else:
            rows = _diff_rows_from_unified_diff(diff_text)
        if not rows and (old_content or new_content):
            rows = _diff_rows_from_contents(old_content, new_content, context=0)
        if not rows:
            return Text("(no file preview available)", style="dim")
        return self._render_diff_rows(rows, path=path)

    def _render_approval_detail(
        self,
        tool_name: str,
        args: dict[str, Any],
        reason: str,
        policy: str,
    ) -> Table:
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(style="dim", no_wrap=True, width=9)
        table.add_column(ratio=1, overflow="fold")
        visible_args = args
        if tool_name == "bash":
            visible_args = {
                key: value for key, value in args.items() if key != "command"
            }
        if visible_args:
            table.add_row(
                "params", self._approval_params_summary(tool_name, visible_args)
            )
        table.add_row("reason", Text(reason, style="dim"))
        table.add_row(
            "approval",
            Text(
                self._approval_choices(policy).replace("Approval: ", ""),
                style="warning",
            ),
        )
        return table

    def _approval_params_summary(self, tool_name: str, args: dict[str, Any]) -> Text:
        parts = [
            f"{key}={self._compact_value(key, value)}"
            for key, value in self._ordered_args(tool_name, args)
        ]
        return Text(
            self._truncate_preview(
                ", ".join(parts), limit=_MAX_TOOL_PARAM_SUMMARY_CHARS
            ),
            style="tool.args",
        )

    def _render_generic_complete(
        self, result: ToolResult, args: dict[str, Any]
    ) -> None:
        elapsed = self._elapsed_label(result.call_id, result)
        label = self._semantic_tool_label(
            result.tool_name, completed=True, failed=result.is_error
        )
        if result.is_error:
            label = result.tool_name
        detail = self._tool_target(result.tool_name, args, result)
        header = self._inline_header(
            "✗ " if result.is_error else "✓ ",
            label,
            detail,
            style="error"
            if result.is_error
            else self._tool_border_style(result.tool_name),
        )
        self._append_elapsed(header, elapsed)
        if not result.is_error and (
            not result.output
            or result.tool_name in {"read_file", "grep", "glob", "list_dir"}
        ):
            self._write(header)
            return
        output = (
            f"{result.tool_name} failed: {_tool_failure_reason(result)}"
            if result.is_error
            else result.output
        )
        body = self._preview_block(
            output,
            path=str(
                result.metadata.get("path", "")
                if isinstance(result.metadata, dict)
                else ""
            ),
        )
        if _should_collapse_text(result.output):
            self._app.write_collapsible(
                header,
                body,
                summary=f"{_line_count(result.output)} lines",
                preview=_preview_text_block(output),
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
                        Text("·", style="bold"),
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
            arguments = (
                payload.get("arguments", {})
                if isinstance(payload.get("arguments", {}), dict)
                else {}
            )
            preview = (
                payload.get("preview", {})
                if isinstance(payload.get("preview", {}), dict)
                else {}
            )
            display = (
                payload.get("display", {})
                if isinstance(payload.get("display", {}), dict)
                else {}
            )
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
                if _is_command_like_tool(tool_name, arguments):
                    self._app.record_subagent_command_tool_start(
                        actor, call_id, tool_name, arguments
                    )
                else:
                    self._app.record_subagent_tool_row(
                        actor,
                        call_id,
                        self._render_subagent_tool_row(call_id, tool_name, arguments),
                    )
            elif _is_supervisor_group_tool(tool_name, arguments):
                self._tool_started_at[call_id] = time.perf_counter()
                self._app._turn_had_tool_calls = True
                if self._app._supervisor_entry is None:
                    self._app.begin_supervisor_group(self._render_supervisor_header())
                self._app.record_supervisor_row(
                    call_id,
                    self._render_supervisor_row(call_id, tool_name, arguments),
                )
            else:
                self._app._turn_had_tool_calls = True
                self._write_inline_tool_start(
                    call_id, tool_name, actor, arguments, display
                )
            self.start_tool_wait(f"{self._tool_display_name(tool_name, actor)} running")
            return

        if event.kind == AgentEventType.TOOL_CALL_COMPLETE and show_tool_calls:
            self.end_assistant()
            result = cast("ToolResult", event.payload)
            if result is None:
                return
            if isinstance(result.metadata, dict) and result.metadata.get(
                "tool_unavailable"
            ):
                self._app.finish_tool_output_stream(result.call_id)
                self._clear_tool_call_state(result.call_id)
                self._tool_started_at.pop(result.call_id, None)
                return
            preview = self._tool_preview_by_call_id.get(result.call_id, {})
            actor = str(
                result.metadata.get("actor")
                or self._tool_actor_by_call_id.get(result.call_id, "")
            ).strip()
            display = self._tool_display_by_call_id.get(result.call_id, {})
            del display
            arguments = self._tool_args_by_call_id.get(result.call_id, {})
            self._app.record_tool_completion(result)
            self._app.finish_tool_output_stream(result.call_id)
            if _is_subagent_tool(result.tool_name):
                self._app.finish_subagent_task(
                    result.call_id,
                    result,
                    elapsed=self._elapsed_label(result.call_id, result),
                )
            elif _is_subagent_actor(actor) and self._app.has_subagent_task(actor):
                file_preview = self._file_change_preview_info(
                    result, arguments, preview
                )
                file_preview_call_id = ""
                if file_preview is not None:
                    self._app.register_file_preview(file_preview)
                    self._app.absorb_approval_entries_for_request(file_preview.request)
                    file_preview_call_id = result.call_id
                if _is_command_like_tool(result.tool_name, arguments):
                    approval_request = ConfirmationRequest(
                        kind=ConfirmationKind.APPROVAL,
                        tool_name=result.tool_name,
                        prompt="",
                        reason="Command has already run.",
                        payload={"preview_only": True},
                        call_id=result.call_id,
                        arguments=dict(arguments),
                        preview={
                            "command": _command_for_tool(
                                result.tool_name, arguments, result
                            )
                        },
                    )
                    self._app.absorb_approval_entries_for_request(approval_request)
                    self._app.record_subagent_command_tool_complete(
                        actor,
                        result.call_id,
                        result.tool_name,
                        arguments,
                        result,
                    )
                else:
                    self._app.record_subagent_tool_row(
                        actor,
                        result.call_id,
                        self._render_subagent_tool_row(
                            result.call_id,
                            result.tool_name,
                            arguments,
                            result,
                            file_preview_call_id=file_preview_call_id,
                        ),
                    )
            elif result.call_id in self._app._supervisor_entries_by_call_id:
                self._app.update_supervisor_row(
                    result.call_id,
                    self._render_supervisor_row(
                        result.call_id, result.tool_name, arguments, result
                    ),
                )
            elif self._app.has_command_tool_entry(
                result.call_id
            ) or _is_command_like_tool(result.tool_name, arguments):
                self._render_command_tool_complete(result, arguments)
            elif result.tool_name in _MUTATING_FILE_TOOLS:
                self._render_file_change_complete(result, arguments, preview)
            else:
                self._render_generic_complete(result, arguments)
            self._clear_tool_call_state(result.call_id)
            self._tool_started_at.pop(result.call_id, None)
            self.start_thinking()
            return

        if event.kind == AgentEventType.TOOL_DENIED:
            self.stop_tool_wait()
            self.end_assistant()
            reason = getattr(event.payload, "reason", str(event.payload))
            self._write_alert(
                "Tool denied",
                str(reason),
                title_style="bold red",
                body_style="red on #2a1717",
            )
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
                if (
                    _is_subagent_actor(actor)
                    and self._app.has_subagent_task(actor)
                    and _is_command_like_tool(req.tool_name, req.arguments)
                ):
                    self._app.record_subagent_command_tool_start(
                        actor,
                        req.call_id,
                        req.tool_name,
                        {str(key): value for key, value in req.arguments.items()},
                        approval_required=True,
                    )
                    return
                display_name = self._tool_display_name(req.tool_name, actor)
                self._write_confirmation_request(
                    req,
                    actor=actor,
                    display_name=display_name,
                    policy=str(req.payload.get("approval_policy", "on-request")),
                )
            else:
                if is_ask_user_confirmation(req):
                    self._write(
                        Group(
                            self._inline_header(
                                "? ",
                                "Nexus needs clarification",
                                f"#{req.call_id[:8] or 'pending'}",
                                style="info",
                            ),
                            Text("\n".join(ask_user_display_lines(req))),
                        )
                    )
                    return
                actor = str(req.payload.get("actor", "") or "").strip()
                display_name = self._tool_display_name(req.tool_name, actor)
                self._write(
                    Group(
                        self._inline_header(
                            "? ",
                            "Clarification needed",
                            f"{display_name}  #{req.call_id[:8] or 'pending'}",
                            style="info",
                        ),
                        self._render_tool_argument_summary(
                            req.tool_name, req.arguments
                        ),
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

        if event.kind == AgentEventType.TURN_COMPLETED:
            self.stop_thinking()
            self.stop_tool_wait()
            self._app.close_supervisor_group()
            self._app.mark_turn_completed()
            return

        if event.kind == AgentEventType.AGENT_STOP:
            self.stop_thinking()
            self.stop_tool_wait()
            self._app.close_supervisor_group()
            self._app.write_turn_footer_if_completed()
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


def _with_inline_toggle(renderable: RenderableType, toggle_id: str) -> RenderableType:
    if isinstance(renderable, _ResponsiveDiff):
        return replace(renderable, toggle_id=toggle_id)
    if isinstance(renderable, _FencedCodeBlock):
        return replace(renderable, toggle_id=toggle_id)
    if isinstance(renderable, Group):
        return Group(
            *(_with_inline_toggle(item, toggle_id) for item in renderable.renderables),
            fit=renderable.fit,
        )
    return renderable


def _assistant_header() -> Text:
    header = Text()
    header.append("Assistant", style="bold green")
    header.append(":", style="green")
    return header


def _is_subagent_tool(tool_name: str) -> bool:
    return tool_name == "delegate_task" or tool_name.startswith("subagent_")


def _is_subagent_actor(actor: str) -> bool:
    return bool(actor) and _is_subagent_tool(actor)


def _is_supervisor_group_tool(
    tool_name: str, args: dict[str, Any] | None = None
) -> bool:
    """Return True for tools that should be grouped in the supervisor collapsible block.

    Bash, file-mutating tools, and sub-agent dispatches are excluded because they
    need their own rich output or delegation UI.
    """
    return (
        not _is_subagent_tool(tool_name)
        and not _is_command_like_tool(tool_name, args or {})
        and tool_name not in _MUTATING_FILE_TOOLS
    )


def _subagent_title(args: dict[str, Any]) -> str:
    title = str(
        args.get("title") or args.get("task") or args.get("instructions") or ""
    ).strip()
    return _single_line(title, limit=120)


def _subagent_task_label(tool_name: str) -> str:
    role = tool_name
    if role.startswith("subagent_"):
        role = role[len("subagent_") :]
    elif role == "delegate_task":
        role = "delegate"
    labels = {
        "planning_analysis": "Planning Task",
        "execution": "Execution Task",
        "review": "Review Task",
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


def _subagent_completion_header(
    result: ToolResult, payload: dict[str, Any], *, elapsed: str = ""
) -> Text:
    del elapsed
    title = _single_line(
        str(payload.get("title") or result.metadata.get("title") or result.tool_name),
        limit=120,
    )
    header = Text(_AGENT_BULLET, style="bold magenta")
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
    status = str(
        payload.get("status")
        or result.metadata.get("status")
        or ("failed" if result.is_error else "completed")
    )
    parts = [status]
    if elapsed:
        parts.append(elapsed)
    parts.append(f"{count} tool{'s' if count != 1 else ''}")
    return " · ".join(parts)


def _subagent_result_preview(result: ToolResult, payload: dict[str, Any]) -> Text:
    status = str(
        payload.get("status")
        or result.metadata.get("status")
        or ("failed" if result.is_error else "completed")
    )
    summary = _single_line(
        str(payload.get("summary") or payload.get("raw_result") or result.output or ""),
        limit=160,
    )
    text = Text(f"{_TOOL_ROW_INDENT}Status ", style="dim")
    text.append(status, style=_subagent_status_style(status, is_error=result.is_error))
    if summary:
        text.append(f"\n{_TOOL_ROW_INDENT}Summary - ", style="dim")
        text.append(summary, style="white")
    text.append(
        f"\n{_TOOL_ROW_INDENT}Expand to view sub-agent tool calls and JSON.",
        style="dim",
    )
    return text


def _subagent_result_summary_block(payload: dict[str, Any], status: str) -> Text:
    summary = _single_line(
        str(payload.get("summary") or payload.get("raw_result") or ""), limit=180
    )
    text = Text(f"{_TOOL_ROW_INDENT}Status ", style="dim")
    rendered_status = status or str(payload.get("status") or "completed")
    text.append(rendered_status, style=_subagent_status_style(rendered_status))
    if summary:
        text.append(f"\n{_TOOL_ROW_INDENT}Summary - ", style="dim")
        text.append(summary, style="white")
    next_action = _single_line(
        str(payload.get("recommended_next_action") or ""), limit=100
    )
    if next_action:
        text.append(f"\n{_TOOL_ROW_INDENT}Next - ", style="dim")
        text.append(next_action, style="white")
    return text


def _subagent_result_body(output: str, payload: dict[str, Any]) -> RenderableType:
    if payload:
        return Syntax(output, "json", theme="monokai", word_wrap=True)
    return _preview_text_block(output)


def _subagent_result_json_row(entry: dict[str, Any]) -> Text:
    call_id = str(entry.get("subagent_call_id", ""))
    expanded = bool(entry.get("subagent_result_json_expanded"))
    is_error = bool(entry.get("subagent_result_is_error"))
    status = "failed" if is_error else "done"
    marker = "[-]" if expanded else "[+]"
    row = Text(_TOOL_ROW_INDENT, style="dim")
    marker_start = len(row.plain)
    row.append(f"{marker} ", style="cyan")
    if call_id:
        row.stylize(
            Style(color="cyan", meta={"nexus_subagent_result_json_call_id": call_id}),
            marker_start,
            len(row.plain),
        )
    row.append(
        "✗ " if is_error else "✓ ", style="bold red" if is_error else "bold green"
    )
    label_start = len(row.plain)
    row.append("Result: Output JSON", style="bold cyan")
    if call_id:
        row.stylize(
            Style(
                color="bright_cyan",
                underline=True,
                meta={"nexus_subagent_result_json_call_id": call_id},
            ),
            label_start,
            len(row.plain),
        )
    row.append(" · ", style="dim")
    row.append(status, style="bold red" if is_error else "dim")
    elapsed = str(entry.get("subagent_result_elapsed") or "").strip()
    if elapsed:
        row.append(" · ", style="dim")
        row.append(elapsed, style="bold bright_cyan")
    return row


def _subagent_status_style(status: str, *, is_error: bool = False) -> str:
    normalized = status.strip().lower()
    if is_error or normalized in {
        "failed",
        "needs_approval",
        "needs_clarification",
        "blocked",
        "failed_verification",
    }:
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


def _bounded_command_preview(value: str) -> tuple[str, bool]:
    lines = str(value or "").splitlines()
    preview = "\n".join(lines[:_COLLAPSED_PREVIEW_LINES])
    truncated = len(lines) > _COLLAPSED_PREVIEW_LINES
    if len(preview) > _COMMAND_PREVIEW_CHARS:
        preview = preview[:_COMMAND_PREVIEW_CHARS].rstrip()
        truncated = True
    return preview, truncated


def _preview_text_block(value: str, *, style: str = "dim on #1f1f1f") -> Text:
    preview = _first_lines(value)
    if _line_count(value) > _COLLAPSED_PREVIEW_LINES:
        preview = f"{preview}\n... click [+] to expand"
    return Text(preview, style=style)


def _line_count(value: str) -> int:
    return len(str(value or "").splitlines()) or 1


def _should_collapse_text(value: str) -> bool:
    text = str(value or "")
    return len(text) > _COLLAPSE_CHAR_LIMIT or _line_count(text) > min(
        _COLLAPSE_LINE_LIMIT, _COLLAPSED_PREVIEW_LINES
    )


def _should_collapse_alert(value: str) -> bool:
    text = str(value or "")
    return len(text) > _ALERT_PREVIEW_CHARS or _line_count(text) > 3


def _alert_preview(value: str) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= _ALERT_PREVIEW_CHARS:
        return compact
    return compact[: max(0, _ALERT_PREVIEW_CHARS - 3)].rstrip() + "..."


def _diff_data_from_preview(preview: dict[str, Any]) -> dict[str, Any]:
    diff = preview.get("diff") if isinstance(preview.get("diff"), dict) else {}
    return {str(key): value for key, value in diff.items()}


def _diff_text_from_data(diff: dict[str, Any]) -> str:
    return str(diff.get("unified_diff", "") or "")


def _diff_data_from_arguments(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "write_file":
        return {
            "old_content": "",
            "new_content": str(args.get("content", "") or ""),
            "is_new_file": True,
        }
    if tool_name == "edit":
        return {
            "old_content": str(args.get("old_string", "") or ""),
            "new_content": str(args.get("new_string", "") or ""),
        }
    if tool_name == "insert_edit_into_file":
        return {
            "old_content": "",
            "new_content": str(args.get("code", "") or ""),
            "is_new_file": True,
        }
    return {}


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


def _diff_rows_from_contents(
    old_content: str, new_content: str, *, context: int = 3
) -> list[_DiffRow]:
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    matcher = SequenceMatcher(None, old_lines, new_lines)
    groups = list(matcher.get_grouped_opcodes(context))
    rows: list[_DiffRow] = []
    for group_index, group in enumerate(groups):
        if group_index:
            rows.append(_DiffRow(None, None, "", "", "ellipsis"))
        for tag, old_start, old_end, new_start, new_end in group:
            if tag == "equal":
                for offset, value in enumerate(old_lines[old_start:old_end]):
                    line_number = old_start + offset + 1
                    rows.append(
                        _DiffRow(
                            line_number, new_start + offset + 1, value, value, "context"
                        )
                    )
                continue
            if tag == "delete":
                for offset, value in enumerate(old_lines[old_start:old_end]):
                    rows.append(
                        _DiffRow(old_start + offset + 1, None, value, "", "delete")
                    )
                continue
            if tag == "insert":
                for offset, value in enumerate(new_lines[new_start:new_end]):
                    rows.append(
                        _DiffRow(None, new_start + offset + 1, "", value, "add")
                    )
                continue
            replaced_old = old_lines[old_start:old_end]
            replaced_new = new_lines[new_start:new_end]
            max_len = max(len(replaced_old), len(replaced_new))
            for offset in range(max_len):
                before = replaced_old[offset] if offset < len(replaced_old) else ""
                after = replaced_new[offset] if offset < len(replaced_new) else ""
                before_number = (
                    old_start + offset + 1 if offset < len(replaced_old) else None
                )
                after_number = (
                    new_start + offset + 1 if offset < len(replaced_new) else None
                )
                kind = "change" if before and after else "delete" if before else "add"
                rows.append(_DiffRow(before_number, after_number, before, after, kind))
    return rows


def _diff_rows_from_unified_diff(diff_text: str) -> list[_DiffRow]:
    rows: list[_DiffRow] = []
    pending_removed: list[tuple[int | None, str]] = []
    before_line: int | None = None
    after_line: int | None = None

    def flush_removed() -> None:
        nonlocal pending_removed
        for line_number, value in pending_removed:
            rows.append(_DiffRow(line_number, None, value, "", "delete"))
        pending_removed = []

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("@@"):
            flush_removed()
            before_line, after_line = _parse_unified_hunk_start(raw_line)
            continue
        if raw_line.startswith(("+++", "---")) or raw_line.startswith("\\"):
            continue
        if raw_line == "":
            continue
        marker = raw_line[0]
        value = raw_line[1:]
        if marker == "-":
            pending_removed.append((before_line, value))
            if before_line is not None:
                before_line += 1
            continue
        if marker == "+":
            if pending_removed:
                removed_number, removed_value = pending_removed.pop(0)
                rows.append(
                    _DiffRow(removed_number, after_line, removed_value, value, "change")
                )
            else:
                rows.append(_DiffRow(None, after_line, "", value, "add"))
            if after_line is not None:
                after_line += 1
            continue
        flush_removed()
        if marker == " ":
            rows.append(_DiffRow(before_line, after_line, value, value, "context"))
            if before_line is not None:
                before_line += 1
            if after_line is not None:
                after_line += 1
            continue
        rows.append(_DiffRow(before_line, after_line, raw_line, raw_line, "context"))
        if before_line is not None:
            before_line += 1
        if after_line is not None:
            after_line += 1
    flush_removed()
    return rows


def _parse_unified_hunk_start(line: str) -> tuple[int | None, int | None]:
    match = re.search(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _collapsed_diff_rows(rows: list[_DiffRow], *, limit: int) -> list[_DiffRow]:
    if len(rows) <= limit:
        return rows
    if limit <= 0:
        return [_DiffRow(None, None, "", "click [+] to expand", "ellipsis")]
    return [
        *rows[: max(0, limit - 1)],
        _DiffRow(None, None, "", "click [+] to expand", "ellipsis"),
    ]


def _render_diff_layout(
    rows: list[_DiffRow],
    *,
    path: str,
    language: str,
    width: int,
    new_file: bool = False,
    toggle_id: str = "",
) -> Panel | Group | Table:
    if new_file:
        return _render_diff_side_panel(
            rows,
            side="after",
            title="New file",
            path=path,
            language=language,
            toggle_id=toggle_id,
        )
    if width < _SIDE_BY_SIDE_DIFF_WIDTH:
        return Group(
            _render_diff_side_panel(
                rows,
                side="before",
                title="Before",
                path=path,
                language=language,
                toggle_id=toggle_id,
            ),
            _render_diff_side_panel(
                rows,
                side="after",
                title="After",
                path=path,
                language=language,
                toggle_id=toggle_id,
            ),
        )
    return _render_side_by_side_diff_table(
        rows, path=path, language=language, toggle_id=toggle_id
    )


def _render_side_by_side_diff_table(
    rows: list[_DiffRow], *, path: str, language: str, toggle_id: str = ""
) -> Table:
    table = Table(
        box=box.SQUARE,
        border_style="grey35",
        collapse_padding=True,
        expand=True,
        padding=(0, 0),
        style=f"on {_DIFF_EDITOR_BACKGROUND}",
    )
    table.add_column(
        _diff_panel_title("Before", path=path, title_style="bold red"),
        ratio=1,
        min_width=1,
        overflow="fold",
    )
    table.add_column(
        _diff_panel_title("After", path=path, title_style="bold green"),
        ratio=1,
        min_width=1,
        overflow="fold",
    )
    if not rows:
        table.add_row(Text("(empty)", style="dim"), Text("(empty)", style="dim"))
        return table
    before_width = _line_number_width(rows, side="before")
    after_width = _line_number_width(rows, side="after")
    highlighter = Syntax("", language, theme="monokai")
    for row in rows:
        table.add_row(
            _diff_side_line(
                row,
                side="before",
                width=before_width,
                highlighter=highlighter,
                toggle_id=toggle_id,
            ),
            _diff_side_line(
                row,
                side="after",
                width=after_width,
                highlighter=highlighter,
                toggle_id=toggle_id,
            ),
        )
    return table


def _render_diff_side_panel(
    rows: list[_DiffRow],
    *,
    side: str,
    title: str,
    path: str,
    language: str,
    toggle_id: str = "",
) -> Panel:
    table = Table.grid(expand=True, padding=0)
    table.add_column(ratio=1, min_width=1, overflow="fold")
    if rows:
        line_number_width = _line_number_width(rows, side=side)
        highlighter = Syntax("", language, theme="monokai")
        for row in rows:
            table.add_row(
                _diff_side_line(
                    row,
                    side=side,
                    width=line_number_width,
                    highlighter=highlighter,
                    toggle_id=toggle_id,
                )
            )
    else:
        table.add_row(Text("(empty)", style=f"dim on {_DIFF_EDITOR_BACKGROUND}"))
    return Panel(
        table,
        title=_diff_panel_title(title, path=path),
        title_align="left",
        border_style="red" if side == "before" else "green",
        box=box.SQUARE,
        padding=(0, 0),
        expand=True,
        style=f"on {_DIFF_EDITOR_BACKGROUND}",
    )


def _diff_panel_title(title: str, *, path: str, title_style: str = "bold") -> Text:
    text = Text(title, style=title_style)
    clean_path = path.replace("\n", " ").strip()
    if clean_path:
        text.append(" | ", style="dim")
        text.append(clean_path, style="dim")
    return text


def _diff_side_line(
    row: _DiffRow, *, side: str, width: int, highlighter: Syntax, toggle_id: str = ""
) -> Text:
    if row.kind == "ellipsis":
        text = Text(
            _diff_ellipsis_line(width), style=f"dim on {_DIFF_EDITOR_BACKGROUND}"
        )
        if toggle_id:
            text.stylize(Style(meta={"nexus_toggle": toggle_id}))
        return text

    line_number = row.before_number if side == "before" else row.after_number
    value = row.before if side == "before" else row.after
    marker = _diff_marker(row, side=side)
    text = Text(overflow="fold")
    text.append(f"{_format_line_number(line_number, width)} | ", style="grey62")
    text.append(marker, style=_diff_marker_style(marker))
    if value:
        text.append_text(_syntax_highlight_line(value, highlighter=highlighter))
    background = _diff_row_background(row, side=side)
    text.stylize(Style(bgcolor=background))
    return text


def _syntax_highlight_line(value: str, *, highlighter: Syntax) -> Text:
    highlighted = highlighter.highlight(value)
    highlighted.rstrip()
    return highlighted


def _diff_marker_style(marker: str) -> str:
    if marker == "-":
        return "bold bright_red"
    if marker == "+":
        return "bold bright_green"
    return "default"


def _should_render_as_new_file(tool_name: str, diff_data: dict[str, Any]) -> bool:
    if tool_name != "write_file":
        return False
    if "old_content" not in diff_data and not diff_data.get("is_new_file"):
        return False
    old_content = str(diff_data.get("old_content", "") or "")
    return bool(diff_data.get("is_new_file")) or old_content == ""


def _render_new_file_preview(
    content: str,
    *,
    path: str = "",
    language: str = "text",
    collapsed: bool = False,
) -> _ResponsiveDiff:
    lines = content.splitlines()
    visible_lines = lines
    if collapsed and len(lines) > _COLLAPSED_PREVIEW_LINES:
        visible_lines = lines[: max(0, _COLLAPSED_PREVIEW_LINES - 1)]
    rows: list[_DiffRow] = []
    for index, line in enumerate(visible_lines, start=1):
        rows.append(_DiffRow(None, index, "", line, "add"))
    if len(visible_lines) < len(lines):
        rows.append(_DiffRow(None, None, "", "click [+] to expand", "ellipsis"))
    return _ResponsiveDiff(
        rows=tuple(rows), path=path, language=language, new_file=True
    )


def _command_from_arguments(args: dict[str, Any]) -> str:
    command = args.get("command")
    return str(command or "").strip() if isinstance(command, str) else ""


def _is_command_like_tool(tool_name: str, args: dict[str, Any] | None = None) -> bool:
    if _command_from_arguments(args or {}):
        return True
    return tool_name in {
        "bash",
        "git_status",
        "git_diff",
        "run_tests",
        "run_python_check",
        "run_formatter",
    }


def _command_for_tool(
    tool_name: str, args: dict[str, Any], result: ToolResult | None = None
) -> str:
    command = _command_from_arguments(args)
    if command:
        return command
    metadata = (
        result.metadata
        if result is not None and isinstance(result.metadata, dict)
        else {}
    )
    raw_command = metadata.get("command")
    if isinstance(raw_command, list) and raw_command:
        return shlex.join(str(part) for part in raw_command)
    if isinstance(raw_command, str) and raw_command.strip():
        return raw_command.strip()
    extra_args = args.get("args")
    suffix = [str(arg) for arg in extra_args] if isinstance(extra_args, list) else []
    if tool_name == "run_tests":
        return shlex.join(["uv", "run", "pytest", *suffix])
    if tool_name == "run_python_check":
        return shlex.join(["python", "-m", "compileall", "-q", *suffix])
    if tool_name == "run_formatter":
        return shlex.join(["ruff", "format", ".", *suffix])
    if tool_name == "git_status":
        return "git status --porcelain=v1 -b"
    if tool_name == "git_diff":
        command_parts = ["git", "diff"]
        if args.get("stat"):
            command_parts.append("--stat")
        raw_ref = str(args.get("ref", "") or "").strip()
        target = str(args.get("target", "working") or "working").strip()
        if raw_ref:
            command_parts.append(raw_ref)
        elif target == "staged":
            command_parts.append("--staged")
        elif target == "head":
            command_parts.append("HEAD")
        path = str(args.get("path", "") or "").strip()
        if path:
            command_parts.extend(["--", path])
        return shlex.join(command_parts)
    return tool_name


def _command_from_preview_or_arguments(
    preview: dict[str, Any], args: dict[str, Any]
) -> str:
    command = preview.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip()
    return _command_from_arguments(args)


def _markdown_code_fence(value: str, *, language: str) -> str:
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", value)), default=0
    )
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}{language}\n{value}\n{fence}"


def _bash_command_block(command: str, *, collapsed: bool = False) -> _FencedCodeBlock:
    return _FencedCodeBlock(
        command,
        language="bash",
        label="Command",
        label_style="bold tool.shell",
        collapsed=collapsed,
    )


def _bash_output_block(output: str, *, collapsed: bool = False) -> _FencedCodeBlock:
    return _FencedCodeBlock(
        output,
        language="text",
        label="Console output",
        collapsed=collapsed,
    )


def _command_tool_body(
    command: str,
    output: str,
    *,
    running: bool = False,
    collapsed: bool = False,
) -> RenderableType:
    parts: list[RenderableType] = []
    if command:
        parts.append(_bash_command_block(command, collapsed=collapsed))
    if output:
        parts.append(_bash_output_block(output, collapsed=collapsed))
    elif running:
        parts.append(Text("Running...", style="dim"))
    elif not parts:
        parts.append(Text("(no tool output)", style="dim"))
    return Group(*parts)


def _command_tool_title(tool_name: str) -> str:
    labels = {
        "bash": "Bash Run",
        "run_tests": "Test Run",
        "run_python_check": "Python Check",
        "run_formatter": "Formatter Run",
        "git_status": "Git Status",
        "git_diff": "Git Diff",
    }
    return labels.get(tool_name, f"{tool_name} Run")


def _command_tool_summary(result: ToolResult, output: str) -> str:
    status = "failed" if result.is_error else "done"
    if not output:
        return status
    lines = _line_count(output)
    return f"{status} · {lines} line{'s' if lines != 1 else ''}"


def _line_number_width(rows: list[_DiffRow], *, side: str) -> int:
    numbers = [
        row.before_number if side == "before" else row.after_number
        for row in rows
        if (row.before_number if side == "before" else row.after_number) is not None
    ]
    if not numbers:
        return 1
    return max(1, len(str(max(numbers))))


def _format_line_number(line_number: int | None, width: int) -> str:
    if line_number is None:
        return " " * width
    return f"{line_number:>{width}}"


def _diff_marker(row: _DiffRow, *, side: str) -> str:
    if side == "before" and row.kind in {"change", "delete"}:
        return "-"
    if side == "after" and row.kind in {"change", "add"}:
        return "+"
    return " "


def _diff_row_background(row: _DiffRow, *, side: str) -> str:
    if row.kind == "change":
        return _DIFF_DELETE_BACKGROUND if side == "before" else _DIFF_ADD_BACKGROUND
    if row.kind == "delete":
        return _DIFF_DELETE_BACKGROUND if side == "before" else _DIFF_EDITOR_BACKGROUND
    if row.kind == "add":
        return _DIFF_ADD_BACKGROUND if side == "after" else _DIFF_EDITOR_BACKGROUND
    return _DIFF_EDITOR_BACKGROUND


def _diff_ellipsis_line(width: int) -> str:
    return f"{' ' * width} | ... click [+] to expand"
