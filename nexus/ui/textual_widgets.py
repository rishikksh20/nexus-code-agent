"""Textual widgets and screens for the Nexus TUI."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Button, Input, RichLog, Static

from nexus.models import ConfirmationRequest

if TYPE_CHECKING:
    from rich.console import RenderableType
    from nexus.ui.textual_app import NexusTextualApp

_RIGHT_MOUSE_BUTTON = 3


def _toggle_id_from_click(event: events.Click) -> str:
    style = getattr(event, "style", None)
    meta = getattr(style, "meta", None)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("nexus_toggle") or "")


def _file_preview_call_id_from_click(event: events.Click) -> str:
    style = getattr(event, "style", None)
    meta = getattr(style, "meta", None)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("nexus_file_preview_call_id") or "")


def _subagent_command_call_id_from_click(event: events.Click) -> str:
    style = getattr(event, "style", None)
    meta = getattr(style, "meta", None)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("nexus_subagent_command_call_id") or "")


def _subagent_result_json_call_id_from_click(event: events.Click) -> str:
    style = getattr(event, "style", None)
    meta = getattr(style, "meta", None)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("nexus_subagent_result_json_call_id") or "")


class PromptInput(Input):
    """Input widget with terminal-style prompt history navigation."""

    BINDINGS = [
        ("up", "history_previous", "Previous prompt"),
        ("down", "history_next", "Next prompt"),
    ]

    def action_history_previous(self) -> None:
        app = cast("NexusTextualApp", self.app)
        if app.move_slash_command_selection(-1):
            return
        app.action_prompt_history_previous()

    def action_history_next(self) -> None:
        app = cast("NexusTextualApp", self.app)
        if app.move_slash_command_selection(1):
            return
        app.action_prompt_history_next()

    def key_enter(self, event: events.Key) -> None:
        if cast("NexusTextualApp", self.app).accept_slash_command_selection():
            event.prevent_default()
            event.stop()

    def key_escape(self, event: events.Key) -> None:
        if cast("NexusTextualApp", self.app).hide_slash_command_suggestions():
            event.prevent_default()
            event.stop()

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
        preview_call_id = _file_preview_call_id_from_click(event)
        if preview_call_id:
            cast("NexusTextualApp", self.app).open_file_change_preview_for_call(
                preview_call_id
            )
            event.stop()
            return
        command_call_id = _subagent_command_call_id_from_click(event)
        if command_call_id:
            cast("NexusTextualApp", self.app).toggle_subagent_command_detail(
                command_call_id
            )
            event.stop()
            return
        result_json_call_id = _subagent_result_json_call_id_from_click(event)
        if result_json_call_id:
            cast("NexusTextualApp", self.app).toggle_subagent_result_json(
                result_json_call_id
            )
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

    def on_resize(self, event: events.Resize) -> None:
        del event
        app = cast("NexusTextualApp", self.app)
        if app._transcript is self:
            app.call_after_refresh(app._rerender_transcript)

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
            line = self.lines[y].crop_extend(
                scroll_x, scroll_x + width, self.rich_style
            )
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


class FileChangePreviewScreen(ModalScreen[None]):
    """Read-only approval preview for a pending file change."""

    CSS = """
    FileChangePreviewScreen {
        align: center middle;
    }

    #file-preview-shell {
        width: 92%;
        height: 90%;
        border: round $accent;
        background: #1f1f1f;
        padding: 1 2;
    }

    #file-preview-title {
        height: auto;
        margin-bottom: 1;
    }

    #file-preview-body-scroll {
        height: 1fr;
        border: round #3f3f3f;
        background: #272822;
        padding: 0 1;
    }

    #file-preview-body {
        width: 100%;
        height: auto;
    }

    #file-preview-actions {
        height: 3;
        margin-top: 1;
    }

    #file-preview-actions Button {
        margin-right: 1;
    }
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(
        self,
        request: ConfirmationRequest,
        *,
        title: Text,
        preview_renderable: RenderableType,
        on_accept: Callable[[], None],
        on_reject: Callable[[], None],
        on_close: Callable[[], None],
        actions_enabled: bool = True,
    ) -> None:
        super().__init__()
        self.request = request
        self.title_renderable = title
        self.preview_renderable = preview_renderable
        self._on_accept = on_accept
        self._on_reject = on_reject
        self._on_close = on_close
        self._resolved = not actions_enabled
        self._actions_enabled = actions_enabled

    def compose(self) -> ComposeResult:
        with Vertical(id="file-preview-shell"):
            yield Static(self.title_renderable, id="file-preview-title")
            with VerticalScroll(id="file-preview-body-scroll"):
                yield Static(self.preview_renderable, id="file-preview-body")
            with Horizontal(id="file-preview-actions"):
                accept = Button("Accept", id="file-preview-accept", variant="success")
                reject = Button("Reject", id="file-preview-reject", variant="error")
                accept.disabled = not self._actions_enabled
                reject.disabled = not self._actions_enabled
                yield accept
                yield reject
                yield Button("Close", id="file-preview-close")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "file-preview-accept":
            if not self._actions_enabled:
                return
            self._resolve(self._on_accept)
            self.dismiss()
            return
        if button_id == "file-preview-reject":
            if not self._actions_enabled:
                return
            self._resolve(self._on_reject)
            self.dismiss()
            return
        if button_id == "file-preview-close":
            self.dismiss()

    def action_close(self) -> None:
        self.dismiss()

    def on_unmount(self) -> None:
        self._on_close()

    def mark_resolved(self) -> None:
        self._actions_enabled = False
        self._resolved = True
        for selector in ("#file-preview-accept", "#file-preview-reject"):
            with suppress(Exception):
                self.query_one(selector, Button).disabled = True

    def _resolve(self, callback: Callable[[], None]) -> None:
        if self._resolved:
            return
        self.mark_resolved()
        callback()
