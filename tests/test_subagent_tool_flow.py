from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import AgentEventType, Message, RuntimeResponse, ToolCall, ToolExecutionContext, ToolResult
from nexus.runtime.agent import Agent
from nexus.sandbox.agent_tool import SubAgentTool, SubagentDefinition
from nexus.tools.base import Tool, ToolKind, ToolRegistry
from nexus.tools.builtin import WriteFileTool


class _RecordingUI:
    def __init__(self) -> None:
        self.events: list[AgentEventType] = []
        self.thinking_labels: list[str] = []
        self.tool_start_seen = asyncio.Event()

    def render_event(self, event, **kwargs) -> None:
        del kwargs
        self.events.append(event.kind)
        if event.kind == AgentEventType.THINKING_STARTED:
            payload = event.payload if isinstance(event.payload, dict) else {}
            self.thinking_labels.append(str(payload.get("actor", "")))
        if event.kind == AgentEventType.TOOL_CALL_START:
            self.tool_start_seen.set()


class _StartAwareReadTool(Tool):
    name = "read_file"
    description = "Test read tool."
    kind = ToolKind.READ
    is_mutating = False
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, call_id, arguments, context):
        del arguments
        ui = context.metadata["ui"]
        assert ui.tool_start_seen.is_set()
        return ToolResult(call_id=call_id, tool_name=self.name, output="read ok")


class _SlowReadTool(_StartAwareReadTool):
    async def execute(self, call_id, arguments, context):
        del arguments, context
        await asyncio.sleep(1)
        return ToolResult(call_id=call_id, tool_name=self.name, output="too late")


@pytest.mark.asyncio
async def test_advanced_supervisor_cannot_execute_normal_write_tool_directly(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteFileTool(), source="core", origin="builtin")
    registry.register(
        SubAgentTool(
            SubagentDefinition(
                name="execution",
                description="Implement focused work.",
                goal_prompt="Use normal tools to implement the assigned change.",
                allowed_tools=["write_file"],
            ),
            base_tool_registry=registry,
        ),
        source="agent",
        origin="execution",
    )
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Writing directly."),
                tool_calls=(
                    ToolCall(
                        call_id="direct-write",
                        tool_name="write_file",
                        arguments={"path": "direct.txt", "content": "wrong"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content="I will delegate instead.")),
        ]
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={"supervisor_cognitive_tools_only": True},
    )
    agent = Agent(model_client=model, tool_registry=registry)

    events = [event async for event in agent.run([Message(role="user", content="write a file")], context, max_turns=2)]

    result = next(event.payload for event in events if event.kind == AgentEventType.TOOL_RESULT)
    assert result.is_error is True
    assert result.metadata["tool_unavailable"] is True
    assert "subagent_execution" in result.output
    assert not (tmp_path / "direct.txt").exists()


@pytest.mark.asyncio
async def test_execution_subagent_can_execute_allowed_write_tool(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteFileTool(), source="core", origin="builtin")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Writing through the execution sub-agent."),
                tool_calls=(
                    ToolCall(
                        call_id="sub-write",
                        tool_name="write_file",
                        arguments={"path": "subagent.txt", "content": "done"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content='{"status": "completed", "summary": "Wrote file."}')),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Implement focused work.",
            goal_prompt="Use normal tools to implement the assigned change.",
            allowed_tools=["write_file"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={"auto_confirm": True, "execution_mode": "auto"},
    )

    result = await tool.execute(
        "sub-call",
        {"title": "Write file", "instructions": "Create subagent.txt with the requested content."},
        context,
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["status"] == "completed"
    assert (tmp_path / "subagent.txt").read_text(encoding="utf-8") == "done"


@pytest.mark.asyncio
async def test_subagent_renders_inner_tool_start_before_read_executes(tmp_path):
    ui = _RecordingUI()
    registry = ToolRegistry()
    registry.register(_StartAwareReadTool(), source="core", origin="test")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Reading the file."),
                tool_calls=(ToolCall(call_id="call-read", tool_name="read_file", arguments={"path": "README.md"}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content='{"status": "completed"}')),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="planning_analysis",
            description="Plan.",
            goal_prompt="Read and plan.",
            allowed_tools=["read_file"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={"ui": ui, "show_tool_calls": True, "stream_output": False},
    )

    result = await tool.execute("sub-call", {"title": "Plan", "instructions": "Read first."}, context)

    assert result.is_error is False
    assert AgentEventType.TOOL_CALL_START in ui.events
    assert ui.events.index(AgentEventType.TOOL_CALL_START) < ui.events.index(AgentEventType.TOOL_CALL_COMPLETE)
    assert "subagent_planning_analysis" in ui.thinking_labels


@pytest.mark.asyncio
async def test_subagent_timeout_returns_structured_failure(tmp_path):
    registry = ToolRegistry()
    registry.register(_SlowReadTool(), source="core", origin="test")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Reading slowly."),
                tool_calls=(ToolCall(call_id="call-read", tool_name="read_file", arguments={"path": "README.md"}),),
                finish_reason="tool_calls",
            ),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="planning_analysis",
            description="Plan.",
            goal_prompt="Read and plan.",
            allowed_tools=["read_file"],
            timeout_seconds=0.01,
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)

    result = await tool.execute("sub-call", {"title": "Plan", "instructions": "Read first."}, context)

    payload = json.loads(result.output)
    assert result.is_error is True
    assert payload["status"] == "failed"
    assert "timed out" in payload["raw_result"]
