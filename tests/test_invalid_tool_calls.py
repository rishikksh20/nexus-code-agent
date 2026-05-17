from __future__ import annotations

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import Message, RuntimeResponse, ToolCall
from nexus.runtime.agent import Agent
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import WriteFileTool


class RecordingModelClient(FakeModelClient):
    def __init__(self, scripted=None) -> None:
        super().__init__(scripted=scripted)
        self.requests = []

    async def chat_completion(self, request, *, stream: bool = True):
        self.requests.append(request)
        async for event in super().chat_completion(request, stream=stream):
            yield event


@pytest.mark.asyncio
async def test_agent_reports_unknown_tool_to_model_context_and_retries(tool_context):
    model = RecordingModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Calling a tool."),
                tool_calls=(ToolCall(call_id="bad-1", tool_name="write", arguments={}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content="Recovered.")),
        ]
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="write a file")],
            tool_context,
            max_turns=3,
        )
    ]

    tool_result = next(event.payload for event in events if event.kind == "tool_result")
    assert tool_result.is_error is True
    assert "Unknown tool name: write" in tool_result.output
    assert "write_file" in tool_result.output

    second_request_messages = model.requests[1].messages
    assert second_request_messages[-2].role == "assistant"
    assert second_request_messages[-2].tool_calls[0].call_id == "bad-1"
    assert second_request_messages[-1].role == "tool"
    assert second_request_messages[-1].tool_call_id == "bad-1"
    assert "Unknown tool name: write" in second_request_messages[-1].content


@pytest.mark.asyncio
async def test_agent_asks_model_to_repair_missing_content_instead_of_user(tool_context):
    model = RecordingModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Writing with the wrong key."),
                tool_calls=(
                    ToolCall(
                        call_id="bad-content",
                        tool_name="write_file",
                        arguments={"path": "hello.txt", "text": "Hello, World!"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(
                message=Message(role="assistant", content="Writing with the right key."),
                tool_calls=(
                    ToolCall(
                        call_id="fixed-content",
                        tool_name="write_file",
                        arguments={"path": "hello.txt", "content": "Hello, World!"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content="Done.")),
        ]
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="create hello.txt")],
            tool_context,
            auto_confirm=True,
            max_turns=4,
        )
    ]

    assert not any(event.kind == "confirmation_requested" for event in events)
    repair_result = next(
        event.payload
        for event in events
        if event.kind == "tool_result" and event.payload.call_id == "bad-content"
    )
    assert repair_result.is_error is True
    assert "Missing required argument(s) for tool 'write_file': 'content'" in repair_result.output
    assert "You supplied 'text'; use 'content'" in repair_result.output
    assert (tool_context.working_directory / "hello.txt").read_text(encoding="utf-8") == "Hello, World!"


@pytest.mark.asyncio
async def test_agent_stops_after_repeated_unknown_tool_repairs(tool_context):
    model = RecordingModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content=f"Bad call {index}."),
                tool_calls=(ToolCall(call_id=f"bad-{index}", tool_name="write", arguments={}),),
                finish_reason="tool_calls",
            )
            for index in range(3)
        ]
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool())
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="write a file")],
            tool_context,
            max_turns=5,
        )
    ]

    tool_results = [event.payload for event in events if event.kind == "tool_result"]
    assert len(tool_results) == 3
    assert tool_results[-1].metadata["retry_count"] == 3
    assert any(
        event.kind == "turn_completed" and event.payload == "invalid_tool_call"
        for event in events
    )
