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
    assert "params:" in output
    assert "path=calculator.py" in output
    assert "diff" not in output


def test_terminal_ui_parameter_summary_is_limited_to_100_characters():
    ui = _build_ui()
    summary = ui._render_tool_argument_summary(  # type: ignore[attr-defined]
        "bash",
        {"command": "echo " + ("abcdefghijklmnopqrstuvwxyz" * 8)},
    )
    text = str(summary)

    assert len(text) <= 108
    assert text.endswith("…")


def test_terminal_ui_treats_edit_as_write_tool():
    ui = _build_ui()

    summary = ui._render_tool_argument_summary(  # type: ignore[attr-defined]
        "edit",
        {
            "new_string": "print('new')\n",
            "path": "calculator.py",
            "old_string": "print('old')\n",
            "replace_all": False,
        },
    )

    assert str(summary).startswith("params: path=calculator.py")
    assert ui._tool_border_style("edit") == "tool.write"  # type: ignore[attr-defined]


def test_terminal_ui_tool_result_shows_compact_args_without_preview_diff():
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

    assert "params:" in output
    assert "path=calculator.py" in output
    assert "diff" not in output
    assert "done" in output


def test_terminal_ui_prefixes_nested_subagent_tool_calls():
    ui = _build_ui()
    start_event = AgentEvent.tool_call_start(
        "call-1",
        "read_file",
        {"path": "calculator/main.py"},
        actor="subagent_planning_analysis",
    )
    complete_event = AgentEvent.tool_call_complete(
        ToolResult(
            call_id="call-1",
            tool_name="read_file",
            output="def add(a, b): return a + b",
            metadata={"actor": "subagent_planning_analysis"},
        )
    )

    ui.render_event(start_event, stream_output=False, show_tool_calls=True)
    ui.render_event(complete_event, stream_output=False, show_tool_calls=True)
    output = ui.console.export_text()

    assert "|-> read_file path=calculator/main.py  #call-1" in output
    assert "subagent_planning_analysis - read_file" not in output


def test_terminal_ui_indents_parallel_tool_calls_inline():
    ui = _build_ui()

    ui.render_event(
        AgentEvent.tool_call_start(
            "call-1",
            "read_file",
            {"path": "calculator/main.py"},
            display={"parallel_group_size": 2, "parallel_index": 0},
        ),
        stream_output=False,
        show_tool_calls=True,
    )
    ui.render_event(
        AgentEvent.tool_call_start(
            "call-2",
            "grep",
            {"pattern": "tool", "path": "nexus/ui"},
            display={"parallel_group_size": 2, "parallel_index": 1},
        ),
        stream_output=False,
        show_tool_calls=True,
    )

    output = ui.console.export_text()

    assert "> -|-> read_file path=calculator/main.py  #call-1" in output
    assert "> -|-> grep pattern=tool, path=nexus/ui  #call-2" in output


def test_terminal_ui_uses_subagent_name_for_thinking_status():
    ui = _build_ui()

    ui.render_event(
        AgentEvent.thinking_started(actor="subagent_execution"),
        stream_output=False,
        show_tool_calls=True,
        show_thinking_indicator=True,
    )

    assert ui._thinking_status is not None  # type: ignore[attr-defined]
    assert "subagent_execution - Thinking" in str(ui._thinking_status.status)  # type: ignore[attr-defined]
    ui.stop_thinking()


def test_terminal_ui_stops_tool_wait_on_completion():
    ui = _build_ui()

    ui.render_event(
        AgentEvent.tool_call_start("call-1", "subagent_execution", {"title": "Implement", "instructions": "Do it"}),
        stream_output=False,
        show_tool_calls=True,
    )
    assert ui._tool_status is not None  # type: ignore[attr-defined]

    ui.render_event(
        AgentEvent.tool_call_complete(ToolResult(call_id="call-1", tool_name="subagent_execution", output="done")),
        stream_output=False,
        show_tool_calls=True,
    )

    assert ui._tool_status is None  # type: ignore[attr-defined]
