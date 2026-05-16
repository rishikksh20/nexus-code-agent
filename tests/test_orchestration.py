from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import Message, RuntimeResponse, ToolExecutionContext, ToolResult
from nexus.runtime.agent import Agent
from nexus.runtime.context_state import load_multi_agent_state
from nexus.runtime.orchestration import (
    AgentRole,
    SharedState,
    TaskComplexity,
    TaskDAG,
    TaskNode,
    classify_task_complexity,
    decide_repair,
    parse_task_dag,
    run_post_execution_checks,
    run_orchestrated_turn,
)
from nexus.tools.base import ToolRegistry


@pytest.mark.asyncio
async def test_basic_agent_mode_calls_existing_turn_runner_path():
    state = SimpleNamespace(config=SimpleNamespace(agent_mode="basic"), session=SimpleNamespace(metadata={}))
    agent = Agent(FakeModelClient(), ToolRegistry())
    calls = []

    async def fake_turn_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return ["existing-path"]

    result = await run_orchestrated_turn(
        state,
        agent,
        prompt_text="hello",
        turn_runner=fake_turn_runner,
    )

    assert result == ["existing-path"]
    assert calls[0][1]["prompt_text"] == "hello"
    assert state.session.metadata == {}


@pytest.mark.asyncio
async def test_advanced_agent_mode_uses_normal_turn_runner_with_cognitive_tools():
    registry = ToolRegistry()
    state = SimpleNamespace(
        config=SimpleNamespace(
            agent_mode="advanced",
            multi_agent_show_plan=False,
            multi_agent_complexity_threshold="medium",
            model_name="fake",
            max_output_tokens=4096,
        ),
        session=SimpleNamespace(metadata={}),
        tool_registry=registry,
        delegation=None,
    )
    agent = Agent(FakeModelClient(), registry)
    prompts = []

    async def fake_turn_runner(*args, **kwargs):
        prompts.append(kwargs["prompt_text"])
        return ["normal-supervisor-path"]

    result = await run_orchestrated_turn(
        state,
        agent,
        prompt_text="Implement this complex plan",
        turn_runner=fake_turn_runner,
    )

    assert result == ["normal-supervisor-path"]
    assert prompts == ["Implement this complex plan"]
    assert state.session.metadata == {}


def test_parse_task_dag_rejects_invalid_json():
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_task_dag("not json", fallback_goal="goal")


def test_task_dag_enforces_dependency_order():
    with pytest.raises(ValueError, match="before dependencies"):
        TaskDAG(
            goal="bad order",
            nodes=(
                TaskNode(id="research", role=AgentRole.RESEARCH, objective="Inspect"),
                TaskNode(id="execute", role=AgentRole.EXECUTION, objective="Do it", dependencies=("research",)),
            ),
            execution_order=("execute", "research"),
        )


def test_complexity_classifier_thresholds():
    assert classify_task_complexity("What time is it?") is TaskComplexity.SIMPLE
    assert classify_task_complexity("Implement this plan across multiple files with tests") is TaskComplexity.LARGE


def test_repair_decision_detects_failed_verification():
    decision = decide_repair(
        verification_results=('run_typecheck: failed\n{"passed": false, "exit_code": 1}',),
        review_findings=(),
        max_iterations=2,
    )

    assert decision.retry is True
    assert decision.target_agent is AgentRole.EXECUTION


@pytest.mark.asyncio
async def test_post_execution_checks_store_changed_files_and_repair_decision(tmp_path):
    registry = ToolRegistry()
    registry.register(_StaticTool("git_status", '{"staged": [], "unstaged": ["nexus/app.py"], "untracked": []}'))
    registry.register(_StaticTool("run_typecheck", '{"passed": false, "exit_code": 1}', is_error=True))
    registry.register(_StaticTool("git_diff", "diff --git a/nexus/app.py b/nexus/app.py\n+broken"))
    state = SimpleNamespace(
        config=SimpleNamespace(
            workspace_root=tmp_path,
            allow_hidden_paths=False,
            model_name="fake",
            max_output_tokens=4096,
            multi_agent_max_repair_iterations=2,
        ),
        session=SimpleNamespace(session_id="session", metadata={}),
        current_turn_id="turn",
        current_trace_id="trace",
        approval_manager=SimpleNamespace(policy=SimpleNamespace(value="on-request")),
        tool_registry=registry,
    )
    agent = Agent(FakeModelClient(scripted=[RuntimeResponse(message=Message(role="assistant", content="Blocking finding: typecheck failed."))]), registry)
    dag = TaskDAG(goal="Fix app", nodes=(TaskNode(id="execute", role=AgentRole.EXECUTION, objective="Fix"),))

    shared_state = await run_post_execution_checks(
        state,
        agent,
        dag=dag,
        complexity=TaskComplexity.LARGE,
        prior_state=SharedState(dag=dag),
    )

    assert shared_state.changed_files == ("nexus/app.py",)
    assert shared_state.repair_decision is not None
    assert shared_state.repair_decision.retry is True
    typed_state = load_multi_agent_state(state.session.metadata)
    assert typed_state.artifacts
    assert any(packet.packet_type == "test_failure" for packet in typed_state.packets)
    assert any(packet.packet_type == "repair_request" for packet in typed_state.packets)


class _StaticTool:
    kind = "read"
    is_mutating = False
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str, output: str, *, is_error: bool = False) -> None:
        self.name = name
        self.description = name
        self._output = output
        self._is_error = is_error

    async def execute(
        self,
        call_id: str,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> ToolResult:
        del arguments, context
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=self._output,
            is_error=self._is_error,
        )
