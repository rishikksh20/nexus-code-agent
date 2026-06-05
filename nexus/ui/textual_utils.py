"""Utility helpers for the Textual Nexus UI."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.style import Style
from rich.table import Table
from rich.text import Text

from nexus.models import ConfirmationRequest, ConfirmationResponse
from nexus.ui.terminal import NEXUS_THEME

_MOUSE_ESCAPE_RE = re.compile(r"(?:)?\[(?:<\d{1,4};\d{1,5};\d{1,5}[mM]|M.{3})")


def _strip_mouse_escape_sequences(value: str) -> str:
    """Remove leaked terminal mouse reports from input text."""
    return _MOUSE_ESCAPE_RE.sub("", value)


def _is_explicit_denial_answer(answer: str) -> bool:
    normalized = " ".join(
        answer.strip()
        .lower()
        .replace("(", " ")
        .replace(")", " ")
        .replace("-", " ")
        .replace("_", " ")
        .split()
    )
    return normalized in {"n", "no"}


def _approval_resolution_summary(response: ConfirmationResponse) -> str:
    if not response.approved:
        return "rejected"
    scope = str(response.scope or "").strip().lower()
    if scope == "turn":
        return "approved for turn"
    if scope == "session":
        return "approved for session"
    return "approved once"


def _approval_request_key(request: ConfirmationRequest) -> str:
    if request.call_id:
        return request.call_id
    try:
        arguments = json.dumps(request.arguments, sort_keys=True, default=str)
    except TypeError:
        arguments = str(request.arguments)
    return f"{request.tool_name}:{arguments}"


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


def _copy_to_system_clipboard(
    text: str,
    *,
    commands: list[list[str]] | None = None,
    run: Callable[..., object] | None = None,
) -> bool:
    runner = run or subprocess.run
    for command in _clipboard_commands() if commands is None else commands:
        try:
            runner(
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


def _renderable_plain_text(renderable: Any, *, width: int = 120) -> str:
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


def _slash_command_suggestion_options(commands: tuple[Any, ...]) -> list[Text]:
    options: list[Text] = []
    for command in commands:
        row = Text()
        row.append(f"/{command.name}", style="bold bright_magenta")
        row.append("  ")
        row.append(str(command.description), style="dim")
        options.append(row)
    return options


def _user_prompt_block(raw_input: str) -> Table:
    text = Text()
    text.append("You", style="bold green")
    text.append(": ", style="green")
    lines = str(raw_input or "").splitlines() or [""]
    for index, line in enumerate(lines):
        if index:
            text.append("\n  ", style="dim")
        text.append(line, style="white")
    grid = Table.grid(expand=True, padding=(1, 1))
    grid.add_column()
    grid.add_row(text)
    grid.style = Style(bgcolor="#1d2b3e")
    return grid


def _input_response_block(prompt: str, raw_input: str) -> Text:
    is_approval = prompt.strip().lower().startswith("allow?")
    title = "Approval response" if is_approval else "Input response"
    text = Text()
    text.append(title, style="bold dark_green")
    text.append(": ", style="bold dark_green")
    text.append(str(raw_input or ""), style="white on #252525")
    return text


def _context_style(percent: float) -> str:
    if percent >= 85:
        return "red"
    if percent >= 65:
        return "yellow"
    return "green"


def _context_pie_icon(percent: float) -> str:
    clamped = max(0.0, min(100.0, float(percent)))
    if clamped < 12.5:
        return "○"
    if clamped < 37.5:
        return "◔"
    if clamped < 62.5:
        return "◑"
    if clamped < 87.5:
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
