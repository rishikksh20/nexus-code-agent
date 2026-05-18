from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import ConfirmationKind, ConfirmationRequest, ConfirmationResponse, Message, RuntimeResponse, ToolCall, ToolResult
from nexus.security import ApprovalManager, ApprovalPolicy, ApprovalScope, PermissionChecker, PermissionDecision
from nexus.runtime.execution import ExecutionMode
from nexus.sandbox.agent_tool import SubAgentTool, SubagentDefinition, _record_inner_approval
from nexus.skills import Skill, SkillRegistry
from nexus.tools.base import Tool, ToolKind, ToolRegistry
from nexus.tools.builtin import GetTimeTool, MemoryTool, PythonLspTool, WriteFileTool
from nexus.tools.registry import get_core_tools
from nexus.tools.subagents import (
    load_subagent_definitions,
    load_subagent_definitions_from_skills,
    register_skill_subagent_tools,
    register_subagent_tools,
)


class FailingRunTestsTool(Tool):
    name = "run_tests"
    description = "Failing test runner for regression coverage."
    kind = ToolKind.READ
    is_mutating = False
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, call_id, arguments, context):
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output="tests/test_app.py::test_read_main FAILED\nE TypeError: unhashable type: 'dict'\nExit code: 1",
            is_error=True,
            metadata={"exit_code": 1},
        )


class RecordingFakeModelClient(FakeModelClient):
    def __init__(self, scripted=None) -> None:
        super().__init__(scripted=scripted)
        self.requests = []

    async def chat_completion(self, request, *, stream: bool = True):
        self.requests.append(request)
        async for event in super().chat_completion(request, stream=stream):
            yield event


def test_core_tools_do_not_register_legacy_write_note_alias(tmp_path):
    config = SimpleNamespace(memory_dir=tmp_path / "memory")

    tool_names = [tool.name for tool in get_core_tools(config)]

    assert "write_file" in tool_names
    assert "edit" in tool_names
    assert "insert_edit_into_file" in tool_names
    assert "apply_patch" in tool_names
    assert "lsp" in tool_names
    assert "modify_file" not in tool_names
    assert "write_note" not in tool_names
    assert len(tool_names) == len(set(tool_names))


@pytest.mark.asyncio
async def test_get_time_tool_returns_utc_timestamp(tool_context):
    result = await GetTimeTool().execute("call-1", {}, tool_context)

    assert result.tool_name == "get_time"
    assert "T" in result.output
    assert result.metadata["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_memory_tool_persists_entries_as_dictionary(tmp_path, tool_context):
    memory_dir = tmp_path / ".nexus" / "memory"
    tool = MemoryTool(memory_dir=memory_dir)

    result = await tool.execute(
        "call-memory",
        {"action": "set", "key": "user_name", "value": "rishikesh"},
        tool_context,
    )

    payload = json.loads((memory_dir / "user_memory.json").read_text(encoding="utf-8"))

    assert result.is_error is False
    assert payload == {"entries": {"user_name": "rishikesh"}}


def test_permission_checker_denies_direct_nexus_memory_file_writes_with_memory_hint(tmp_path, tool_context):
    result = PermissionChecker().evaluate(
        WriteFileTool(),
        {"path": ".nexus/memory/user_name.md", "content": "rishikesh"},
        ExecutionMode.DEFAULT,
        context=tool_context,
    )

    assert result.decision is PermissionDecision.DENY
    assert "memory" in result.reason.lower()


@pytest.mark.asyncio
async def test_register_subagent_tools_registers_default_and_specialist_tools():
    registry = ToolRegistry()
    config = SimpleNamespace(agent_mode="advanced")
    definitions = [
        SubagentDefinition(
            name="explore",
            description="Investigate a focused codebase question.",
            goal_prompt="Explore the requested slice and summarize the result.",
        )
    ]

    count = register_subagent_tools(registry, config, definitions=definitions)

    assert count == 5
    specialist = registry.record("subagent_explore")
    assert specialist.source == "agent"
    assert specialist.origin == "explore"
    assert specialist.tool.is_mutating is False
    assert registry.record("subagent_planning_analysis").origin == "planning_analysis"
    assert registry.record("subagent_execution").origin == "execution"
    assert registry.record("subagent_review").origin == "review"
    assert registry.record("subagent_verification").origin == "verification"
    verification_tools = registry.record("subagent_verification").tool._definition.allowed_tools
    assert "bash" in verification_tools


@pytest.mark.asyncio
async def test_subagent_tool_returns_structured_supervisor_envelope(tool_context):
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    tool = SubAgentTool(
        SubagentDefinition(
            name="planning_analysis",
            description="Analyze and plan.",
            goal_prompt="Return a plan.",
            allowed_tools=["get_time"],
        ),
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )

    result = await tool.execute(
        "call-1",
        {
            "title": "Plan focused change",
            "instructions": "Inspect the task and summarize next steps.",
            "input_packet_ids": ["packet-0001"],
        },
        tool_context,
    )

    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["status"] == "completed"
    assert payload["agent"] == "subagent_planning_analysis"
    assert payload["role"] == "planning_analysis"
    assert payload["context"]["scope"] == "isolated"
    assert payload["context"]["input_packet_ids"] == ["packet-0001"]
    assert payload["raw_result"]
    assert result.metadata["context_snapshot"]["scope"] == "isolated"


@pytest.mark.asyncio
async def test_subagent_goal_prompt_stays_in_system_prompt_only(tool_context):
    registry = ToolRegistry()
    goal_prompt = "Return a careful implementation plan."
    task_instructions = "Inspect the task and summarize next steps."
    model = RecordingFakeModelClient(
        scripted=[
            RuntimeResponse(message=Message(role="assistant", content="Plan ready.")),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="planning_analysis",
            description="Analyze and plan.",
            goal_prompt=goal_prompt,
            allowed_tools=[],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )

    result = await tool.execute(
        "call-plan",
        {"title": "Plan focused change", "instructions": task_instructions},
        tool_context,
    )

    assert result.is_error is False
    request = model.requests[0]
    assert goal_prompt in request.system_prompt
    assert request.system_prompt.count(goal_prompt) == 1
    assert request.messages[-1].content == task_instructions
    assert goal_prompt not in request.messages[-1].content


@pytest.mark.asyncio
async def test_subagent_tool_preserves_failed_test_output_after_vague_final_response(tool_context):
    registry = ToolRegistry()
    registry.register(FailingRunTestsTool(), source="core", origin="test")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Running the focused tests."),
                tool_calls=(
                    ToolCall(call_id="call-tests", tool_name="run_tests", arguments={}),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(
                message=Message(
                    role="assistant",
                    content="It seems I'm having difficulty accessing the necessary tools to proceed.",
                ),
            ),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Implement and validate.",
            goal_prompt="Run tests and report the result.",
            allowed_tools=["run_tests"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )

    result = await tool.execute(
        "call-1",
        {"title": "Validate app", "instructions": "Run focused tests and report failures."},
        tool_context,
    )

    payload = json.loads(result.output)
    assert payload["status"] == "failed"
    assert payload["runtime_status"] == "failed"
    assert payload["recommended_next_action"] == "continue"
    assert "tests/test_app.py::test_read_main FAILED" in payload["raw_result"]
    assert "difficulty accessing the necessary tools" in payload["raw_result"]


@pytest.mark.asyncio
async def test_subagent_tool_marks_unresolved_clarification_as_error(tool_context):
    registry = ToolRegistry()
    registry.register(WriteFileTool(), source="core", origin="builtin")
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="I need to write a file."),
                tool_calls=(
                    ToolCall(call_id="missing-path", tool_name="write_file", arguments={"content": "hello"}),
                ),
                finish_reason="tool_calls",
            ),
        ]
    )
    tool = SubAgentTool(
        SubagentDefinition(
            name="execution",
            description="Implement a focused change.",
            goal_prompt="Write the requested file.",
            allowed_tools=["write_file"],
        ),
        model_client_factory=lambda: model,
        base_tool_registry=registry,
        config=SimpleNamespace(model_name="fake", temperature=0.0, max_output_tokens=4096),
    )

    result = await tool.execute(
        "call-clarify",
        {"title": "Write file", "instructions": "Write a file, asking for missing details if needed."},
        tool_context,
    )

    payload = json.loads(result.output)
    assert result.is_error is True
    assert result.metadata["status"] == "needs_clarification"
    assert payload["status"] == "needs_clarification"
    assert payload["runtime_status"] == "needs_clarification"
    assert payload["recommended_next_action"] == "ask_user"
    assert "Provide a value for 'path'" in payload["raw_result"]


@pytest.mark.asyncio
async def test_subagent_tool_requires_registry_for_direct_execution(tool_context):
    tool = SubAgentTool(
        SubagentDefinition(
            name="planning_analysis",
            description="Analyze and plan.",
            goal_prompt="Return a plan.",
            allowed_tools=["get_time"],
        )
    )

    result = await tool.execute(
        "call-1",
        {"title": "Plan focused change", "instructions": "Inspect the task."},
        tool_context,
    )

    assert result.is_error is True
    assert "not attached to a tool registry" in result.output


def test_subagent_inner_approval_turn_scope_matches_supervisor_behavior():
    manager = ApprovalManager(policy=ApprovalPolicy.ON_REQUEST)
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow tool 'write_file'?",
        reason="write_file replaces the entire file.",
        call_id="call-1",
        payload={"approval_policy": "on-request", "risk_level": "medium"},
        arguments={"path": "one.txt", "content": "one"},
    )

    _record_inner_approval(
        manager,
        request,
        ConfirmationResponse(approved=True, scope=ApprovalScope.TURN.value),
    )

    assert manager.is_turn_wide_mutating_preapproved(
        "write_file",
        is_mutating=True,
        risk_level="medium",
    )
    assert manager.is_pre_approved("write_file", {"path": "two.txt", "content": "two"}) is False


@pytest.mark.asyncio
async def test_register_subagent_tools_advanced_mode_does_not_require_delegation_flag():
    registry = ToolRegistry()
    config = SimpleNamespace(agent_mode="advanced")

    count = register_subagent_tools(registry, config)

    assert count == 4
    assert {record.name for record in registry.records()} == {
        "subagent_planning_analysis",
        "subagent_execution",
        "subagent_review",
        "subagent_verification",
    }


@pytest.mark.asyncio
async def test_register_subagent_tools_skips_unless_advanced_mode():
    registry = ToolRegistry()
    config = SimpleNamespace(agent_mode="basic")

    count = register_subagent_tools(registry, config)

    assert count == 0
    assert registry.records() == []


def test_load_subagent_definitions_builds_definition_objects():
    config = SimpleNamespace(
        delegation_subagents=[
            {
                "name": "explore",
                "description": "Investigate a focused codebase question.",
                "goal_prompt": "Read the relevant code and summarize the answer.",
                "allowed_tools": ["read_file", "glob", "grep"],
                "max_turns": 12,
                "timeout_seconds": 300,
            }
        ]
    )

    definitions = load_subagent_definitions(config)

    assert len(definitions) == 1
    assert definitions[0].name == "explore"
    assert definitions[0].allowed_tools == ["read_file", "glob", "grep"]
    assert definitions[0].max_turns == 12


def test_load_subagent_definitions_from_skills_uses_subagent_prefix():
    registry = SkillRegistry()
    registry.register(
        Skill(
            name="subagent-review",
            description="Review a focused code slice.",
            content="Inspect the selected code and report issues.",
        )
    )
    registry.register(
        Skill(
            name="nexus-agent",
            description="Builtin self-documentation skill.",
            content="Ignore me.",
        )
    )

    definitions = load_subagent_definitions_from_skills(registry)

    assert len(definitions) == 1
    assert definitions[0].name == "review"
    assert definitions[0].description == "Review a focused code slice."


@pytest.mark.asyncio
async def test_register_subagent_tools_respects_tool_filters():
    registry = ToolRegistry()
    config = SimpleNamespace(
        agent_mode="advanced",
        allowed_tools=["subagent_explore"],
        denied_tools=[],
    )
    definitions = [
        SubagentDefinition(
            name="explore",
            description="Investigate a focused codebase question.",
            goal_prompt="Explore the requested slice and summarize the result.",
        )
    ]

    count = register_subagent_tools(registry, config, definitions=definitions)

    assert count == 1
    assert registry.records()[0].name == "subagent_explore"


@pytest.mark.asyncio
async def test_register_skill_subagent_tools_registers_skill_backed_cognitive_tools():
    registry = ToolRegistry()
    config = SimpleNamespace(agent_mode="advanced", allowed_tools=[], denied_tools=[])
    skill_registry = SkillRegistry()
    skill_registry.register(
        Skill(
            name="subagent-review",
            description="Review a focused code slice.",
            content="Inspect the selected code and report issues.",
        )
    )

    count = register_skill_subagent_tools(registry, config, skill_registry)

    assert count == 1
    record = registry.record("subagent_review")
    assert record.source == "agent-skill"
    assert record.origin == "review"


@pytest.mark.asyncio
async def test_python_lsp_document_symbols_and_hover(tool_context):
    source = tool_context.working_directory / "sample.py"
    source.write_text(
        'class Greeter:\n'
        '    """Friendly class."""\n'
        '    greeting = "hi"\n'
        '\n'
        '    def greet(self, name):\n'
        '        """Return a greeting."""\n'
        '        return format_name(name)\n'
        '\n'
        'def format_name(value):\n'
        '    return value.title()\n',
        encoding="utf-8",
    )
    tool = PythonLspTool()

    symbols = await tool.execute("call-lsp-1", {"operation": "document_symbol", "file_path": "sample.py"}, tool_context)
    hover = await tool.execute(
        "call-lsp-2",
        {"operation": "hover", "file_path": "sample.py", "symbol": "greet"},
        tool_context,
    )

    assert symbols.is_error is False
    assert "class Greeter - sample.py:1:1" in symbols.output
    assert "function Greeter.greet - sample.py:5:5" in symbols.output
    assert "variable Greeter.greeting - sample.py:3:5" in symbols.output
    assert hover.is_error is False
    assert "function Greeter.greet" in hover.output
    assert "docstring: Return a greeting." in hover.output


@pytest.mark.asyncio
async def test_python_lsp_workspace_definition_and_references(tool_context):
    (tool_context.working_directory / "lib.py").write_text(
        "def helper(value):\n"
        "    return value + 1\n",
        encoding="utf-8",
    )
    (tool_context.working_directory / "main.py").write_text(
        "from lib import helper\n"
        "\n"
        "result = helper(41)\n",
        encoding="utf-8",
    )
    tool = PythonLspTool()

    workspace_symbols = await tool.execute(
        "call-lsp-3",
        {"operation": "workspace_symbol", "query": "help"},
        tool_context,
    )
    definition = await tool.execute(
        "call-lsp-4",
        {"operation": "go_to_definition", "file_path": "main.py", "line": 3, "character": 10},
        tool_context,
    )
    references = await tool.execute(
        "call-lsp-5",
        {"operation": "find_references", "file_path": "main.py", "symbol": "helper"},
        tool_context,
    )

    assert workspace_symbols.is_error is False
    assert "function helper - lib.py:1:1" in workspace_symbols.output
    assert definition.is_error is False
    assert "function helper - lib.py:1:1" in definition.output
    assert references.is_error is False
    assert "lib.py:1:def helper(value):" in references.output
    assert "main.py:3:result = helper(41)" in references.output


@pytest.mark.asyncio
async def test_python_lsp_rejects_outside_and_non_python_paths(tool_context):
    tool = PythonLspTool()
    text_file = tool_context.working_directory / "notes.txt"
    text_file.write_text("hello", encoding="utf-8")

    outside = await tool.execute(
        "call-lsp-6",
        {"operation": "document_symbol", "file_path": "../escape.py"},
        tool_context,
    )
    non_python = await tool.execute(
        "call-lsp-7",
        {"operation": "document_symbol", "file_path": "notes.txt"},
        tool_context,
    )

    assert outside.is_error is True
    assert "outside the current workspace" in outside.output
    assert non_python.is_error is True
    assert "Python files only" in non_python.output
