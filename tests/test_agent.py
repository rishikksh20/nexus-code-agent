from __future__ import annotations

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import ConfirmationKind, Message, RuntimeResponse, ToolCall
from nexus.runtime.agent import Agent
from nexus.runtime.execution import ExecutionMode
from nexus.security import ApprovalManager, PermissionDecision
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import BashTool, GetTimeTool, WriteFileTool, WriteNoteTool


@pytest.mark.asyncio
async def test_agent_executes_read_only_tool(tool_context):
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Checking time."),
                tool_calls=(ToolCall(call_id="1", tool_name="get_time", arguments={}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(
                message=Message(role="assistant", content="All done."),
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event async for event in agent.run(
            [Message(role="user", content="what time is it?")],
            tool_context,
        )
    ]

    assert any(event.kind == "tool_result" for event in events)
    # AGENT_STOP is the final event; turn_completed precedes it
    assert events[-1].kind == "AGENT_STOP"
    assert any(event.kind == "turn_completed" for event in events)


@pytest.mark.asyncio
async def test_agent_requires_confirmation_for_mutating_tool(tool_context):
    initial_response = RuntimeResponse(
        message=Message(role="assistant", content="Writing note."),
        tool_calls=(
            ToolCall(
                call_id="2",
                tool_name="write_note",
                arguments={"path": "notes/out.txt", "content": "hello"},
            ),
        ),
        finish_reason="tool_calls",
    )
    model = FakeModelClient(
        scripted=[
            initial_response,
        ]
    )
    registry = ToolRegistry()
    registry.register(WriteNoteTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event async for event in agent.run(
            [Message(role="user", content="write a note")],
            tool_context,
            mode=ExecutionMode.DEFAULT,
        )
    ]

    confirmation = next(event for event in events if event.kind == "confirmation_requested")
    assert confirmation.payload.tool_name == "write_note"

    denied_model = FakeModelClient(scripted=[initial_response])
    denied_agent = Agent(model_client=denied_model, tool_registry=registry)
    denied_events = [
        event async for event in denied_agent.run(
            [Message(role="user", content="write a note")],
            tool_context,
            mode=ExecutionMode.PLAN,
        )
    ]
    denial = next(event for event in denied_events if event.kind == "tool_denied")
    assert denial.payload.decision is PermissionDecision.DENY


@pytest.mark.asyncio
async def test_agent_confirmation_request_includes_file_diff_preview(tool_context):
    initial_response = RuntimeResponse(
        message=Message(role="assistant", content="Writing file."),
        tool_calls=(
            ToolCall(
                call_id="wf-1",
                tool_name="write_file",
                arguments={"path": "calculator.py", "content": "print('calculator')\n"},
            ),
        ),
        finish_reason="tool_calls",
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    agent = Agent(
        model_client=FakeModelClient(scripted=[initial_response]),
        tool_registry=registry,
    )

    events = [
        event async for event in agent.run(
            [Message(role="user", content="create calculator")],
            tool_context,
            mode=ExecutionMode.DEFAULT,
        )
    ]

    confirmation = next(event for event in events if event.kind == "confirmation_requested")

    assert not any(event.kind == "TOOL_CALL_START" for event in events)
    assert confirmation.payload.call_id == "wf-1"
    assert confirmation.payload.preview["diff"]["path"].endswith("calculator.py")
    assert "+print('calculator')" in confirmation.payload.preview["diff"]["unified_diff"]


@pytest.mark.asyncio
async def test_agent_confirmation_request_includes_shell_command_preview(tool_context):
    initial_response = RuntimeResponse(
        message=Message(role="assistant", content="Running shell."),
        tool_calls=(
            ToolCall(
                call_id="sh-1",
                tool_name="bash",
                arguments={"command": "mkdir -p operations && touch operations/add.py"},
            ),
        ),
        finish_reason="tool_calls",
    )
    registry = ToolRegistry()
    registry.register(BashTool())
    agent = Agent(
        model_client=FakeModelClient(scripted=[initial_response]),
        tool_registry=registry,
    )

    events = [
        event async for event in agent.run(
            [Message(role="user", content="create ops")],
            tool_context,
            mode=ExecutionMode.DEFAULT,
        )
    ]

    confirmation = next(event for event in events if event.kind == "confirmation_requested")

    assert confirmation.payload.call_id == "sh-1"
    assert confirmation.payload.preview["command"] == "mkdir -p operations && touch operations/add.py"


@pytest.mark.asyncio
async def test_agent_turn_wide_approval_still_requires_confirmation_for_dangerous_bash(tool_context):
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Run dangerous command."),
                tool_calls=(
                    ToolCall(
                        call_id="sh-danger",
                        tool_name="bash",
                        arguments={"command": "rm -rf build"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(BashTool())
    approval_manager = ApprovalManager()
    approval_manager.record_turn_wide_mutating_approval()
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="clean the build output")],
            tool_context,
            mode=ExecutionMode.DEFAULT,
            approval_manager=approval_manager,
        )
    ]

    confirmation = next(event for event in events if event.kind == "confirmation_requested")
    assert confirmation.payload.tool_name == "bash"
    assert confirmation.payload.payload["risk_level"] == "dangerous"


def test_agent_plans_same_batch_preapproved_tool_calls(tool_context):
    registry = ToolRegistry()
    registry.register(WriteNoteTool())
    registry.register(BashTool())
    agent = Agent(model_client=FakeModelClient(), tool_registry=registry)
    approval_manager = ApprovalManager()
    approval_manager.record_turn_wide_mutating_approval()
    note_one = ToolCall(
        call_id="note-1",
        tool_name="write_note",
        arguments={"path": "notes/one.txt", "content": "one"},
    )
    note_two = ToolCall(
        call_id="note-2",
        tool_name="write_note",
        arguments={"path": "notes/two.txt", "content": "two"},
    )
    dangerous_shell = ToolCall(
        call_id="sh-danger",
        tool_name="bash",
        arguments={"command": "rm -rf build"},
    )

    planned = agent.preapproved_tool_calls_from_batch(
        (note_one, note_two, dangerous_shell),
        first_tool_call=note_one,
        mode=ExecutionMode.DEFAULT,
        context=tool_context,
        approval_manager=approval_manager,
        auto_confirm_read_only=True,
    )

    assert planned == (note_one, note_two)


@pytest.mark.asyncio
async def test_agent_stops_with_continue_message_when_tool_call_limit_is_reached(tool_context):
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Checking time twice."),
                tool_calls=(
                    ToolCall(call_id="time-1", tool_name="get_time", arguments={}),
                    ToolCall(call_id="time-2", tool_name="get_time", arguments={}),
                ),
                finish_reason="tool_calls",
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="check the time twice")],
            tool_context,
            max_tool_calls_per_turn=1,
        )
    ]

    text_complete = next(
        event
        for event in reversed(events)
        if event.kind == "TEXT_COMPLETE" and "continue" in str(event.payload).lower()
    )
    turn_completed = next(event for event in events if event.kind == "turn_completed")
    tool_results = [event for event in events if event.kind == "tool_result"]

    assert len(tool_results) == 1
    assert "Write `continue`" in text_complete.payload
    assert turn_completed.payload == "tool_call_limit"


@pytest.mark.asyncio
async def test_agent_requests_clarification_for_missing_required_tool_argument(tool_context):
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Need note details."),
                tool_calls=(
                    ToolCall(
                        call_id="3",
                        tool_name="write_note",
                        arguments={"content": "hello"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(WriteNoteTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event async for event in agent.run(
            [Message(role="user", content="write a note")],
            tool_context,
        )
    ]

    clarification = next(event for event in events if event.kind == "confirmation_requested")
    assert clarification.payload.kind is ConfirmationKind.CLARIFICATION
    assert clarification.payload.payload["field"] == "path"


@pytest.mark.asyncio
async def test_agent_hard_denies_write_note_outside_workspace_even_in_auto_mode(tool_context):
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Writing note outside the workspace."),
                tool_calls=(
                    ToolCall(
                        call_id="4",
                        tool_name="write_note",
                        arguments={"path": "../escape.txt", "content": "hello"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(WriteNoteTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="write outside the workspace")],
            tool_context,
            mode=ExecutionMode.AUTO,
        )
    ]

    denial = next(event for event in events if event.kind == "tool_denied")
    assert denial.payload.decision is PermissionDecision.DENY
    assert "outside the current workspace" in denial.payload.reason.lower()
    assert not (tool_context.working_directory.parent / "escape.txt").exists()


@pytest.mark.asyncio
async def test_agent_hard_denies_write_note_into_internal_nexus_state(tool_context):
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Updating Nexus state."),
                tool_calls=(
                    ToolCall(
                        call_id="5",
                        tool_name="write_note",
                        arguments={"path": ".nexus/config.toml", "content": "provider = \"fake\""},
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(WriteNoteTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="update the harness config")],
            tool_context,
            mode=ExecutionMode.AUTO,
        )
    ]

    denial = next(event for event in events if event.kind == "tool_denied")
    assert denial.payload.decision is PermissionDecision.DENY
    assert ".nexus" in denial.payload.reason
    assert not (tool_context.working_directory / ".nexus" / "config.toml").exists()


@pytest.mark.asyncio
async def test_agent_does_not_emit_empty_assistant_message(tool_context):
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content=""),
                finish_reason="stop",
            )
        ]
    )
    agent = Agent(model_client=model, tool_registry=ToolRegistry())

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="hello")],
            tool_context,
        )
    ]

    assert not any(event.kind == "model_response" for event in events)
    assert any(event.kind == "turn_completed" for event in events)
