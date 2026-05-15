from __future__ import annotations

from rich.console import Console

from nexus.models import AgentEvent, AgentEventType, ConfirmationKind, ConfirmationRequest, ToolResult
from nexus.ui.terminal import NEXUS_THEME, TerminalUI


def _build_ui() -> TerminalUI:
    ui = TerminalUI(color=False)
    ui._console = Console(  # type: ignore[attr-defined]
        record=True,
        no_color=True,
        force_terminal=False,
        width=220,
        highlight=False,
        theme=NEXUS_THEME,
    )
    return ui


def test_terminal_ui_startup_banner_includes_ascii_wordmark():
    ui = _build_ui()

    ui.print_banner("fake", "demo-model", "default", workspace="/workspace")
    output = ui.console.export_text()

    assert "Nexus Coding Agent" in output
    assert "███    ██ ███████" in output
    assert "██   ████ ███████" in output
    assert "Provider" in output
    assert "demo-model" in output


def test_terminal_ui_renders_inline_approval_inside_tool_panel():
    ui = _build_ui()
    diff_preview = {
        "path": "/workspace/calculator.py",
        "unified_diff": "--- /workspace/calculator.py\n+++ /workspace/calculator.py\n@@ -1 +1 @@\n-print('old')\n+print('new')\n",
        "old_content": "print('old')\n",
        "new_content": "print('new')\n",
    }
    event = AgentEvent(
        kind=AgentEventType.CONFIRMATION_REQUESTED,
        payload=ConfirmationRequest(
            kind=ConfirmationKind.APPROVAL,
            tool_name="write_file",
            prompt="Allow tool 'write_file'?",
            reason="write_file replaces the entire file — confirmation required.",
            call_id="abc12345",
            payload={"approval_policy": "on-request"},
            arguments={"path": "calculator.py", "content": "print('new')\n"},
            preview={"diff": diff_preview, "affected_paths": ["/workspace/calculator.py"]},
        ),
    )

    ui.render_event(event, stream_output=False, show_tool_calls=True)
    output = ui.console.export_text()

    assert "write_file  #abc12345" in output
    assert "approval required" in output
    assert "Approval: [y]es once" in output
    assert "yes [t]urn" in output
    assert "Approval required — write_file" not in output
    assert "diff" in output
    assert "+print('new')" in output


def test_terminal_ui_diff_preview_is_limited_to_150_characters():
    ui = _build_ui()
    long_line = "+" + ("abcdefghijklmnopqrstuvwxyz" * 8)

    preview = ui._compact_diff_preview(  # type: ignore[attr-defined]
        {
            "diff": {
                "unified_diff": f"--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-{long_line}\n+{long_line}\n",
                "old_content": long_line,
                "new_content": long_line,
            }
        }
    )

    assert len(preview) <= 150
    assert preview.endswith("…")


def test_terminal_ui_tool_result_reuses_stored_preview_diff():
    ui = _build_ui()
    start_event = AgentEvent.tool_call_start(
        "call-1",
        "modify_file",
        {"path": "calculator.py", "start_line": 1, "end_line": 1, "new_content": "print('new')\n"},
        preview={
            "affected_paths": ["/workspace/calculator.py"],
            "diff": {
                "path": "/workspace/calculator.py",
                "unified_diff": "--- /workspace/calculator.py\n+++ /workspace/calculator.py\n@@ -1 +1 @@\n-print('old')\n+print('new')\n",
                "old_content": "print('old')\n",
                "new_content": "print('new')\n",
            },
        },
    )
    complete_event = AgentEvent.tool_call_complete(
        ToolResult(
            call_id="call-1",
            tool_name="modify_file",
            output="Replaced lines 1–1 in calculator.py",
            metadata={"path": "calculator.py"},
        )
    )

    ui.render_event(start_event, stream_output=False, show_tool_calls=True)
    ui.render_event(complete_event, stream_output=False, show_tool_calls=True)
    output = ui.console.export_text()

    assert output.count("diff") >= 2
    assert "+print('new')" in output
    assert "done" in output
