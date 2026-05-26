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


class _RecordingModel(FakeModelClient):
    def __init__(self) -> None:
        super().__init__(
            scripted=[
                RuntimeResponse(message=Message(role="assistant", content="Done.")),
            ]
        )
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return await super().complete(request)


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


class _PlainReadTool(Tool):
    name = "read_file"
    description = "Plain test read tool."
    kind = ToolKind.READ
    is_mutating = False
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, call_id, arguments, context):
        del arguments, context
        return ToolResult(call_id=call_id, tool_name=self.name, output="read ok")


class _SlowReadTool(_StartAwareReadTool):
    async def execute(self, call_id, arguments, context):
        del arguments, context
        await asyncio.sleep(1)
        return ToolResult(call_id=call_id, tool_name=self.name, output="too late")


class _NamedDelayReadTool(Tool):
    kind = ToolKind.READ
    is_mutating = False
    input_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, name: str, timings: dict[str, float], *, delay: float = 0.05) -> None:
        self.name = name
        self.description = f"Delay read tool {name}."
        self._timings = timings
        self._delay = delay

    async def execute(self, call_id, arguments, context):
        del call_id, arguments, context
        self._timings[f"{self.name}_start"] = asyncio.get_running_loop().time()
        await asyncio.sleep(self._delay)
        self._timings[f"{self.name}_end"] = asyncio.get_running_loop().time()
        return ToolResult(call_id=self.name, tool_name=self.name, output=f"done:{self.name}")


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
async def test_supervisor_tool_schemas_prefer_subagents_when_direct_tools_are_available(tmp_path):
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
    model = _RecordingModel()
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={"supervisor_available_tools": ["write_file", "subagent_execution"]},
    )
    agent = Agent(model_client=model, tool_registry=registry)

    [event async for event in agent.run([Message(role="user", content="write a file")], context, max_turns=1)]

    assert model.requests
    schemas = list(model.requests[0].tool_schemas)
    assert schemas[0]["function"]["name"] == "subagent_execution"
    subagent_description = schemas[0]["function"]["description"]
    direct_description = next(
        schema["function"]["description"]
        for schema in schemas
        if schema["function"]["name"] == "write_file"
    )
    assert "Preferred for file edits" in subagent_description
    assert "Supervisor direct-use escape hatch" in direct_description


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
async def test_execution_subagent_runs_non_mutating_tools_in_parallel_when_enabled(tmp_path):
    timings: dict[str, float] = {}
    registry = ToolRegistry()
    registry.register(_NamedDelayReadTool("read_alpha", timings), source="core", origin="builtin")
    registry.register(_NamedDelayReadTool("read_beta", timings), source="core", origin="builtin")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Read both files."),
                tool_calls=(
                    ToolCall(call_id="alpha", tool_name="read_alpha", arguments={}),
                    ToolCall(call_id="beta", tool_name="read_beta", arguments={}),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content='{"status": "completed", "summary": "Read both files."}')),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Implement focused work.",
            goal_prompt="Use normal tools to implement the assigned change.",
            allowed_tools=["read_alpha", "read_beta"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(
            model_name="fake",
            temperature=0.0,
            max_output_tokens=4096,
            parallel_tools=True,
            parallel_tool_window=2,
        ),
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={"auto_confirm": True, "execution_mode": "auto"},
    )

    result = await tool.execute(
        "sub-call",
        {"title": "Read files", "instructions": "Read both files before summarizing."},
        context,
    )

    assert result.is_error is False
    assert abs(timings["read_alpha_start"] - timings["read_beta_start"]) < 0.03
    assert max(timings["read_alpha_end"], timings["read_beta_end"]) - min(timings["read_alpha_start"], timings["read_beta_start"]) < 0.11


@pytest.mark.asyncio
async def test_execution_subagent_uses_own_tools_not_supervisor_scope(tmp_path):
    registry = ToolRegistry()
    registry.register(_PlainReadTool(), source="core", origin="test")
    registry.register(WriteFileTool(), source="core", origin="builtin")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Writing through the execution sub-agent."),
                tool_calls=(
                    ToolCall(
                        call_id="sub-write",
                        tool_name="write_file",
                        arguments={"path": "subagent-owned.txt", "content": "owned"},
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
            allowed_tools=["read_file", "write_file"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={
            "auto_confirm": True,
            "execution_mode": "auto",
            "supervisor_available_tools": ["read_file", "subagent_execution"],
        },
    )

    result = await tool.execute(
        "sub-call",
        {"title": "Write file", "instructions": "Create subagent-owned.txt with the requested content."},
        context,
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["context"]["allowed_tools"] == ["read_file", "write_file"]
    assert (tmp_path / "subagent-owned.txt").read_text(encoding="utf-8") == "owned"


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
async def test_subagent_allowed_tool_can_execute_even_when_not_in_base_allowlist(tmp_path):
    registry = ToolRegistry()
    registry.register(_PlainReadTool(), source="core", origin="test")
    config = SimpleNamespace(
        model_name="fake",
        temperature=0.0,
        max_output_tokens=4096,
        subagent_profiles=[
            {
                "name": "execution",
                "allowed_tools": ["read_file"],
            }
        ],
    )
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Reading."),
                tool_calls=(ToolCall(call_id="sub-read", tool_name="read_file", arguments={"path": "README.md"}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content='{"status": "completed", "summary": "Read."}')),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Execute focused work.",
            goal_prompt="Use scoped tools.",
            allowed_tools=["get_time"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=config,
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)

    result = await tool.execute("sub-call", {"title": "Read", "instructions": "Read README."}, context)

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["status"] == "completed"
    assert payload["context"]["allowed_tools"] == ["read_file"]


@pytest.mark.asyncio
async def test_subagent_disallowed_tool_and_legacy_call_time_tools_do_not_grant_tool(tmp_path):
    registry = ToolRegistry()
    registry.register(_PlainReadTool(), source="core", origin="test")
    config = SimpleNamespace(
        model_name="fake",
        temperature=0.0,
        max_output_tokens=4096,
        subagent_profiles=[
            {
                "name": "execution",
                "allowed_tools": [],
            }
        ],
    )
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Reading."),
                tool_calls=(ToolCall(call_id="sub-read", tool_name="read_file", arguments={"path": "README.md"}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content='{"status": "failed", "summary": "No tool."}')),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Execute focused work.",
            goal_prompt="Use scoped tools.",
            allowed_tools=["get_time"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=config,
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)

    result = await tool.execute(
        "sub-call",
        {"title": "Read", "instructions": "Read README.", "allowed_tools": ["read_file"]},
        context,
    )

    payload = json.loads(result.output)
    assert result.is_error is True
    assert payload["context"]["allowed_tools"] == []
    assert "Unknown tool name: read_file" in payload["raw_result"]


def test_subagent_schema_does_not_allow_supervisor_tool_overrides():
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Execute focused work.",
            goal_prompt="Use configured tools.",
            allowed_tools=["write_file"],
        )
    )

    assert "allowed_tools" not in tool.input_schema["properties"]
    assert tool.input_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_subagent_cannot_call_another_subagent_even_with_all_tools(tmp_path):
    registry = ToolRegistry()
    registry.register(_PlainReadTool(), source="core", origin="test")
    registry.register(
        SubAgentTool(
            SubagentDefinition(
                name="review",
                description="Review focused work.",
                goal_prompt="Review only.",
                allowed_tools=["read_file"],
            ),
            base_tool_registry=registry,
        ),
        source="agent",
        origin="review",
    )
    config = SimpleNamespace(
        model_name="fake",
        temperature=0.0,
        max_output_tokens=4096,
        subagent_profiles=[
            {
                "name": "execution",
                "allowed_tools": ["all"],
            }
        ],
    )
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Delegating again."),
                tool_calls=(
                    ToolCall(
                        call_id="nested-subagent",
                        tool_name="subagent_review",
                        arguments={"title": "Review", "instructions": "Review this."},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content='{"status": "failed", "summary": "No nested sub-agent."}')),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Execute focused work.",
            goal_prompt="Use configured tools.",
            allowed_tools=None,
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=config,
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)

    result = await tool.execute("sub-call", {"title": "Execute", "instructions": "Try nested."}, context)

    payload = json.loads(result.output)
    assert result.is_error is True
    assert payload["context"]["allowed_tools"] == ["read_file"]
    assert "subagent_review" not in payload["context"]["allowed_tools"]
    assert "Unknown tool name: subagent_review" in payload["raw_result"]


@pytest.mark.asyncio
async def test_subagent_allowed_skill_metadata_is_injected_without_skill_body(tmp_path):
    class RecordingFakeModel(FakeModelClient):
        def __init__(self):
            super().__init__([
                RuntimeResponse(message=Message(role="assistant", content='{"status": "completed"}')),
            ])
            self.system_prompt = ""

        async def complete(self, request):
            self.system_prompt = request.system_prompt
            return await super().complete(request)

    registry = ToolRegistry()
    config = SimpleNamespace(
        model_name="fake",
        temperature=0.0,
        max_output_tokens=4096,
        subagent_profiles=[
            {
                "name": "review",
                "allowed_skills": ["review"],
            }
        ],
    )
    model = RecordingFakeModel()
    tool = SubAgentTool(
        SubagentDefinition(
            name="review",
            description="Review focused work.",
            goal_prompt="Review carefully.",
            allowed_tools=[],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=config,
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={
            "global_active_skills": ["review"],
            "skill_catalog": {
                "review": {
                    "name": "review",
                    "description": "Review skill",
                    "source": "local",
                    "path": "/tmp/review/SKILL.md",
                    "license": "",
                    "compatibility": "",
                    "allowed_tools": [],
                    "content": "Always review carefully.",
                }
            },
        },
    )

    result = await tool.execute("sub-call", {"title": "Review", "instructions": "Review."}, context)

    assert result.is_error is False
    assert "name=review" in model.system_prompt
    assert "description=Review skill" in model.system_prompt
    assert "SKILL.md" in model.system_prompt
    assert "Always review carefully." not in model.system_prompt


@pytest.mark.asyncio
async def test_subagent_allowed_mcp_server_tools_are_callable_and_prompted(tmp_path):
    class RecordingFakeModel(FakeModelClient):
        def __init__(self):
            super().__init__(
                [
                    RuntimeResponse(
                        message=Message(role="assistant", content="Reading MCP."),
                        tool_calls=(ToolCall(call_id="sub-mcp-read", tool_name="fs_read", arguments={"path": "README.md"}),),
                        finish_reason="tool_calls",
                    ),
                    RuntimeResponse(message=Message(role="assistant", content='{"status": "completed", "summary": "Read MCP."}')),
                ]
            )
            self.system_prompt = ""

        async def complete(self, request):
            self.system_prompt = request.system_prompt
            return await super().complete(request)

    registry = ToolRegistry()
    mcp_tool = _PlainReadTool()
    mcp_tool.name = "fs_read"
    registry.register(mcp_tool, source="mcp", origin="filesystem")
    config = SimpleNamespace(
        model_name="fake",
        temperature=0.0,
        max_output_tokens=4096,
        subagent_profiles=[
            {
                "name": "execution",
                "allowed_tools": [],
                "allowed_mcps": ["filesystem"],
            }
        ],
    )
    model = RecordingFakeModel()
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Execute focused work.",
            goal_prompt="Use scoped MCP tools.",
            allowed_tools=[],
            allowed_mcps=[],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=config,
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)

    result = await tool.execute("sub-call", {"title": "MCP", "instructions": "Use MCP."}, context)

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["context"]["allowed_tools"] == ["fs_read"]
    assert payload["context"]["allowed_mcp_servers"] == ["filesystem"]
    assert "Allowed MCP servers: filesystem" in model.system_prompt


@pytest.mark.asyncio
async def test_subagent_mcp_tools_ignore_supervisor_mcp_scope(tmp_path):
    registry = ToolRegistry()
    mcp_tool = _PlainReadTool()
    mcp_tool.name = "fs_write_tools"
    registry.register(mcp_tool, source="mcp", origin="filesystem")
    config = SimpleNamespace(
        model_name="fake",
        temperature=0.0,
        max_output_tokens=4096,
        subagent_profiles=[
            {
                "name": "execution",
                "allowed_tools": [],
                "allowed_mcps": ["filesystem"],
            }
        ],
    )
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Using MCP write."),
                tool_calls=(ToolCall(call_id="sub-mcp-write", tool_name="fs_write_tools", arguments={"path": "README.md"}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content='{"status": "completed", "summary": "Used MCP."}')),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Execute focused work.",
            goal_prompt="Use scoped MCP tools.",
            allowed_tools=[],
            allowed_mcps=[],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=config,
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={"supervisor_available_tools": ["read_file", "subagent_execution"]},
    )

    result = await tool.execute("sub-call", {"title": "MCP", "instructions": "Use MCP."}, context)

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["context"]["allowed_tools"] == ["fs_write_tools"]
    assert payload["context"]["allowed_mcp_servers"] == ["filesystem"]


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
