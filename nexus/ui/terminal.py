"""nexus.ui.terminal — Terminal UI: the single Rich output layer for Nexus.

All console interactions — rendering agent events, printing banners, showing
tables, streaming markdown, and displaying approval prompts — go through this
class so that theming and markup rules live in exactly one place.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.rule import Rule
from rich.table import Table
from rich.theme import Theme

if TYPE_CHECKING:
    from nexus.models import AgentEvent, ConfirmationRequest

# ---------------------------------------------------------------------------
# Theme — all semantic colour/style names used throughout the package
# ---------------------------------------------------------------------------

NEXUS_THEME = Theme(
    {
        # Generic severity levels
        "primary": "bold cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "info": "blue",
        "muted": "dim",
        # Agent event rendering
        "tool.name": "dim cyan",
        "tool.args": "dim",
        "tool.result": "dim",
        "tool.denied": "bold red",
        # Confirmation panels
        "approval.header": "bold yellow",
        "clarification.header": "bold cyan",
        # REPL startup
        "banner.title": "bold",
        # Version string
        "version": "bold",
    }
)


# ---------------------------------------------------------------------------
# TerminalUI
# ---------------------------------------------------------------------------


class TerminalUI:
    """Centralised terminal output layer built on Rich.

    All console interactions — rendering, prompting, and theming — go through
    this class so that styling rules and Rich markup live in exactly one place.

    The underlying ``Console`` is accessible via the :attr:`console` property
    for the handful of callers that pass it to Rich internals (e.g. ``Live``).
    The :attr:`file` property and the :meth:`print` / :meth:`input` pass-throughs
    ensure that existing code using ``console.print(...)`` or
    ``console.file.write(...)`` continues to work without change.
    """

    def __init__(self, *, color: bool = True) -> None:
        self._console = Console(theme=NEXUS_THEME, no_color=not color)

    # ------------------------------------------------------------------
    # Raw pass-throughs
    # ------------------------------------------------------------------

    @property
    def console(self) -> Console:
        """Expose the underlying Rich Console (e.g. for ``Live(console=…)``)."""
        return self._console

    @property
    def file(self):
        """Expose ``console.file`` for callers that write raw text (e.g. JSON)."""
        return self._console.file

    def print(self, *args, **kwargs) -> None:
        """Pass-through to ``Console.print``; all existing call-sites work unchanged."""
        self._console.print(*args, **kwargs)

    def input(self, prompt: str = "") -> str:
        """Pass-through to ``Console.input`` for interactive prompts."""
        return self._console.input(prompt)

    def prompt_user(self) -> str:
        """Render the standard interactive user prompt and return the input."""
        return self._console.input("[primary]>[/primary] ")

    # ------------------------------------------------------------------
    # Semantic output — generic severity levels
    # ------------------------------------------------------------------

    def print_version(self, version: str) -> None:
        self._console.print(version, style="version")

    def print_config_error(self, exc: Exception) -> None:
        self._console.print(f"[error]Configuration error:[/error] {exc}")

    def print_error(self, msg: str) -> None:
        """Red bold ✗ error block (e.g. provider request failure)."""
        self._console.print(f"\n[error]✗ Request failed.[/error] {msg}\n")

    def print_warning(self, msg: str) -> None:
        self._console.print(f"[warning]Warning:[/warning] {msg}")

    def print_success(self, msg: str) -> None:
        self._console.print(msg, style="success")

    def print_info(self, msg: str) -> None:
        self._console.print(msg, style="info")

    def print_muted(self, msg: str) -> None:
        self._console.print(msg, style="muted")

    # ------------------------------------------------------------------
    # Structural primitives
    # ------------------------------------------------------------------

    def print_rule(self, title: str = "", *, style: str = "rule.line") -> None:
        self._console.print(Rule(title, style=style))

    def print_markdown(self, content: str) -> None:
        self._console.print(Markdown(content))

    def stream_markdown(self, content: str) -> None:
        """Word-by-word typewriter effect with progressive Rich Markdown rendering."""
        words = content.split()
        if not words:
            return
        accumulated = ""
        update_every = 5
        with Live(
            Markdown(""),
            console=self._console,
            refresh_per_second=20,
            vertical_overflow="visible",
        ) as live:
            for i, word in enumerate(words):
                accumulated += ("" if i == 0 else " ") + word
                if i % update_every == update_every - 1 or i == len(words) - 1:
                    live.update(Markdown(accumulated))
                    time.sleep(0.012)

    def make_table(self, title: str = "", *columns: str) -> Table:
        """Create and return a Rich ``Table`` pre-loaded with *columns*."""
        table = Table(title=title) if title else Table()
        for col in columns:
            table.add_column(col)
        return table

    # ------------------------------------------------------------------
    # REPL banner & startup messages
    # ------------------------------------------------------------------

    def print_banner(self, provider: str, model: str, mode: str) -> None:
        self._console.print("Nexus Coding Agent", style="banner.title")
        self._console.print(f"Provider: {provider}  |  Model: {model}  |  Mode: {mode}")

    def print_session_resumed(self, session_id: str, msg_count: int) -> None:
        noun = "message" if msg_count == 1 else "messages"
        self._console.print(
            f"[muted]Resumed session [bold]{session_id}[/bold] "
            f"({msg_count} {noun}). "
            "Use [bold]/session new[/bold] to start fresh or "
            "[bold]/session list[/bold] to pick another.[/muted]"
        )

    def print_help_hint(self) -> None:
        self._console.print("Type /help for runtime commands, /skills for skill control, or /quit to exit.\n")

    def print_fake_provider_notice(self) -> None:
        self._console.print(
            "[warning]Note:[/warning] Using the [bold]fake[/bold] provider — "
            "responses are mocked. Set a real provider and API key in your "
            ".env or .nexus/config.toml for live coding-agent responses."
        )
        self._console.print()

    def print_no_api_key_warning(self, provider: str) -> None:
        self._console.print(
            f"[warning]Warning:[/warning] No API key found for provider "
            f"[bold]{provider}[/bold]. Requests will fail. Add your key to "
            "[bold].env[/bold] (e.g. MISTRAL_API_KEY=sk-...) or set the "
            "environment variable."
        )
        self._console.print()

    # ------------------------------------------------------------------
    # Agent event rendering
    # ------------------------------------------------------------------

    def render_event(
        self,
        event: AgentEvent,
        *,
        stream_output: bool,
        show_tool_calls: bool,
    ) -> None:
        """Render a single agent event to the terminal."""
        from nexus.models import AgentEventType, ConfirmationKind  # local import avoids circular

        # ------------------------------------------------------------------
        # Reference-style streaming events
        # ------------------------------------------------------------------

        if event.kind == AgentEventType.TEXT_DELTA:
            # Real-time token chunk: print inline without markup so that
            # _stream_turn_live produces a live typewriter effect.
            if stream_output and event.payload:
                self._console.print(event.payload, end="", markup=False, highlight=False)

        elif event.kind == AgentEventType.TEXT_COMPLETE:
            if stream_output:
                # Chunks were already streamed; just close the paragraph.
                self._console.print("\n")
            else:
                # Batch mode: render the full text as Markdown.
                if event.payload:
                    self._console.print()
                    self._console.print(Markdown(event.payload))
                    self._console.print()

        elif event.kind == AgentEventType.TOOL_CALL_START and show_tool_calls:
            payload = event.payload or {}
            name = payload.get("name", "")
            args = payload.get("arguments", {})
            args_str = str(args)
            args_preview = args_str[:150] + ("…" if len(args_str) > 150 else "")
            self._console.print(
                f"[tool.name]⚙ {name}[/tool.name] [tool.args]{args_preview}[/tool.args]"
            )

        elif event.kind == AgentEventType.TOOL_CALL_COMPLETE and show_tool_calls:
            result = event.payload
            if result is not None:
                output = str(result.output)
                preview = output[:150] + ("…" if len(output) > 150 else "")
                self._console.print(f"[tool.result]  ↳ {preview}[/tool.result]")

        # ------------------------------------------------------------------
        # Legacy Nexus events (backward-compatible)
        # ------------------------------------------------------------------

        elif event.kind == AgentEventType.THINKING_STARTED and show_tool_calls:
            self._console.print("[muted]⋯ thinking…[/muted]")

        elif event.kind == AgentEventType.MODEL_RESPONSE:
            # Content is now rendered via TEXT_DELTA / TEXT_COMPLETE.
            # MODEL_RESPONSE is kept for history management only; no output here.
            pass

        elif event.kind == AgentEventType.TOOL_CALL_REQUESTED and show_tool_calls:
            args_str = str(event.payload.arguments)
            args_preview = args_str[:150] + ("…" if len(args_str) > 150 else "")
            self._console.print(
                f"[tool.name]⚙ {event.payload.tool_name}[/tool.name] "
                f"[tool.args]{args_preview}[/tool.args]"
            )

        elif event.kind == AgentEventType.TOOL_RESULT and show_tool_calls:
            output = str(event.payload.output)
            preview = output[:150] + ("…" if len(output) > 150 else "")
            self._console.print(f"[tool.result]  ↳ {preview}[/tool.result]")

        elif event.kind == AgentEventType.TOOL_DENIED:
            self._console.print(
                f"[tool.denied]✗ denied:[/tool.denied] {event.payload.reason}"
            )

        elif event.kind == AgentEventType.CONFIRMATION_REQUESTED:
            req: ConfirmationRequest = event.payload
            self._console.print()
            if req.kind is ConfirmationKind.APPROVAL:
                self.print_approval_request(req)
            else:
                self.print_clarification_request(req)
            self._console.print()

    def render_events(
        self,
        events: list,
        *,
        stream_output: bool,
        show_tool_calls: bool,
    ) -> None:
        """Render a list of agent events in order."""
        for event in events:
            self.render_event(event, stream_output=stream_output, show_tool_calls=show_tool_calls)

    # ------------------------------------------------------------------
    # Confirmation / clarification panels
    # ------------------------------------------------------------------

    def print_approval_request(self, req: ConfirmationRequest) -> None:
        self._console.print(
            Rule(
                "[approval.header]Approval Required[/approval.header]",
                style="approval.header",
            )
        )
        self._console.print(f"  [bold]Tool:[/bold] [primary]{req.tool_name}[/primary]")
        for key, val in req.arguments.items():
            val_str = str(val)
            preview = val_str[:150] + ("…" if len(val_str) > 150 else "")
            self._console.print(f"  [muted]  {key}:[/muted] {preview}")
        if req.reason:
            # Highlight risk level inside the reason string for visibility.
            reason = req.reason
            for marker, style in (
                ("Dangerous", "bold red"),
                ("dangerous", "bold red"),
                ("High-risk", "bold red"),
                ("high-risk", "bold red"),
                ("Medium-risk", "bold yellow"),
                ("medium-risk", "bold yellow"),
            ):
                if marker in reason:
                    reason = reason.replace(marker, f"[{style}]{marker}[/{style}]")
                    break
            self._console.print(f"  [muted]Reason:[/muted] {reason}")
        self._console.print(
            "  [muted]Approve:[/muted] "
            "[bold]\\[y][/bold]es (once)  "
            "[bold]\\[t][/bold]urn  "
            "[bold]\\[s][/bold]ession  "
            "[bold]\\[N][/bold]o"
        )

    def print_clarification_request(self, req: ConfirmationRequest) -> None:
        self._console.print(
            Rule(
                f"[clarification.header]Clarification Needed — "
                f"{req.tool_name}[/clarification.header]",
                style="clarification.header",
            )
        )
        self._console.print(f"  {req.prompt}")

    # ------------------------------------------------------------------
    # Provider setup reminder (shown after nexus init or on startup)
    # ------------------------------------------------------------------

    def print_provider_setup_reminder(self, config) -> None:
        from os import environ

        provider = config.provider
        if provider == "fake":
            return
        has_key = bool(
            config.api_key
            or environ.get("MISTRAL_API_KEY")
            or environ.get("NEXUS_API_KEY")
            or environ.get("OPENAI_API_KEY")
        )
        if has_key:
            return

        self._console.print()
        self._console.print(
            f"[warning]⚠  No API key found for provider [bold]{provider}[/bold].[/warning]"
        )
        self._console.print("Set your key using one of the following methods:\n")
        if provider == "mistral":
            self._console.print("  1. Create a [bold].env[/bold] file in this directory:")
            self._console.print("       [success]MISTRAL_API_KEY=sk-...[/success]\n")
            self._console.print("  2. Export as an environment variable:")
            self._console.print("       [success]export MISTRAL_API_KEY=sk-...[/success]\n")
            self._console.print("  3. Set it in [bold].nexus/config.toml[/bold]:")
            self._console.print('       [success]api_key = "sk-..."[/success]\n')
            self._console.print(
                "  Get a key at "
                "[link=https://console.mistral.ai]https://console.mistral.ai[/link]"
            )
        else:
            self._console.print("  1. Create a [bold].env[/bold] file in this directory:")
            self._console.print("       [success]NEXUS_API_KEY=sk-...[/success]\n")
            self._console.print("  2. Export as an environment variable:")
            self._console.print("       [success]export NEXUS_API_KEY=sk-...[/success]\n")
            self._console.print("  3. Set it in [bold].nexus/config.toml[/bold]:")
            self._console.print('       [success]api_key = "sk-..."[/success]')
        self._console.print()

    # ------------------------------------------------------------------
    # Doctor report rendering (moved from cli/doctor.py)
    # ------------------------------------------------------------------

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
            self._console.print(
                "Registered tools: " + ", ".join(report.registered_tools)
            )
