"""nexus.ui.terminal — Terminal UI: the single Rich output layer for Nexus.

All console interactions — rendering agent events, printing banners, showing
tables, streaming markdown, and displaying approval prompts — go through this
class so that theming and markup rules live in exactly one place.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from nexus.runtime.clarifications import ask_user_display_lines, is_ask_user_confirmation

if TYPE_CHECKING:
    from nexus.models import AgentEvent, ConfirmationRequest, ToolResult


NEXUS_THEME = Theme(
    {
        "primary": "bold cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "info": "cyan",
        "muted": "grey62",
        "border": "grey35",
        "banner.title": "bold bright_white",
        "assistant.header": "bold bright_white",
        "tool": "bold bright_magenta",
        "tool.read": "cyan",
        "tool.write": "yellow",
        "tool.shell": "bright_magenta",
        "tool.network": "bright_blue",
        "tool.memory": "green",
        "tool.agent": "bright_cyan",
        "tool.result": "grey70",
        "tool.args": "grey70",
        "tool.denied": "bold red",
        "approval.header": "bold yellow",
        "clarification.header": "bold cyan",
        "version": "bold",
    }
)

_MAX_PREVIEW_CHARS = 150
_MAX_TOOL_PARAM_SUMMARY_CHARS = 100
_NEXUS_ASCII_BANNER = r"""
███    ██ ███████ ██   ██ ██   ██ ███████      █████  ██
████   ██ ██       ██ ██  ██   ██ ██          ██   ██ ██
██ ██  ██ █████     ███   ██   ██ ███████     ███████ ██
██  ██ ██ ██       ██ ██  ██   ██      ██     ██   ██ ██
██   ████ ███████ ██   ██  █████  ███████     ██   ██ ██
""".strip("\n")
_BANNER_PALETTE = (
    "yellow",
    "bright_green",
    "magenta",
    "bright_yellow",
    "green",
    "bright_magenta",
)


def _ascii_banner_text() -> str:
    return _NEXUS_ASCII_BANNER


def _solid_ascii_banner() -> Text:
    banner = Text(overflow="fold", no_wrap=False)
    color_index = 0
    for char in _ascii_banner_text():
        if char == "\n":
            banner.append(char)
            continue
        if char == " ":
            banner.append(char)
            continue
        banner.append(char, style=_BANNER_PALETTE[color_index % len(_BANNER_PALETTE)])
        color_index += 1
    return banner


class TerminalUI:
    """Centralised terminal output layer built on Rich."""

    def __init__(self, *, color: bool = True) -> None:
        self._console = Console(theme=NEXUS_THEME, no_color=not color, highlight=False)
        self._assistant_stream_open = False
        self._thinking_status = None
        self._tool_status = None
        self._tool_args_by_call_id: dict[str, dict[str, Any]] = {}
        self._tool_preview_by_call_id: dict[str, dict[str, Any]] = {}
        self._tool_actor_by_call_id: dict[str, str] = {}
        self._tool_display_by_call_id: dict[str, dict[str, Any]] = {}
        self._workspace_root: Path | None = None

    @property
    def console(self) -> Console:
        return self._console

    @property
    def file(self):
        return self._console.file

    def print(self, *args, **kwargs) -> None:
        self._console.print(*args, **kwargs)

    def input(self, prompt: str = "") -> str:
        return input(Text.from_markup(prompt).plain if prompt else "")

    def prompt_user(self) -> str:
        self.end_assistant()
        return input("> ")

    def print_version(self, version: str) -> None:
        self._console.print(version, style="version")

    def print_config_error(self, exc: Exception) -> None:
        self._console.print(f"[error]Configuration error:[/error] {exc}")

    def print_error(self, msg: str) -> None:
        self.end_assistant()
        self._console.print()
        self._console.print(
            Panel(
                Text(msg, style="error"),
                title=Text("Request failed", style="error"),
                title_align="left",
                border_style="error",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        self._console.print()

    def print_warning(self, msg: str) -> None:
        self._console.print(f"[warning]Warning:[/warning] {msg}")

    def print_success(self, msg: str) -> None:
        self._console.print(msg, style="success")

    def print_info(self, msg: str) -> None:
        self._console.print(msg, style="info")

    def print_muted(self, msg: str) -> None:
        self._console.print(msg, style="muted")

    def print_rule(self, title: str = "", *, style: str = "border") -> None:
        self._console.print(Rule(title, style=style))

    def print_markdown(self, content: str) -> None:
        self._console.print(Markdown(content))

    def stream_markdown(self, content: str) -> None:
        words = content.split()
        if not words:
            return
        accumulated = ""
        with Live(
            Markdown(""),
            console=self._console,
            refresh_per_second=20,
            vertical_overflow="visible",
        ) as live:
            for index, word in enumerate(words):
                accumulated += ("" if index == 0 else " ") + word
                live.update(Markdown(accumulated))
                time.sleep(0.012)

    def make_table(self, title: str = "", *columns: str) -> Table:
        table = Table(title=title) if title else Table()
        for col in columns:
            table.add_column(col)
        return table

    def begin_assistant(self) -> None:
        self.stop_thinking()
        self.stop_tool_wait()
        if self._assistant_stream_open:
            return
        self._console.print()
        self._console.print(Rule(Text("Assistant", style="assistant.header"), style="border"))
        self._assistant_stream_open = True

    def end_assistant(self) -> None:
        self.stop_thinking()
        if self._assistant_stream_open:
            self._console.print()
            self._console.print()
        self._assistant_stream_open = False

    def start_thinking(self, label: str = "Thinking") -> None:
        if self._thinking_status is not None:
            return
        self._thinking_status = self._console.status(f"[muted]{label}…[/muted]", spinner="dots")
        self._thinking_status.start()

    def stop_thinking(self) -> None:
        if self._thinking_status is None:
            return
        self._thinking_status.stop()
        self._thinking_status = None

    def start_tool_wait(self, label: str) -> None:
        self.stop_thinking()
        self.stop_tool_wait()
        self._tool_status = self._console.status(f"[muted]{label}…[/muted]", spinner="dots")
        self._tool_status.start()

    def stop_tool_wait(self) -> None:
        if self._tool_status is None:
            return
        self._tool_status.stop()
        self._tool_status = None

    def print_banner(
        self,
        provider: str,
        model: str,
        mode: str,
        *,
        workspace: str | Path | None = None,
    ) -> None:
        self._workspace_root = Path(workspace).resolve() if workspace is not None else None
        wordmark = _solid_ascii_banner()
        body = Table.grid(expand=True)
        body.add_column(style="muted", width=12)
        body.add_column(style="primary")
        body.add_row("Provider", provider)
        body.add_row("Model", model)
        body.add_row("Mode", mode)
        if workspace is not None:
            body.add_row("Workspace", str(Path(workspace).resolve()))
        body.add_row("Quick help", "/help  •  /skills  •  /session  •  /quit")
        self._console.print(
            Panel(
                Group(wordmark, "", body),
                title=Text("Nexus Coding Agent", style="banner.title"),
                title_align="left",
                border_style="border",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

    def print_session_resumed(self, session_id: str, msg_count: int) -> None:
        noun = "message" if msg_count == 1 else "messages"
        self._console.print(
            Panel(
                Text(
                    f"Resumed session {session_id} with {msg_count} {noun}. "
                    "Use /session new to start fresh or /session list to switch.",
                    style="muted",
                ),
                title=Text("Session", style="info"),
                title_align="left",
                border_style="border",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )

    def print_help_hint(self) -> None:
        self._console.print("[muted]Type /help for runtime commands, /skills for skill control, or /quit to exit.[/muted]")
        self._console.print()

    def print_fake_provider_notice(self) -> None:
        self._console.print(
            Panel(
                Text(
                    "Using the fake provider — responses are mocked. Set a real provider, API_KEY, and BASE_URL in .env for live coding-agent responses.",
                    style="warning",
                ),
                title=Text("Provider notice", style="warning"),
                title_align="left",
                border_style="warning",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )
        self._console.print()

    def print_no_api_key_warning(self, provider: str) -> None:
        self._console.print(
            Panel(
                Text(
                    f"No API key found for provider {provider}. Add API_KEY to .env (or a provider-specific key such as MISTRAL_API_KEY / COHERE_API_KEY / OPENAI_API_KEY) before starting a live session.",
                    style="warning",
                ),
                title=Text("Provider setup required", style="warning"),
                title_align="left",
                border_style="warning",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )
        self._console.print()

    def _relative_path(self, value: str) -> str:
        if self._workspace_root is None:
            return value
        try:
            return str(Path(value).resolve().relative_to(self._workspace_root))
        except Exception:  # noqa: BLE001
            return value

    def _guess_language(self, path: str | None) -> str:
        if not path:
            return "text"
        suffix = Path(path).suffix.lower()
        return {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "jsx",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".json": "json",
            ".toml": "toml",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".md": "markdown",
            ".sh": "bash",
            ".bash": "bash",
            ".zsh": "bash",
            ".diff": "diff",
        }.get(suffix, "text")

    def _ordered_args(self, tool_name: str, args: dict[str, Any]) -> list[tuple[str, Any]]:
        preferred = {
            "read_file": ["path", "offset", "limit"],
            "write_file": ["path", "content"],
            "edit": ["path", "old_string", "new_string", "replace_all"],
            "insert_edit_into_file": ["path", "code"],
            "apply_patch": ["patch", "strip"],
            "bash": ["command", "timeout", "cwd"],
            "glob": ["pattern"],
            "grep": ["pattern", "path"],
        }.get(tool_name, [])
        ordered: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for key in preferred:
            if key in args:
                ordered.append((key, args[key]))
                seen.add(key)
        ordered.extend((key, value) for key, value in args.items() if key not in seen)
        return ordered

    def _compact_value(self, key: str, value: Any) -> str:
        if isinstance(value, str):
            if key in {"content", "old_string", "new_string", "old_text", "new_text", "replacement", "new_content", "code", "input"}:
                line_count = len(value.splitlines()) or 0
                byte_count = len(value.encode("utf-8", errors="replace"))
                return f"<{line_count} lines • {byte_count} bytes>"
            if key in {"path", "cwd"}:
                return self._relative_path(value)
            return self._truncate_preview(value)
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list, bool, int, float)) else str(value)

    def _truncate_preview(self, text: str, *, limit: int = _MAX_PREVIEW_CHARS) -> str:
        single_line = text.replace("\n", " ⏎ ").strip()
        if len(single_line) <= limit:
            return single_line
        return single_line[: limit - 1] + "…"

    def _compact_diff_preview(self, preview: dict[str, Any]) -> str:
        diff = preview.get("diff") if isinstance(preview.get("diff"), dict) else {}
        unified_diff = str(diff.get("unified_diff", "") or "")
        changed_lines = [
            line.strip()
            for line in unified_diff.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        if not changed_lines:
            old_content = str(diff.get("old_content", "") or "")
            new_content = str(diff.get("new_content", "") or "")
            changed_lines = [f"- {old_content}", f"+ {new_content}"] if (old_content or new_content) else []
        return self._truncate_preview(" | ".join(changed_lines))

    def _render_diff_block(self, diff_text: str) -> Syntax:
        cleaned = diff_text.rstrip() or "(no diff preview)"
        if len(cleaned) > 4000:
            cleaned = cleaned[:4000] + "\n… [truncated]"
        return Syntax(
            cleaned,
            "diff",
            theme="monokai",
            word_wrap=True,
            line_numbers=False,
        )

    def _render_tool_preview(self, tool_name: str, args: dict[str, Any], preview: dict[str, Any]) -> Group | Table | None:
        if not preview:
            if tool_name == "apply_patch":
                patch_text = str(args.get("patch", "") or "").strip()
                if patch_text:
                    return Group(self._render_diff_block(patch_text))
            return None
        table = Table.grid(padding=(0, 1))
        table.add_column(style="muted", justify="right", no_wrap=True)
        table.add_column(style="tool.args", overflow="fold")
        blocks: list[Any] = []

        affected_paths = preview.get("affected_paths") if isinstance(preview.get("affected_paths"), list) else []
        if affected_paths:
            table.add_row("target", self._relative_path(str(affected_paths[0])))
        command = preview.get("command")
        if isinstance(command, str) and command.strip():
            table.add_row("command", self._truncate_preview(command))
        diff = preview.get("diff") if isinstance(preview.get("diff"), dict) else None
        if diff is not None:
            diff_path = diff.get("path")
            if isinstance(diff_path, str) and not affected_paths:
                table.add_row("target", self._relative_path(diff_path))
            compact_diff = self._compact_diff_preview(preview)
            if compact_diff:
                table.add_row("change", compact_diff)
        if tool_name == "apply_patch" and diff is None:
            patch_text = str(args.get("patch", "") or "").strip()
            if patch_text:
                blocks.append(Text("diff", style="muted"))
                blocks.append(self._render_diff_block(patch_text))
        if table.row_count:
            blocks.insert(0, table)
        if not blocks:
            return None
        if len(blocks) == 1:
            return blocks[0]
        return Group(*blocks)

    def _render_tool_panel_body(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        preview: dict[str, Any] | None = None,
        reason: str | None = None,
        approval_policy: str | None = None,
        clarification_prompt: str | None = None,
    ) -> Group:
        blocks: list[Any] = []
        blocks.append(self._render_tool_argument_summary(tool_name, args))
        preview_renderable = self._render_tool_preview(tool_name, args, preview or {})
        if preview_renderable is not None:
            blocks.append(preview_renderable)
        if clarification_prompt:
            blocks.append(Text(clarification_prompt, style="info"))
        if reason:
            blocks.append(Text(reason, style="warning"))
        if approval_policy is not None:
            blocks.append(Text(self._approval_choices(approval_policy), style="muted"))
        return Group(*blocks)

    def _render_tool_argument_summary(self, tool_name: str, args: dict[str, Any]) -> Text:
        if not args:
            return Text("params: (none)", style="muted")
        parts = [
            f"{key}={self._compact_value(key, value)}"
            for key, value in self._ordered_args(tool_name, args)
        ]
        summary = self._truncate_preview(
            ", ".join(parts),
            limit=_MAX_TOOL_PARAM_SUMMARY_CHARS,
        )
        return Text(f"params: {summary}", style="tool.args")

    def _render_args_table(self, tool_name: str, args: dict[str, Any]) -> Table:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="muted", justify="right", no_wrap=True)
        table.add_column(style="tool.args", overflow="fold")
        for key, value in self._ordered_args(tool_name, args):
            table.add_row(key, self._compact_value(key, value))
        return table

    def _tool_border_style(self, tool_name: str) -> str:
        if tool_name in {"read_file", "glob", "grep", "ls", "get_time"}:
            return "tool.read"
        if tool_name in {"write_file", "edit", "insert_edit_into_file", "apply_patch"}:
            return "tool.write"
        if tool_name == "bash":
            return "tool.shell"
        if tool_name.startswith("web_"):
            return "tool.network"
        if tool_name.startswith("memory") or tool_name.startswith("todo"):
            return "tool.memory"
        if tool_name.startswith("subagent") or tool_name.startswith("delegate"):
            return "tool.agent"
        return "tool"

    def _tool_display_name(self, tool_name: str, actor: str = "") -> str:
        actor = actor.strip()
        if not actor:
            return tool_name
        return f"{actor} - {tool_name}"

    def _store_tool_call_state(
        self,
        call_id: str,
        arguments: dict[str, Any],
        preview: dict[str, Any],
        actor: str,
        display: dict[str, Any],
    ) -> None:
        self._tool_args_by_call_id[call_id] = dict(arguments)
        self._tool_preview_by_call_id[call_id] = dict(preview)
        self._tool_display_by_call_id[call_id] = dict(display)
        if actor:
            self._tool_actor_by_call_id[call_id] = actor

    def _clear_tool_call_state(self, call_id: str) -> None:
        self._tool_args_by_call_id.pop(call_id, None)
        self._tool_preview_by_call_id.pop(call_id, None)
        self._tool_actor_by_call_id.pop(call_id, None)
        self._tool_display_by_call_id.pop(call_id, None)

    def _tool_is_mutating(self, tool_name: str, display: dict[str, Any] | None = None) -> bool:
        info = display or {}
        if "is_mutating" in info:
            return bool(info.get("is_mutating"))
        return tool_name in {
            "apply_patch",
            "create_directory",
            "create_file",
            "delete_file",
            "edit",
            "insert_edit_into_file",
            "modify_file",
            "move_file",
            "rename_file",
            "smart_edit",
            "write_file",
        }

    def _should_render_compact_tool(self, tool_name: str, display: dict[str, Any] | None = None) -> bool:
        return not self._tool_is_mutating(tool_name, display)

    def _pretty_subagent_name(self, name: str) -> str:
        value = name.strip()
        if value.startswith("subagent_"):
            value = value[len("subagent_") :]
        elif value.startswith("subagent-"):
            value = value[len("subagent-") :]
        return value.replace("_", " ").replace("-", " ").strip() or name

    def _compact_tool_label(self, tool_name: str) -> str:
        if tool_name.startswith("subagent_") or tool_name.startswith("subagent-"):
            return f"Subagent {self._pretty_subagent_name(tool_name)}"
        if tool_name.startswith("delegate"):
            return "Subagent"
        return tool_name

    def _compact_tool_detail(self, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name.startswith("subagent_") or tool_name.startswith("subagent-") or tool_name.startswith("delegate"):
            task = str(args.get("title") or args.get("task") or args.get("instructions") or "").strip()
            return f"task={self._truncate_preview(task, limit=90)}" if task else ""
        parts: list[str] = []
        for key, value in self._ordered_args(tool_name, args):
            if key in {"content", "code", "input", "new_content", "new_text", "old_text", "patch", "replacement"} and parts:
                continue
            parts.append(f"{key}={self._compact_value(key, value)}")
            joined = ", ".join(parts)
            if len(parts) >= 2 or len(joined) >= 90:
                return self._truncate_preview(joined, limit=100)
        return self._truncate_preview(", ".join(parts), limit=100)

    def _compact_tool_prefix(self, *, actor: str, display: dict[str, Any]) -> str:
        parallel = int(display.get("parallel_group_size", 0) or 0) > 1
        if actor:
            return f"    {'-|-> ' if parallel else '|-> '}"
        return f"> {'-|-> ' if parallel else ''}"

    def _render_compact_tool_start(
        self,
        call_id: str,
        tool_name: str,
        actor: str,
        args: dict[str, Any],
        display: dict[str, Any],
    ) -> Text:
        line = Text(self._compact_tool_prefix(actor=actor, display=display), style="muted")
        line.append(self._compact_tool_label(tool_name), style=self._tool_border_style(tool_name))
        detail = self._compact_tool_detail(tool_name, args)
        if detail:
            line.append(f" {detail}", style="tool.args")
        line.append(f"  #{call_id[:8] or 'pending'}", style="muted")
        return line

    def _render_compact_tool_error(
        self,
        result: ToolResult,
        *,
        actor: str,
        display: dict[str, Any],
    ) -> Text:
        del display
        prefix = "    x " if actor else "> x "
        line = Text(prefix, style="muted")
        line.append(self._compact_tool_label(result.tool_name), style="error")
        line.append(
            f" failed: {self._truncate_preview(result.output or 'Tool failed.', limit=120)}",
            style="error",
        )
        line.append(f"  #{result.call_id[:8]}", style="muted")
        return line

    def _render_tool_start_renderable(
        self,
        call_id: str,
        tool_name: str,
        actor: str,
        arguments: dict[str, Any],
        preview: dict[str, Any],
        display: dict[str, Any],
    ) -> Any:
        display_name = self._tool_display_name(tool_name, actor)
        if self._should_render_compact_tool(tool_name, display):
            return self._render_compact_tool_start(call_id, tool_name, actor, arguments, display)
        return Panel(
            self._render_tool_panel_body(tool_name, arguments, preview=preview),
            title=Text(f"{display_name}  #{call_id[:8] or 'pending'}", style="tool"),
            title_align="left",
            subtitle=Text("running", style="muted"),
            subtitle_align="right",
            border_style=self._tool_border_style(tool_name),
            box=box.ROUNDED,
            padding=(1, 2),
        )

    def _render_tool_completion_renderable(
        self,
        result: ToolResult,
        *,
        preview: dict[str, Any],
        actor: str,
        display: dict[str, Any],
    ) -> Any | None:
        display_name = self._tool_display_name(result.tool_name, actor)
        if self._should_render_compact_tool(result.tool_name, display):
            if result.is_error:
                return self._render_compact_tool_error(result, actor=actor, display=display)
            return None
        return Panel(
            self._render_tool_result_body(result, preview=preview),
            title=Text(f"{display_name}  #{result.call_id[:8]}", style="tool"),
            title_align="left",
            subtitle=Text("failed" if result.is_error else "done", style="error" if result.is_error else "success"),
            subtitle_align="right",
            border_style=self._tool_border_style(result.tool_name),
            box=box.ROUNDED,
            padding=(1, 2),
        )

    def _preview_block(self, text: str, *, path: str | None = None) -> Syntax | Text:
        cleaned = text.rstrip() or "(no tool output)"
        if len(cleaned) > 4000:
            cleaned = cleaned[:4000] + "\n… [truncated]"
        return Syntax(
            cleaned,
            self._guess_language(path),
            theme="monokai",
            word_wrap=True,
            line_numbers=False,
        )

    def _render_tool_result_body(self, result: ToolResult, *, preview: dict[str, Any] | None = None) -> Group:
        metadata = result.metadata or {}
        blocks: list[Any] = []
        path = metadata.get("path") if isinstance(metadata.get("path"), str) else None
        suppress_output_preview = result.tool_name in {"write_file", "edit", "insert_edit_into_file", "apply_patch"}
        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="muted", no_wrap=True)
        summary.add_column(style="tool.result")
        if path:
            summary.add_row("path", self._relative_path(str(path)))
        for key in ("lines", "bytes", "entries", "matches", "files_searched", "status_code", "timezone", "results", "count"):
            if key in metadata:
                summary.add_row(key, str(metadata[key]))
        if summary.row_count:
            blocks.append(summary)
        blocks.append(self._render_tool_argument_summary(result.tool_name, self._tool_args_by_call_id.get(result.call_id, {})))
        if result.is_error:
            blocks.append(Text(result.output or "Tool failed.", style="error"))
        elif suppress_output_preview:
            blocks.append(Text("Changes applied.", style="success"))
        else:
            blocks.append(self._preview_block(result.output, path=path))
        return Group(*blocks)

    def _approval_choices(self, approval_policy: str) -> str:
        if approval_policy == "approve-turn":
            return "Approval: [y]es for this turn  •  [n]o"
        if approval_policy == "approve-session":
            return "Approval: [y]es for this session  •  [n]o"
        return "Approval: [y]es once  •  yes [t]urn  •  [n]o"

    def print_clarification_request(self, req: ConfirmationRequest) -> None:
        if is_ask_user_confirmation(req):
            self._console.print(
                Panel(
                    Text("\n".join(ask_user_display_lines(req))),
                    title=Text("Nexus needs clarification", style="clarification.header"),
                    title_align="left",
                    border_style="info",
                    box=box.ROUNDED,
                    padding=(1, 2),
                )
            )
            return
        self._console.print(
            Panel(
                Group(
                    Text(req.prompt),
                    self._render_tool_argument_summary(req.tool_name, req.arguments),
                ),
                title=Text(f"Clarification needed — {req.tool_name}", style="clarification.header"),
                title_align="left",
                border_style="info",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )

    def render_event(
        self,
        event: AgentEvent,
        *,
        stream_output: bool,
        show_tool_calls: bool,
        show_thinking_indicator: bool = True,
    ) -> None:
        from nexus.models import AgentEventType, ConfirmationKind

        if event.kind == AgentEventType.AGENT_START:
            return

        if event.kind == AgentEventType.THINKING_STARTED and show_thinking_indicator:
            self.end_assistant()
            self.start_thinking(_thinking_label(event))
            return

        if event.kind == AgentEventType.TEXT_DELTA:
            self.stop_thinking()
            self.stop_tool_wait()
            if stream_output and event.payload:
                self.begin_assistant()
                self._console.print(event.payload, end="", markup=False, highlight=False)
            return

        if event.kind == AgentEventType.TEXT_COMPLETE:
            self.stop_thinking()
            self.stop_tool_wait()
            if stream_output:
                self.end_assistant()
            elif event.payload:
                self.begin_assistant()
                self._console.print(Markdown(str(event.payload)))
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
            self._console.print(
                self._render_tool_start_renderable(call_id, tool_name, actor, arguments, preview, display)
            )
            self.start_tool_wait(f"{self._tool_display_name(tool_name, actor)} running")
            return

        if event.kind == AgentEventType.TOOL_CALL_COMPLETE and show_tool_calls:
            self.stop_tool_wait()
            self.end_assistant()
            result = cast("ToolResult", event.payload)
            if result is None:
                return
            if isinstance(result.metadata, dict) and result.metadata.get("tool_unavailable"):
                self._clear_tool_call_state(result.call_id)
                return
            preview = self._tool_preview_by_call_id.get(result.call_id, {})
            actor = str(result.metadata.get("actor") or self._tool_actor_by_call_id.get(result.call_id, "")).strip()
            display = self._tool_display_by_call_id.get(result.call_id, {})
            renderable = self._render_tool_completion_renderable(
                result,
                preview=preview,
                actor=actor,
                display=display,
            )
            if renderable is not None:
                self._console.print(renderable)
            self._clear_tool_call_state(result.call_id)
            return

        if event.kind == AgentEventType.TOOL_DENIED:
            self.stop_tool_wait()
            self.end_assistant()
            self._console.print(
                Panel(
                    Text(event.payload.reason, style="tool.denied"),
                    title=Text("Tool denied", style="tool.denied"),
                    title_align="left",
                    border_style="tool.denied",
                    box=box.ROUNDED,
                    padding=(0, 2),
                )
            )
            return

        if event.kind == AgentEventType.CONFIRMATION_REQUESTED:
            self.stop_tool_wait()
            self.end_assistant()
            req = cast("ConfirmationRequest", event.payload)
            self._console.print()
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
                self._console.print(
                    Panel(
                        self._render_tool_panel_body(
                            req.tool_name,
                            req.arguments,
                            preview=req.preview,
                            reason=req.reason,
                            approval_policy=str(req.payload.get("approval_policy", "on-request")),
                        ),
                        title=Text(f"{display_name}  #{req.call_id[:8] or 'pending'}", style="tool"),
                        title_align="left",
                        subtitle=Text("approval required", style="warning"),
                        subtitle_align="right",
                        border_style=self._tool_border_style(req.tool_name),
                        box=box.ROUNDED,
                        padding=(1, 2),
                    )
                )
            else:
                if is_ask_user_confirmation(req):
                    self.print_clarification_request(req)
                    self._console.print()
                    return
                actor = str(req.payload.get("actor", "") or "").strip()
                display_name = self._tool_display_name(req.tool_name, actor)
                self._console.print(
                    Panel(
                        self._render_tool_panel_body(
                            req.tool_name,
                            req.arguments,
                            preview=req.preview,
                            clarification_prompt=req.prompt,
                            reason=req.reason,
                        ),
                        title=Text(f"{display_name}  #{req.call_id[:8] or 'pending'}", style="tool"),
                        title_align="left",
                        subtitle=Text("clarification needed", style="info"),
                        subtitle_align="right",
                        border_style=self._tool_border_style(req.tool_name),
                        box=box.ROUNDED,
                        padding=(1, 2),
                    )
                )
            self._console.print()
            return

        if event.kind == AgentEventType.AGENT_ERROR:
            self.stop_thinking()
            self.stop_tool_wait()
            payload = event.payload or {}
            error = payload.get("error") if isinstance(payload, dict) else str(payload)
            self.print_error(str(error or "Unknown provider error."))
            return

        # Legacy events are emitted alongside the new reference-style events.
        # Ignore their UI rendering here to avoid duplicate tool/status lines.
        if event.kind in {
            AgentEventType.MODEL_RESPONSE,
            AgentEventType.TURN_COMPLETED,
            AgentEventType.TOOL_CALL_REQUESTED,
            AgentEventType.TOOL_RESULT,
            AgentEventType.AGENT_STOP,
        }:
            return

    def render_events(
        self,
        events: list,
        *,
        stream_output: bool,
        show_tool_calls: bool,
        show_thinking_indicator: bool = True,
    ) -> None:
        for event in events:
            self.render_event(
                event,
                stream_output=stream_output,
                show_tool_calls=show_tool_calls,
                show_thinking_indicator=show_thinking_indicator,
            )

    def print_provider_setup_reminder(self, config) -> None:
        from nexus.integrations.registry import provider_has_api_key

        provider = config.provider
        if provider == "fake":
            return
        if provider_has_api_key(config):
            return

        self._console.print()
        self._console.print(
            Panel(
                Text(
                    "Create a .env file with API_KEY and BASE_URL, or set api_key in .nexus/config.toml before running live provider requests.",
                    style="warning",
                ),
                title=Text(f"No API key found for {provider}", style="warning"),
                title_align="left",
                border_style="warning",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        self._console.print()

    def print_doctor_report(self, report, *, output_format: str) -> None:
        if output_format == "json":
            self._console.file.write(json.dumps(report.to_dict(), indent=2) + "\n")
            return
        if output_format == "jsonl":
            for gate in report.gates:
                self._console.file.write(json.dumps(gate.to_dict()) + "\n")
            return

        self._console.print(f"Doctor status: {report.overall_status}")
        for gate in report.gates:
            table = Table(title=gate.name)
            table.add_column("Check")
            table.add_column("Status")
            table.add_column("Detail")
            for check in gate.checks:
                table.add_row(check.name, check.status, check.detail)
            self._console.print(table)
        if report.registered_tools:
            self._console.print("Registered tools: " + ", ".join(report.registered_tools))


def _thinking_label(event: Any) -> str:
    payload = getattr(event, "payload", None)
    actor = payload.get("actor") if isinstance(payload, dict) else ""
    actor = str(actor).strip() if actor else ""
    return f"{actor} - Thinking" if actor else "Thinking"
