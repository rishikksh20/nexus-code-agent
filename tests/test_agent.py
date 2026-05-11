from __future__ import annotations

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import ConfirmationKind, Message, RuntimeResponse, ToolCall
from nexus.runtime.agent import Agent
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.permissions import PermissionDecision
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, WriteNoteTool


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
