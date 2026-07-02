from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import AgentEventType, Message, RuntimeResponse, ToolCall, ToolExecutionContext, ToolResult
from nexus.runtime.agent import Agent
from nexus.runtime.context_state import load_multi_agent_state, load_subagent_continuation
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


class _StrictHistoryModel(FakeModelClient):
    def __init__(self, scripted) -> None:
        super().__init__(scripted=scripted)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        _assert_provider_safe_tool_history(request.messages)
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


class _CountingReadTool(_PlainReadTool):
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, call_id, arguments, context):
        self.calls += 1
        return await super().execute(call_id, arguments, context)


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


def _assert_provider_safe_tool_history(messages) -> None:
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        expected = {tool_call.call_id for tool_call in message.tool_calls}
        actual: set[str] = set()
        for following in messages[index + 1 :]:
            if following.role != "tool":
                break
            if following.tool_call_id:
                actual.add(following.tool_call_id)
        assert actual == expected


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
    assert "Preferred for file edits, implementation, and cheap local validation" in subagent_description
    assert "provide both title and instructions" in subagent_description
    assert "Supervisor direct-use path" in direct_description


@pytest.mark.asyncio
async def test_supervisor_repairs_subagent_call_missing_title_and_instructions(tmp_path):
    registry = ToolRegistry()
    registry.register(
        SubAgentTool(
            SubagentDefinition(
                name="execution",
                description="Implement focused work.",
                goal_prompt="Use normal tools to implement the assigned change.",
                allowed_tools=[],
            ),
            base_tool_registry=registry,
        ),
        source="agent",
        origin="execution",
    )
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Delegating without args."),
                tool_calls=(ToolCall(call_id="bad-subagent", tool_name="subagent_execution", arguments={}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content="Recovered.")),
        ]
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={"supervisor_available_tools": ["subagent_execution"]},
    )
    agent = Agent(model_client=model, tool_registry=registry)

    events = [
        event
        async for event in agent.run(
            [Message(role="user", content="write a file")],
            context,
            max_turns=3,
        )
    ]

    result = next(event.payload for event in events if event.kind == AgentEventType.TOOL_RESULT)
    assert result.is_error is True
    assert "Missing required argument(s) for tool 'subagent_execution': 'title', 'instructions'" in result.output
    assert "For a new sub-agent task, supply both 'title' and 'instructions'" in result.output
    assert "Both 'title' and 'instructions' are required" not in result.output


@pytest.mark.asyncio
async def test_execution_subagent_can_execute_allowed_write_tool(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteFileTool(), source="core", origin="builtin")
    model = _StrictHistoryModel(
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
    assert len(model.requests) == 2
    assert model.requests[0].messages[0].content.startswith("Begin delegated task: Write file.")
    assert "Create subagent.txt with the requested content." not in model.requests[0].messages[0].content


@pytest.mark.asyncio
async def test_subagent_request_context_truncates_oversized_instructions(tmp_path):
    registry = ToolRegistry()
    long_instructions = "inspect this carefully\n" + ("context-line\n" * 2_000)
    model = _RecordingModel()
    tool = SubAgentTool(
        SubagentDefinition(
            name="explorer",
            description="Explore focused context.",
            goal_prompt="Return a concise summary.",
            allowed_tools=[],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)

    result = await tool.execute(
        "sub-call",
        {"title": "Large context", "instructions": long_instructions},
        context,
    )

    assert result.is_error is False
    assert len(model.requests) == 1
    request = model.requests[0]
    assert len(request.messages[0].content) < 320
    assert "context-line" not in request.messages[0].content
    assert "task instructions truncated by Nexus" in request.system_prompt
    assert len(request.system_prompt) < len(long_instructions)


@pytest.mark.asyncio
async def test_subagent_tool_turn_does_not_surface_inner_max_loop_pause(tmp_path):
    registry = ToolRegistry()
    registry.register(_PlainReadTool(), source="core", origin="builtin")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Reading the file."),
                tool_calls=(ToolCall(call_id="sub-read", tool_name="read_file", arguments={"path": "README.md"}),),
                finish_reason="tool_calls",
            ),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Read focused context.",
            goal_prompt="Use normal tools to inspect the assigned file.",
            allowed_tools=["read_file"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)

    result = await tool.execute(
        "sub-call",
        {"title": "Read file", "instructions": "Read README.md and report what happened."},
        context,
    )

    payload = json.loads(result.output)
    assert result.is_error is False
    assert payload["context"]["tool_call_count"] == 1
    assert "Single-query turn limit reached" not in payload["raw_result"]
    assert "max_loop_iterations" not in payload["raw_result"]
    assert "Completed read_file: read ok" in payload["raw_result"]


@pytest.mark.asyncio
async def test_subagent_tool_call_limit_does_not_persist_unmatched_tool_calls(tmp_path):
    registry = ToolRegistry()
    registry.register(_PlainReadTool(), source="core", origin="builtin")
    tool_calls = tuple(
        ToolCall(
            call_id=f"read-{index}",
            tool_name="read_file",
            arguments={"path": f"file-{index}.txt"},
        )
        for index in range(31)
    )
    model = _StrictHistoryModel(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Reading many files."),
                tool_calls=tool_calls,
                finish_reason="tool_calls",
            ),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="planning_analysis",
            description="Explore focused context.",
            goal_prompt="Use normal tools to inspect the assigned files.",
            allowed_tools=["read_file"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(
            model_name="fake",
            temperature=0.0,
            max_output_tokens=4096,
            parallel_tools=False,
            parallel_tool_window=4,
        ),
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)

    result = await tool.execute(
        "sub-call",
        {"title": "Explore many files", "instructions": "Read every file in the target set."},
        context,
    )

    payload = json.loads(result.output)
    assert result.is_error is True
    assert payload["status"] == "failed"
    assert payload["context"]["tool_call_count"] == 30
    assert "Tool call limit reached" in payload["raw_result"]
    assert len(model.requests) == 1


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
async def test_same_turn_duplicate_read_tool_call_reuses_cached_result(tmp_path):
    registry = ToolRegistry()
    read_tool = _CountingReadTool()
    registry.register(read_tool, source="core", origin="test")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Reading once."),
                tool_calls=(ToolCall(call_id="read-1", tool_name="read_file", arguments={"path": "README.md"}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(
                message=Message(role="assistant", content="Reading again."),
                tool_calls=(ToolCall(call_id="read-2", tool_name="read_file", arguments={"path": "README.md"}),),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content="Done.")),
        ]
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path)
    agent = Agent(model_client=model, tool_registry=registry)

    events = [event async for event in agent.run([Message(role="user", content="read twice")], context, max_turns=3)]

    results = [event.payload for event in events if event.kind == AgentEventType.TOOL_RESULT]
    assert read_tool.calls == 1
    assert [result.call_id for result in results] == ["read-1", "read-2"]
    assert results[1].metadata["read_cache_hit"] is True
    assert results[1].metadata["cached_from_call_id"] == "read-1"


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
async def test_impact_analyzer_subagent_persists_structured_handoff_packet(tmp_path):
    registry = ToolRegistry()
    impact_payload = {
        "status": "completed",
        "summary": "Scoped runtime agent impact.",
        "changed_files": ["nexus/runtime/agent.py"],
        "affected_modules": ["nexus.runtime"],
        "public_interfaces_changed": [],
        "risk_level": "medium",
        "validation_category": "auto_validatable",
        "candidate_review_targets": ["nexus/runtime/agent.py"],
        "candidate_tests": ["tests/test_subagent_tool_flow.py"],
        "verification_policy": {
            "syntax_check": True,
            "formatter_check": False,
            "unit_tests": ["tests/test_subagent_tool_flow.py"],
            "integration_tests": [],
            "e2e_tests": [],
            "manual_validation": [],
        },
        "failure_attribution_hints": ["Read-cache failures should relate to agent tool execution."],
    }
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(message=Message(role="assistant", content=json.dumps(impact_payload))),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="verification",
            description="Analyze impact.",
            goal_prompt="Return impact JSON.",
            allowed_tools=[],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path, metadata={})

    result = await tool.execute("impact-call", {"title": "Impact", "instructions": "Analyze the diff."}, context)

    payload = json.loads(result.output)
    state = load_multi_agent_state(context.metadata)
    assert result.is_error is False
    assert payload["output_packet_ids"] == ["packet-0001"]
    assert result.metadata["output_packet_ids"] == ["packet-0001"]
    assert state.packets[0].packet_type == "impact_analysis"
    assert state.packets[0].modified_files == ("nexus/runtime/agent.py",)
    assert state.packets[0].recommended_tests == ("tests/test_subagent_tool_flow.py",)
    assert state.packets[0].artifacts


@pytest.mark.asyncio
async def test_subagent_packets_persist_to_durable_session_metadata(tmp_path):
    registry = ToolRegistry()
    durable_metadata = {}
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(
                    role="assistant",
                    content=json.dumps(
                        {
                            "status": "needs_clarification",
                            "summary": "Need a product decision.",
                            "findings": ["Both config layouts are viable."],
                            "clarifications_needed": ["Should config be global, project-specific, or both?"],
                        }
                    ),
                )
            ),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="planning_analysis",
            description="Explore focused work.",
            goal_prompt="Inspect and summarize.",
            allowed_tools=[],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={"session_metadata": durable_metadata},
    )

    result = await tool.execute("explore-call", {"title": "Explore config", "instructions": "Inspect config scope."}, context)

    state = load_multi_agent_state(durable_metadata)
    assert result.is_error is False
    assert state.packets[0].task_id == "explore-call"
    assert state.continuations["explore-call"].status == "needs_clarification"
    assert context.metadata["multi_agent_packet_summaries"]["packet-0001"]


@pytest.mark.asyncio
async def test_subagent_resume_rehydrates_compact_logical_task_context(tmp_path):
    class RecordingModel(FakeModelClient):
        def __init__(self, response):
            super().__init__([RuntimeResponse(message=Message(role="assistant", content=json.dumps(response)))])
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            return await super().complete(request)

    registry = ToolRegistry()
    durable_metadata = {}
    first_model = RecordingModel(
        {
            "status": "needs_clarification",
            "summary": "Two auth strategies are viable.",
            "findings": ["No existing auth implementation was found."],
            "related_files": ["nexus/app.py"],
            "clarifications_needed": ["Should Nexus use JWT or session auth?"],
        }
    )
    resumed_model = RecordingModel(
        {
            "status": "completed",
            "summary": "Updated the plan for JWT auth.",
            "findings": ["Use JWT auth."],
        }
    )
    models = iter((first_model, resumed_model))
    tool = SubAgentTool(
        SubagentDefinition(
            name="planning_analysis",
            description="Explore focused work.",
            goal_prompt="Inspect and summarize.",
            allowed_tools=[],
        ),
        model_client_factory=lambda: next(models),
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(
        session_id="test-session",
        working_directory=tmp_path,
        metadata={
            "session_metadata": durable_metadata,
            "supervisor_task_input": "Add authentication to Nexus.",
        },
    )

    first_result = await tool.execute(
        "planning-call",
        {"title": "Explore auth", "instructions": "Inspect existing auth patterns and propose a plan."},
        context,
    )
    resumed_result = await tool.execute(
        "resume-call",
        {
            "resume_task_id": "planning-call",
            "clarification": {
                "question": "Should Nexus use JWT or session auth?",
                "answer": "Use JWT auth.",
                "selected_option_id": "jwt",
            },
        },
        context,
    )

    payload = json.loads(resumed_result.output)
    continuation = load_subagent_continuation(durable_metadata, "planning-call")
    state = load_multi_agent_state(durable_metadata)
    assert json.loads(first_result.output)["status"] == "needs_clarification"
    assert payload["task_id"] == "planning-call"
    assert resumed_result.metadata["task_id"] == "planning-call"
    assert continuation is not None
    assert continuation.status == "completed"
    assert state.tasks["planning-call"].status == "completed"
    assert state.tasks["planning-call"].assigned_agent_id == "subagent_planning_analysis"
    assert state.agents["subagent_planning_analysis"].task_id == "planning-call"
    assert continuation.clarification_answers[-1].selected_option_id == "jwt"
    assert "No existing auth implementation was found." in continuation.findings
    assert "Use JWT auth." in continuation.findings
    assert resumed_model.requests
    prompt = resumed_model.requests[0].system_prompt
    assert "Original supervisor request: Add authentication to Nexus." in prompt
    assert "Original delegated task: Inspect existing auth patterns and propose a plan." in prompt
    assert "Previous findings:" in prompt
    assert "No existing auth implementation was found." in prompt
    assert "Clarification question: Should Nexus use JWT or session auth?" in prompt
    assert "User answer: Use JWT auth." in prompt
    assert "Selected option id: jwt" in prompt
    assert "Do not ask the same clarification again." in prompt


@pytest.mark.asyncio
async def test_code_reviewer_subagent_persists_failure_analysis_packet(tmp_path):
    registry = ToolRegistry()
    review_payload = {
        "status": "failed",
        "summary": "Focused test failed.",
        "related_files": ["tests/test_runtime.py"],
        "tests_run": ["uv run pytest tests/test_runtime.py"],
        "failure_analysis": {
            "related_to_task": False,
            "confidence": 0.82,
            "reasoning_summary": "Failure is in an unrelated migration fixture.",
            "suspected_causes": ["pre-existing fixture issue"],
            "likely_preexisting": True,
            "recommended_next_action": "report_without_fixing",
        },
    }
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(message=Message(role="assistant", content=json.dumps(review_payload))),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="review",
            description="Review and verify.",
            goal_prompt="Classify failures.",
            allowed_tools=[],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )
    context = ToolExecutionContext(session_id="test-session", working_directory=tmp_path, metadata={})

    result = await tool.execute("review-call", {"title": "Review", "instructions": "Review the diff."}, context)

    state = load_multi_agent_state(context.metadata)
    assert result.is_error is False
    assert state.packets[0].packet_type == "failure_analysis"
    assert state.packets[0].confidence == 0.82
    assert state.packets[0].failure_summary == "Failure is in an unrelated migration fixture."


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
