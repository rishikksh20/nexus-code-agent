from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from nexus.models import AgentEvent, Message, RuntimeRequest, StreamEventType, ToolExecutionContext
from nexus.runtime.agent import Agent
from nexus.runtime.context_state import (
    AgentContextRecord,
    ContextPacket,
    ContextScope,
    estimate_messages,
    make_context_packet,
    record_agent_context,
    record_context_packet,
)
from nexus.runtime.repl_state import ReplState
from nexus.runtime.turn_runner import ConfirmationCallback, run_agent_turn
from nexus.tools.base import ToolRegistry
from nexus.ui import TerminalUI


class TaskComplexity(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    LARGE = "large"


class AgentRole(str, Enum):
    SUPERVISOR = "supervisor"
    PLANNER = "planner"
    RESEARCH = "research"
    EXECUTION = "execution"
    TEST = "test"
    REVIEW = "review"
    DOCS = "docs"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True, frozen=True)
class TaskNode:
    id: str
    role: AgentRole
    objective: str
    dependencies: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    claimed_resources: tuple[str, ...] = ()
    status: TaskStatus = TaskStatus.PENDING
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role.value,
            "objective": self.objective,
            "dependencies": list(self.dependencies),
            "allowed_tools": list(self.allowed_tools),
            "claimed_resources": list(self.claimed_resources),
            "status": self.status.value,
            "summary": self.summary,
        }


@dataclass(slots=True, frozen=True)
class TaskDAG:
    goal: str
    nodes: tuple[TaskNode, ...]
    execution_order: tuple[str, ...] = ()
    completed_summaries: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.execution_order:
            object.__setattr__(self, "execution_order", tuple(node.id for node in self.nodes))
        _validate_task_dag(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "nodes": [node.to_dict() for node in self.nodes],
            "execution_order": list(self.execution_order),
            "completed_summaries": dict(self.completed_summaries),
        }


@dataclass(slots=True, frozen=True)
class SharedState:
    dag: TaskDAG | None = None
    repo_map: tuple[str, ...] = ()
    findings: dict[str, str] = field(default_factory=dict)
    changed_files: tuple[str, ...] = ()
    verification_results: tuple[str, ...] = ()
    review_findings: tuple[str, ...] = ()
    repair_decision: RepairDecision | None = None
    context_packets: tuple[ContextPacket, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "dag": self.dag.to_dict() if self.dag else None,
            "repo_map": list(self.repo_map),
            "findings": dict(self.findings),
            "changed_files": list(self.changed_files),
            "verification_results": list(self.verification_results),
            "review_findings": list(self.review_findings),
            "repair_decision": self.repair_decision.to_dict() if self.repair_decision else None,
            "context_packets": [packet.to_dict() for packet in self.context_packets],
        }


@dataclass(slots=True, frozen=True)
class RepairDecision:
    retry: bool
    reason: str
    target_agent: AgentRole = AgentRole.EXECUTION
    max_repair_iteration: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "retry": self.retry,
            "reason": self.reason,
            "target_agent": self.target_agent.value,
            "max_repair_iteration": self.max_repair_iteration,
        }


async def run_orchestrated_turn(
    state: ReplState,
    agent: Agent,
    *,
    prompt_text: str,
    ui: TerminalUI | None = None,
    approval_callback: ConfirmationCallback | None = None,
    auto_confirm: bool = False,
    turn_runner: Callable[..., Awaitable[list[AgentEvent]]] = run_agent_turn,
) -> list[AgentEvent]:
    """Run a user turn through optional supervisor/planner orchestration.

    ``multi_agent_mode = "off"`` delegates directly to ``run_agent_turn`` so the
    existing single-agent runtime stays the default behavior. In ``auto`` or
    ``always`` mode, this function builds a validated task DAG without exposing
    tool schemas to the planner, stores the compact state in session metadata,
    and then executes through the existing turn runner.
    """
    mode = str(getattr(state.config, "multi_agent_mode", "off")).strip().lower()
    if mode == "off":
        return await turn_runner(
            state,
            agent,
            prompt_text=prompt_text,
            ui=ui,
            approval_callback=approval_callback,
            auto_confirm=auto_confirm,
        )

    complexity = (
        TaskComplexity.LARGE
        if mode == "always"
        else classify_task_complexity(
            prompt_text,
            threshold=TaskComplexity(str(getattr(state.config, "multi_agent_complexity_threshold", "medium"))),
        )
    )
    if complexity is TaskComplexity.SIMPLE:
        return await turn_runner(
            state,
            agent,
            prompt_text=prompt_text,
            ui=ui,
            approval_callback=approval_callback,
            auto_confirm=auto_confirm,
        )

    dag = await plan_task_dag(
        state,
        agent,
        prompt_text,
        complexity=complexity,
    )
    planner_packet = make_context_packet(
        source_agent="planner",
        target_agent="execution",
        summary=f"Validated DAG for goal: {dag.goal}",
        artifacts=tuple(node.objective for node in dag.nodes),
    )
    shared_state = SharedState(dag=dag, context_packets=(planner_packet,))
    _record_supervisor_context(state, dag=dag, packet=planner_packet)
    _store_shared_state(state, shared_state, complexity=complexity)
    if ui is not None and getattr(state.config, "multi_agent_show_plan", True):
        ui.print_markdown(_format_dag_for_display(dag))

    planned_prompt = _execution_prompt_with_plan(prompt_text, dag)
    record_agent_context(
        state.session.metadata,
        AgentContextRecord(
            agent_id="execution",
            role=AgentRole.EXECUTION.value,
            scope=ContextScope.HANDOFF,
            summary="Main execution agent receives user prompt plus validated supervisor DAG.",
            token_estimate=estimate_messages(getattr(state, "history", [])) + planner_packet.token_estimate,
            message_count=len(getattr(state, "history", [])),
            shared_inputs=(planner_packet.packet_id,),
            allowed_tools=tuple(record.name for record in state.tool_registry.records()),
        ),
    )
    events = await turn_runner(
        state,
        agent,
        prompt_text=planned_prompt,
        ui=ui,
        approval_callback=approval_callback,
        auto_confirm=auto_confirm,
    )
    post_state = await run_post_execution_checks(
        state,
        agent,
        dag=dag,
        complexity=complexity,
        prior_state=shared_state,
    )
    _store_shared_state(state, post_state, complexity=complexity)
    if ui is not None and getattr(state.config, "multi_agent_show_plan", True):
        ui.print_markdown(_format_post_check_summary(post_state))
    return events


def classify_task_complexity(
    prompt_text: str,
    *,
    threshold: TaskComplexity = TaskComplexity.MEDIUM,
) -> TaskComplexity:
    lowered = prompt_text.lower()
    large_markers = (
        "architecture",
        "multi agent",
        "multi-agent",
        "refactor",
        "roadmap",
        "implement this plan",
        "extensive",
        "end to end",
    )
    medium_markers = (
        "add tests",
        "update docs",
        "multiple files",
        "debug",
        "fix failing",
        "investigate",
        "review",
    )
    if any(marker in lowered for marker in large_markers) or len(prompt_text) > 1200:
        complexity = TaskComplexity.LARGE
    elif any(marker in lowered for marker in medium_markers) or len(prompt_text) > 400:
        complexity = TaskComplexity.MEDIUM
    else:
        complexity = TaskComplexity.SIMPLE

    if threshold is TaskComplexity.LARGE and complexity is TaskComplexity.MEDIUM:
        return TaskComplexity.SIMPLE
    if threshold is TaskComplexity.SIMPLE and complexity is TaskComplexity.SIMPLE:
        return TaskComplexity.MEDIUM
    return complexity


async def plan_task_dag(
    state: ReplState,
    agent: Agent,
    prompt_text: str,
    *,
    complexity: TaskComplexity,
) -> TaskDAG:
    system_prompt = _planner_system_prompt(
        max_steps=8 if complexity is TaskComplexity.MEDIUM else 12,
        tool_names=[record.name for record in state.tool_registry.records()],
    )
    request = RuntimeRequest(
        model_name=state.config.model_name,
        system_prompt=system_prompt,
        messages=(Message(role="user", content=prompt_text),),
        tool_schemas=(),
        temperature=0.0,
        max_output_tokens=min(int(state.config.max_output_tokens), 2048),
    )
    raw_text = ""
    async for event in agent.model_client.chat_completion(request, stream=True):
        if event.type is StreamEventType.TEXT_DELTA and event.text_delta:
            raw_text += event.text_delta.content
        elif event.type is StreamEventType.ERROR:
            raise ValueError(event.error or "Planner model request failed.")
    if not raw_text.strip():
        # FakeModelClient and some providers may return only MESSAGE_COMPLETE in
        # unusual cases. Fall back to a deterministic local plan.
        return default_task_dag(prompt_text, complexity=complexity)
    return parse_task_dag(raw_text, fallback_goal=prompt_text)


def parse_task_dag(raw_text: str, *, fallback_goal: str = "") -> TaskDAG:
    payload = _extract_json_object(raw_text)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Planner returned invalid JSON: {exc}") from exc

    goal = str(data.get("goal") or fallback_goal).strip()
    raw_nodes = data.get("tasks") or data.get("nodes")
    if not goal:
        raise ValueError("Planner DAG must include a non-empty goal.")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("Planner DAG must include at least one task node.")

    nodes: list[TaskNode] = []
    for index, item in enumerate(raw_nodes, start=1):
        if not isinstance(item, dict):
            raise ValueError("Planner task nodes must be objects.")
        node_id = str(item.get("id") or f"task_{index}").strip()
        role = _coerce_role(str(item.get("agent") or item.get("role") or "execution"))
        objective = str(item.get("objective") or item.get("description") or "").strip()
        if not node_id or not objective:
            raise ValueError("Planner task nodes require id and objective.")
        nodes.append(
            TaskNode(
                id=node_id,
                role=role,
                objective=objective,
                dependencies=tuple(str(dep).strip() for dep in item.get("depends_on", ()) or () if str(dep).strip()),
                allowed_tools=tuple(str(tool).strip() for tool in item.get("allowed_tools", ()) or () if str(tool).strip()),
                claimed_resources=tuple(str(path).strip() for path in item.get("claimed_resources", ()) or () if str(path).strip()),
            )
        )
    execution_order = tuple(str(item).strip() for item in data.get("execution_order", ()) or () if str(item).strip())
    return TaskDAG(goal=goal, nodes=tuple(nodes), execution_order=execution_order)


def default_task_dag(prompt_text: str, *, complexity: TaskComplexity) -> TaskDAG:
    nodes = [
        TaskNode(
            id="research",
            role=AgentRole.RESEARCH,
            objective="Map the relevant code paths and summarize existing patterns.",
            allowed_tools=("read_file", "glob", "grep", "list_dir", "lsp"),
        ),
        TaskNode(
            id="execute",
            role=AgentRole.EXECUTION,
            objective="Implement the requested change using the current Nexus runtime patterns.",
            dependencies=("research",),
        ),
        TaskNode(
            id="verify",
            role=AgentRole.TEST,
            objective="Run focused verification and summarize failures.",
            dependencies=("execute",),
            allowed_tools=("run_tests", "run_linter", "run_typecheck", "git_status"),
        ),
        TaskNode(
            id="review",
            role=AgentRole.REVIEW,
            objective="Review the resulting diff for bugs, regressions, and maintainability issues.",
            dependencies=("execute",),
            allowed_tools=("git_diff", "read_file", "grep", "lsp"),
        ),
    ]
    if complexity is TaskComplexity.MEDIUM:
        nodes = nodes[1:]
    return TaskDAG(goal=prompt_text.strip(), nodes=tuple(nodes))


async def run_post_execution_checks(
    state: ReplState,
    agent: Agent,
    *,
    dag: TaskDAG,
    complexity: TaskComplexity,
    prior_state: SharedState | None = None,
) -> SharedState:
    """Run read-only verification/review checks after planned execution.

    This deliberately avoids mutating tools and does not perform automatic
    repair. If checks fail, the repair decision is stored for the supervisor UI
    and for the next user-visible turn.
    """
    del complexity
    context = ToolExecutionContext(
        session_id=getattr(state.session, "session_id", "multi-agent-supervisor"),
        working_directory=getattr(state.config, "workspace_root", Path.cwd()),
        metadata={
            "turn_id": getattr(state, "current_turn_id", ""),
            "trace_id": getattr(state, "current_trace_id", ""),
            "approval_policy": getattr(getattr(state, "approval_manager", None), "policy", "on-request").value
            if hasattr(getattr(getattr(state, "approval_manager", None), "policy", None), "value")
            else str(getattr(getattr(state, "approval_manager", None), "policy", "on-request")),
            "allow_hidden_paths": getattr(state.config, "allow_hidden_paths", False),
        },
    )
    git_status = await _execute_optional_tool(state.tool_registry, "git_status", {}, context)
    changed_files = tuple(_changed_files_from_status(git_status))
    verification_results: list[str] = []
    review_findings: list[str] = []
    findings = dict(prior_state.findings) if prior_state else {}

    # Keep v1 lightweight: syntax/type verification plus diff review. Full test
    # execution remains available through run_tests and specialist workers.
    typecheck = await _execute_optional_tool(state.tool_registry, "run_typecheck", {}, context)
    if typecheck:
        verification_results.append(typecheck)
    diff = await _execute_optional_tool(state.tool_registry, "git_diff", {"target": "working"}, context)
    if diff and diff != "(no diff)":
        review = await summarize_review_findings(state, agent, dag=dag, diff=diff, verification="\n".join(verification_results))
        review_findings.append(review)
    elif diff:
        review_findings.append("No working-tree diff found for review.")

    repair_decision = decide_repair(
        verification_results=tuple(verification_results),
        review_findings=tuple(review_findings),
        max_iterations=int(getattr(state.config, "multi_agent_max_repair_iterations", 2)),
    )
    findings["post_execution"] = repair_decision.reason
    feedback_packet = make_context_packet(
        source_agent="test-review",
        target_agent="execution",
        summary=repair_decision.reason,
        artifacts=tuple((*verification_results, *review_findings))[:4],
    )
    record_context_packet(state.session.metadata, feedback_packet)
    record_agent_context(
        state.session.metadata,
        AgentContextRecord(
            agent_id="test-review",
            role="test+review",
            scope=ContextScope.HANDOFF,
            summary=repair_decision.reason,
            token_estimate=feedback_packet.token_estimate,
            shared_inputs=("execution diff", "verification output"),
            handoff_outputs=(feedback_packet.packet_id,),
            allowed_tools=("git_status", "git_diff", "run_typecheck"),
        ),
    )
    return SharedState(
        dag=dag,
        repo_map=prior_state.repo_map if prior_state else (),
        findings=findings,
        changed_files=changed_files,
        verification_results=tuple(verification_results),
        review_findings=tuple(review_findings),
        repair_decision=repair_decision,
        context_packets=(*((prior_state.context_packets if prior_state else ())), feedback_packet),
    )


async def summarize_review_findings(
    state: ReplState,
    agent: Agent,
    *,
    dag: TaskDAG,
    diff: str,
    verification: str,
) -> str:
    if not diff.strip():
        return "No diff to review."
    prompt = (
        "You are the Nexus review agent. Review the diff and verification output. "
        "Do not call tools. Return concise findings only; say 'No blocking findings' if clean.\n\n"
        f"Goal:\n{dag.goal}\n\nVerification:\n{verification or '(not run)'}\n\nDiff:\n{diff[:20000]}"
    )
    request = RuntimeRequest(
        model_name=state.config.model_name,
        system_prompt="Review code changes for concrete bugs, regressions, and missing tests. Do not call tools.",
        messages=(Message(role="user", content=prompt),),
        tool_schemas=(),
        temperature=0.0,
        max_output_tokens=min(int(state.config.max_output_tokens), 1200),
    )
    text = ""
    async for event in agent.model_client.chat_completion(request, stream=True):
        if event.type is StreamEventType.TEXT_DELTA and event.text_delta:
            text += event.text_delta.content
        elif event.type is StreamEventType.ERROR:
            return f"Review failed: {event.error or 'unknown error'}"
    return text.strip() or "Review produced no findings."


def decide_repair(
    *,
    verification_results: tuple[str, ...],
    review_findings: tuple[str, ...],
    max_iterations: int,
) -> RepairDecision:
    combined = "\n".join((*verification_results, *review_findings)).lower()
    failing_markers = (
        '"passed": false',
        '"exit_code": 1',
        '"exit_code": 2',
        "failed",
        "error",
        "blocking finding",
        "regression",
    )
    retry = any(marker in combined for marker in failing_markers)
    reason = (
        "Post-execution checks found issues; ask the execution agent for a focused repair."
        if retry
        else "Post-execution checks did not find blocking issues."
    )
    return RepairDecision(
        retry=retry,
        reason=reason,
        target_agent=AgentRole.EXECUTION,
        max_repair_iteration=max_iterations,
    )


async def _execute_optional_tool(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> str:
    try:
        tool = registry.get(tool_name)
    except LookupError:
        return f"{tool_name}: not registered"
    if tool.is_mutating:
        return f"{tool_name}: skipped because the supervisor only runs read-only post checks"
    result = await tool.execute(f"supervisor-{tool_name}", arguments, context)
    prefix = f"{tool_name}: {'failed' if result.is_error else 'passed'}"
    return f"{prefix}\n{result.output}"


def _changed_files_from_status(status_output: str) -> list[str]:
    if not status_output or status_output.endswith("not registered"):
        return []
    _, _, body = status_output.partition("\n")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    paths: set[str] = set()
    for key in ("staged", "unstaged", "untracked"):
        value = payload.get(key, [])
        if isinstance(value, list):
            paths.update(str(item) for item in value)
    return sorted(paths)


def _validate_task_dag(dag: TaskDAG) -> None:
    ids = {node.id for node in dag.nodes}
    if len(ids) != len(dag.nodes):
        raise ValueError("Task DAG contains duplicate node ids.")
    for node in dag.nodes:
        missing = [dep for dep in node.dependencies if dep not in ids]
        if missing:
            raise ValueError(f"Task node '{node.id}' depends on unknown node(s): {', '.join(missing)}")
    order = dag.execution_order or tuple(node.id for node in dag.nodes)
    if set(order) != ids:
        raise ValueError("Task DAG execution_order must contain every node id exactly once.")
    seen: set[str] = set()
    for node_id in order:
        node = next(node for node in dag.nodes if node.id == node_id)
        missing = [dep for dep in node.dependencies if dep not in seen]
        if missing:
            raise ValueError(f"Task DAG execution_order schedules '{node_id}' before dependencies: {', '.join(missing)}")
        seen.add(node_id)


def _planner_system_prompt(*, max_steps: int, tool_names: list[str]) -> str:
    return (
        "You are the Nexus planner agent. Produce only JSON and do not call tools.\n"
        "Return a task DAG with this shape: "
        '{"goal": "...", "tasks": [{"id": "research", "agent": "research", '
        '"objective": "...", "depends_on": [], "allowed_tools": []}], '
        '"execution_order": ["research"]}.\n'
        f"Use at most {max_steps} tasks. Valid agents: research, execution, test, review, docs. "
        f"Known tools for allowed_tools hints: {', '.join(tool_names)}."
    )


def _extract_json_object(raw_text: str) -> str:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return stripped
    return stripped[start : end + 1]


def _coerce_role(raw_role: str) -> AgentRole:
    normalized = raw_role.strip().lower()
    aliases = {
        "tester": "test",
        "verification": "test",
        "verify": "test",
        "document": "docs",
        "documentation": "docs",
        "executor": "execution",
    }
    normalized = aliases.get(normalized, normalized)
    try:
        return AgentRole(normalized)
    except ValueError:
        return AgentRole.EXECUTION


def _store_shared_state(state: ReplState, shared_state: SharedState, *, complexity: TaskComplexity) -> None:
    state.session.metadata["multi_agent"] = {
        "mode": getattr(state.config, "multi_agent_mode", "off"),
        "complexity": complexity.value,
        "shared_state": shared_state.to_dict(),
    }


def _record_supervisor_context(state: ReplState, *, dag: TaskDAG, packet: ContextPacket) -> None:
    record_context_packet(state.session.metadata, packet)
    record_agent_context(
        state.session.metadata,
        AgentContextRecord(
            agent_id="supervisor",
            role=AgentRole.SUPERVISOR.value,
            scope=ContextScope.SHARED,
            summary=f"Supervising DAG with {len(dag.nodes)} task(s): {dag.goal}",
            token_estimate=estimate_messages(getattr(state, "history", [])),
            message_count=len(getattr(state, "history", [])),
            handoff_outputs=(packet.packet_id,),
        ),
    )
    record_agent_context(
        state.session.metadata,
        AgentContextRecord(
            agent_id="planner",
            role=AgentRole.PLANNER.value,
            scope=ContextScope.ISOLATED,
            summary=f"Planner produced validated DAG for: {dag.goal}",
            token_estimate=packet.token_estimate,
            message_count=1,
            handoff_outputs=(packet.packet_id,),
        ),
    )


def _format_dag_for_display(dag: TaskDAG) -> str:
    lines = ["### Multi-Agent Plan", "", f"Goal: {dag.goal}", ""]
    by_id = {node.id: node for node in dag.nodes}
    for node_id in dag.execution_order:
        node = by_id[node_id]
        deps = f" after {', '.join(node.dependencies)}" if node.dependencies else ""
        lines.append(f"- `{node.id}` ({node.role.value}{deps}): {node.objective}")
    return "\n".join(lines)


def _format_post_check_summary(shared_state: SharedState) -> str:
    decision = shared_state.repair_decision
    lines = ["### Multi-Agent Post-Checks", ""]
    lines.append(f"Changed files: {', '.join(shared_state.changed_files) or 'none'}")
    if decision is not None:
        lines.append(f"Repair needed: {'yes' if decision.retry else 'no'}")
        lines.append(f"Reason: {decision.reason}")
    return "\n".join(lines)


def _execution_prompt_with_plan(prompt_text: str, dag: TaskDAG) -> str:
    return (
        f"{prompt_text}\n\n"
        "[Nexus multi-agent supervisor plan]\n"
        f"{json.dumps(dag.to_dict(), indent=2)}\n"
        "Execute through the normal Nexus tool and approval flow. Keep the plan as guidance; "
        "do not bypass confirmation or provider-safe history rules."
    )
