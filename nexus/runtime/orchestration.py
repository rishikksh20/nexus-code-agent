from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from nexus.models import (
    AgentEvent,
    AgentEventType,
    ConfirmationKind,
    ConfirmationRequest,
    Message,
    RuntimeRequest,
    RuntimeResponse,
    StreamEventType,
    ToolExecutionContext,
    ToolResult,
)
from nexus.runtime.agent import Agent
from nexus.runtime.context_state import (
    AgentContextRecord,
    AgentSessionState,
    ContextPacket,
    ContextScope,
    MultiAgentSessionState,
    TaskContext,
    append_artifact_record,
    append_multi_agent_event,
    estimate_messages,
    load_multi_agent_state,
    make_artifact_record,
    make_context_packet,
    make_implementation_complete_packet,
    make_repair_request_packet,
    make_research_complete_packet,
    make_review_findings_packet,
    make_test_failure_packet,
    make_test_success_packet,
    record_agent_context,
    record_context_packet,
    render_context_packet,
    save_multi_agent_state,
    upsert_agent_state,
    upsert_task_context,
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
    PLANNING_ANALYSIS = "planning_analysis"
    RESEARCH = "research"
    EXECUTION = "execution"
    TEST = "test"
    REVIEW = "review"
    DOCS = "docs"


class TaskStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
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
    """Run one turn.

    Advanced mode exposes cognitive sub-agent tools to the supervisor model,
    but no longer auto-runs a separate DAG scheduler. The supervisor decides
    when to call sub-agent tools through the normal tool loop.
    """
    return await turn_runner(
        state,
        agent,
        prompt_text=prompt_text,
        ui=ui,
        approval_callback=approval_callback,
        auto_confirm=auto_confirm,
    )


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
            id="planning_analysis",
            role=AgentRole.PLANNING_ANALYSIS,
            objective=(
                "Map the relevant code paths, summarize existing patterns, "
                "identify risks, and outline the implementation approach."
            ),
            allowed_tools=("read_file", "glob", "grep", "list_dir", "lsp"),
        ),
        TaskNode(
            id="execute",
            role=AgentRole.EXECUTION,
            objective="Implement the requested change using the current Nexus runtime patterns.",
            dependencies=("planning_analysis",),
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
    git_status_artifact = _store_check_artifact(
        state,
        artifact_type="git_status",
        task_id="verify",
        producer_agent="test",
        output=git_status,
    )
    changed_files = tuple(_changed_files_from_status(git_status))
    verification_results: list[str] = []
    review_findings: list[str] = []
    artifact_ids: list[str] = []
    if git_status_artifact is not None:
        artifact_ids.append(git_status_artifact.artifact_id)
    findings = dict(prior_state.findings) if prior_state else {}

    # Keep v1 lightweight: syntax/type verification plus diff review. Full test
    # execution remains available through run_tests and specialist workers.
    typecheck = await _execute_optional_tool(state.tool_registry, "run_typecheck", {}, context)
    if typecheck:
        verification_results.append(typecheck)
        artifact = _store_check_artifact(
            state,
            artifact_type="typecheck_output",
            task_id="verify",
            producer_agent="test",
            output=typecheck,
        )
        if artifact is not None:
            artifact_ids.append(artifact.artifact_id)
    diff = await _execute_optional_tool(state.tool_registry, "git_diff", {"target": "working"}, context)
    if diff and diff != "(no diff)":
        diff_artifact = _store_check_artifact(
            state,
            artifact_type="diff",
            task_id="review",
            producer_agent="review",
            output=diff,
        )
        if diff_artifact is not None:
            artifact_ids.append(diff_artifact.artifact_id)
        review = await summarize_review_findings(state, agent, dag=dag, diff=diff, verification="\n".join(verification_results))
        review_findings.append(review)
        review_artifact = _store_check_artifact(
            state,
            artifact_type="review_report",
            task_id="review",
            producer_agent="review",
            output=review,
        )
        if review_artifact is not None:
            artifact_ids.append(review_artifact.artifact_id)
    elif diff:
        review_findings.append("No working-tree diff found for review.")

    repair_decision = decide_repair(
        verification_results=tuple(verification_results),
        review_findings=tuple(review_findings),
        max_iterations=int(getattr(state.config, "multi_agent_max_repair_iterations", 2)),
    )
    findings["post_execution"] = repair_decision.reason
    packet_factory = make_test_failure_packet if repair_decision.retry else make_test_success_packet
    feedback_packet = packet_factory(
        metadata=state.session.metadata,
        task_id="verify",
        summary=repair_decision.reason,
        modified_files=changed_files,
        failure_summary=repair_decision.reason if repair_decision.retry else None,
        artifact_ids=tuple(artifact_ids),
    )
    record_context_packet(state.session.metadata, feedback_packet)
    packets = [feedback_packet]
    if review_findings:
        review_packet = make_review_findings_packet(
            metadata=state.session.metadata,
            task_id="review",
            summary=review_findings[-1],
            modified_files=changed_files,
            artifact_ids=tuple(artifact_ids),
            confidence=0.7,
        )
        record_context_packet(state.session.metadata, review_packet)
        packets.append(review_packet)
    if repair_decision.retry:
        repair_packet = make_repair_request_packet(
            metadata=state.session.metadata,
            task_id="execute",
            summary="Focused repair requested after failed post-execution checks.",
            modified_files=changed_files,
            failure_summary=repair_decision.reason,
            artifact_ids=tuple(artifact_ids),
        )
        record_context_packet(state.session.metadata, repair_packet)
        packets.append(repair_packet)
        _increment_repair_iteration(state, task_id="execute", packet_id=repair_packet.packet_id)
        append_multi_agent_event(
            state.session.metadata,
            "REPAIR_REQUESTED",
            task_id="execute",
            packet_id=repair_packet.packet_id,
            summary=repair_decision.reason,
        )
    record_agent_context(
        state.session.metadata,
        AgentContextRecord(
            agent_id="test-review",
            role="test+review",
            scope=ContextScope.HANDOFF,
            summary=repair_decision.reason,
            token_estimate=feedback_packet.token_estimate,
            shared_inputs=("execution diff", "verification output"),
            handoff_outputs=tuple(packet.packet_id for packet in packets),
            allowed_tools=("git_status", "git_diff", "run_typecheck"),
        ),
    )
    _update_post_check_task_contexts(
        state,
        changed_files=changed_files,
        artifact_ids=tuple(artifact_ids),
        verification_failed=repair_decision.retry,
        packets=tuple(packet.packet_id for packet in packets),
    )
    return SharedState(
        dag=dag,
        repo_map=prior_state.repo_map if prior_state else (),
        findings=findings,
        changed_files=changed_files,
        verification_results=tuple(verification_results),
        review_findings=tuple(review_findings),
        repair_decision=repair_decision,
        context_packets=(*((prior_state.context_packets if prior_state else ())), *packets),
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


def _initialize_typed_state(
    state: ReplState,
    *,
    dag: TaskDAG,
    prompt_text: str,
    planner_packet: ContextPacket,
) -> None:
    metadata = state.session.metadata
    current = load_multi_agent_state(metadata)
    initialized = MultiAgentSessionState(
        schema_version=current.schema_version,
        session_id=getattr(state.session, "session_id", current.session_id),
        objective=prompt_text.strip() or dag.goal,
        dag=dag.to_dict(),
        tasks={},
        agents={},
        packets=[],
        artifacts={},
        events=[],
        latest_summary=None,
        counters=current.counters,
    )
    save_multi_agent_state(metadata, initialized)
    tasks: dict[str, TaskContext] = {}
    for node in dag.nodes:
        task = TaskContext(
            task_id=node.id,
            role=node.role.value,
            objective=node.objective,
            status=TaskStatus.BLOCKED.value if node.dependencies else TaskStatus.PENDING.value,
            dependencies=node.dependencies,
            assigned_agent_id=node.role.value,
            input_packet_ids=(planner_packet.packet_id,) if node.role is AgentRole.EXECUTION else (),
            related_files=node.claimed_resources,
        )
        tasks[node.id] = task
    save_multi_agent_state(
        metadata,
        MultiAgentSessionState(
            schema_version=initialized.schema_version,
            session_id=initialized.session_id,
            objective=initialized.objective,
            dag=initialized.dag,
            tasks=tasks,
            agents=initialized.agents,
            packets=initialized.packets,
            artifacts=initialized.artifacts,
            events=initialized.events,
            latest_summary=initialized.latest_summary,
            counters=load_multi_agent_state(metadata).counters,
        ),
    )
    for node in dag.nodes:
        append_multi_agent_event(
            metadata,
            "TASK_CREATED",
            task_id=node.id,
            agent_id=node.role.value,
            summary=node.objective,
        )
    for node in dag.nodes:
        append_multi_agent_event(
            metadata,
            "TASK_BLOCKED" if node.dependencies else "TASK_STARTED",
            task_id=node.id,
            agent_id=node.role.value,
            summary="Waiting on dependencies." if node.dependencies else "Ready to start.",
        )


def _record_execution_agent_state(
    state: ReplState,
    *,
    dag: TaskDAG,
    planner_packet: ContextPacket,
    events: list[AgentEvent],
) -> None:
    summary = _latest_assistant_summary(events) or "Execution turn completed through the normal turn runner."
    execution_task = next((node for node in dag.nodes if node.role is AgentRole.EXECUTION), None)
    task_id = execution_task.id if execution_task is not None else "execute"
    implementation_packet = make_implementation_complete_packet(
        metadata=state.session.metadata,
        task_id=task_id,
        summary=summary[:800],
        recommended_tests=("uv run pytest tests/test_orchestration.py",),
    )
    record_context_packet(state.session.metadata, implementation_packet)
    upsert_agent_state(
        state.session.metadata,
        AgentSessionState(
            agent_id="execution",
            role=AgentRole.EXECUTION.value,
            task_id=task_id,
            status=TaskStatus.COMPLETED.value,
            working_summary=summary[:800],
            token_estimate=estimate_messages(getattr(state, "history", [])),
            message_count=len(getattr(state, "history", [])),
            allowed_tools=tuple(record.name for record in state.tool_registry.records()),
            input_packet_ids=(planner_packet.packet_id,),
            output_packet_ids=(implementation_packet.packet_id,),
        ),
    )
    existing = load_multi_agent_state(state.session.metadata).tasks.get(task_id)
    if existing is not None:
        upsert_task_context(
            state.session.metadata,
            TaskContext(
                task_id=existing.task_id,
                role=existing.role,
                objective=existing.objective,
                status=TaskStatus.COMPLETED.value,
                dependencies=existing.dependencies,
                assigned_agent_id=existing.assigned_agent_id,
                input_packet_ids=existing.input_packet_ids,
                output_packet_ids=(*existing.output_packet_ids, implementation_packet.packet_id),
                artifact_ids=existing.artifact_ids,
                related_files=existing.related_files,
                modified_files=existing.modified_files,
                repair_iteration=existing.repair_iteration,
            ),
        )
        append_multi_agent_event(
            state.session.metadata,
            "TASK_COMPLETED",
            task_id=task_id,
            agent_id=AgentRole.EXECUTION.value,
            packet_id=implementation_packet.packet_id,
            summary=summary[:500],
        )


async def _run_research_nodes_if_enabled(
    state: ReplState,
    *,
    dag: TaskDAG,
    planner_packet: ContextPacket,
) -> tuple[ContextPacket, ...]:
    runtime = getattr(state, "delegation", None)
    if runtime is None:
        return ()
    research_nodes = [node for node in dag.nodes if node.role is AgentRole.PLANNING_ANALYSIS]
    if not research_nodes:
        return ()
    max_parallel = max(1, int(getattr(state.config, "multi_agent_max_parallel_tasks", 1)))
    packets: list[ContextPacket] = []
    for start in range(0, len(research_nodes), max_parallel):
        batch = research_nodes[start : start + max_parallel]
        results = await asyncio.gather(
            *(_run_one_research_node(state, node=node, dag=dag, planner_packet=planner_packet) for node in batch)
        )
        packets.extend(packet for packet in results if packet is not None)
    return tuple(packets)


async def _run_one_research_node(
    state: ReplState,
    *,
    node: TaskNode,
    dag: TaskDAG,
    planner_packet: ContextPacket,
) -> ContextPacket | None:
    from nexus.runtime.delegation import DelegationRequest, TaskStatus as DelegatedTaskStatus

    runtime = getattr(state, "delegation", None)
    if runtime is None:
        return None
    allowed_tools = node.allowed_tools or ("read_file", "glob", "grep", "list_dir", "lsp")
    request = DelegationRequest(
        title=f"Planning Analysis: {node.id}",
        instructions=(
            f"Goal: {dag.goal}\n\nPlanning and analysis task: {node.objective}\n"
            "Return compact findings, plan, risks, clarification needs, and related files only."
        ),
        allowed_tools=allowed_tools,
        claimed_resources=node.claimed_resources,
        shared_context=(render_context_packet(planner_packet),),
        input_packet_ids=(planner_packet.packet_id,),
    )
    task = await runtime.submit(request)
    completed = await runtime.wait_for_task(task.task_id, timeout=30.0)
    if completed is None:
        summary = "Planning analysis worker did not return a task record."
        status = TaskStatus.FAILED.value
    elif completed.status is DelegatedTaskStatus.COMPLETED:
        summary = completed.result_summary or "Planning analysis completed."
        status = TaskStatus.COMPLETED.value
    else:
        summary = completed.error or completed.result_summary or "Planning analysis failed."
        status = TaskStatus.FAILED.value
    packet = make_research_complete_packet(
        metadata=state.session.metadata,
        task_id=node.id,
        summary=summary[:1000],
        related_files=node.claimed_resources,
        confidence=0.7 if status == TaskStatus.COMPLETED.value else 0.2,
    )
    record_context_packet(state.session.metadata, packet)
    upsert_task_context(
        state.session.metadata,
        TaskContext(
            task_id=node.id,
            role=node.role.value,
            objective=node.objective,
            status=status,
            dependencies=node.dependencies,
            assigned_agent_id=getattr(completed, "assigned_worker", None) if completed is not None else None,
            input_packet_ids=(planner_packet.packet_id,),
            output_packet_ids=(packet.packet_id,),
            related_files=node.claimed_resources,
        ),
    )
    append_multi_agent_event(
        state.session.metadata,
        "TASK_COMPLETED" if status == TaskStatus.COMPLETED.value else "TASK_FAILED",
        task_id=node.id,
        agent_id=AgentRole.PLANNING_ANALYSIS.value,
        packet_id=packet.packet_id,
        summary=summary[:500],
    )
    return packet


async def _run_dag_nodes_with_delegation(
    state: ReplState,
    *,
    dag: TaskDAG,
    prompt_text: str,
    planner_packet: ContextPacket,
    initial_packets: tuple[ContextPacket, ...],
    ui: TerminalUI | None,
    approval_callback: ConfirmationCallback | None,
    auto_confirm: bool,
) -> list[AgentEvent] | None:
    runtime = getattr(state, "delegation", None)
    if runtime is None:
        return None

    packets_by_task: dict[str, ContextPacket] = {
        packet.task_id: packet for packet in initial_packets if packet.task_id
    }
    summaries: list[str] = []
    final_events: list[AgentEvent] = []
    by_id = {node.id: node for node in dag.nodes}
    for node_id in dag.execution_order:
        node = by_id[node_id]
        if node.id in packets_by_task:
            continue
        if not _dependencies_completed(node, packets_by_task):
            _mark_task_blocked(state, node, reason="Dependency failed or did not produce a handoff packet.")
            summaries.append(f"{node.id}: skipped because dependencies were not complete.")
            continue

        task_events = await _run_one_delegated_node(
            state,
            node=node,
            dag=dag,
            prompt_text=prompt_text,
            planner_packet=planner_packet,
            dependency_packets=tuple(packets_by_task[dep] for dep in node.dependencies if dep in packets_by_task),
            ui=ui,
            approval_callback=approval_callback,
            auto_confirm=auto_confirm,
        )
        if any(event.kind == AgentEventType.CONFIRMATION_REQUESTED for event in task_events.events):
            return task_events.events
        final_events.extend(task_events.events)
        summaries.append(f"{node.id}: {task_events.summary}")
        if task_events.packet is not None:
            packets_by_task[node.id] = task_events.packet
        if not task_events.success:
            for remaining_id in dag.execution_order[dag.execution_order.index(node_id) + 1 :]:
                remaining = by_id[remaining_id]
                if remaining.id not in packets_by_task:
                    _mark_task_blocked(state, remaining, reason=f"Blocked after '{node.id}' failed.")
            break

    summary = "\n".join(summaries) or "No delegated tasks were runnable."
    final_events.append(
        AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(message=Message(role="assistant", content=summary)),
        )
    )
    return final_events


@dataclass(slots=True, frozen=True)
class _DelegatedNodeResult:
    events: list[AgentEvent]
    packet: ContextPacket | None
    summary: str
    success: bool


async def _run_one_delegated_node(
    state: ReplState,
    *,
    node: TaskNode,
    dag: TaskDAG,
    prompt_text: str,
    planner_packet: ContextPacket,
    dependency_packets: tuple[ContextPacket, ...],
    ui: TerminalUI | None,
    approval_callback: ConfirmationCallback | None,
    auto_confirm: bool,
) -> _DelegatedNodeResult:
    from nexus.runtime.delegation import DelegationRequest, TaskStatus as DelegatedTaskStatus

    runtime = getattr(state, "delegation", None)
    if runtime is None:
        return _DelegatedNodeResult([], None, "Delegation runtime is not available.", False)

    input_packets = (planner_packet, *dependency_packets)
    request = DelegationRequest(
        title=f"{node.role.value.title()}: {node.id}",
        instructions=_delegated_node_instructions(
            node,
            dag=dag,
            prompt_text=prompt_text,
            dependency_packets=dependency_packets,
        ),
        allowed_tools=_allowed_tools_for_node(node, state.tool_registry),
        claimed_resources=node.claimed_resources,
        shared_context=tuple(render_context_packet(packet) for packet in input_packets),
        input_packet_ids=tuple(packet.packet_id for packet in input_packets),
    )
    task = await runtime.submit(request)
    tool_name = f"subagent_{node.role.value}"
    _render_delegated_tool_start(ui, state, tool_name=tool_name, call_id=task.task_id, request=request)
    upsert_task_context(
        state.session.metadata,
        TaskContext(
            task_id=node.id,
            role=node.role.value,
            objective=node.objective,
            status=TaskStatus.RUNNING.value,
            dependencies=node.dependencies,
            assigned_agent_id=task.assigned_worker,
            input_packet_ids=request.input_packet_ids,
            related_files=node.claimed_resources,
        ),
    )
    append_multi_agent_event(
        state.session.metadata,
        "TASK_STARTED",
        task_id=node.id,
        agent_id=task.assigned_worker or node.role.value,
        summary="Delegated task to worker agent.",
    )

    completed, pending_event = await _wait_for_delegated_task_with_approvals(
        state,
        task_id=task.task_id,
        ui=ui,
        approval_callback=approval_callback,
        auto_confirm=auto_confirm,
    )
    if pending_event is not None:
        return _DelegatedNodeResult([pending_event], None, "Waiting for approval.", False)

    if completed is None:
        summary = "Worker did not return before timeout."
        success = False
    elif completed.status is DelegatedTaskStatus.COMPLETED:
        summary = completed.result_summary or "Worker completed."
        success = True
    else:
        summary = completed.error or completed.result_summary or "Worker failed."
        success = False
    if (
        success
        and _node_requires_tool_work(node)
        and not _worker_used_tools(completed)
        and str(getattr(state.config, "provider", "fake")) != "fake"
    ):
        summary = (
            "Worker completed without using any tools, so no files were inspected or changed. "
            "Treating this task as failed instead of silently accepting a no-op."
        )
        success = False

    packet = _record_delegated_node_result(
        state,
        node=node,
        summary=summary,
        success=success,
        assigned_agent_id=getattr(completed, "assigned_worker", task.assigned_worker)
        if completed is not None
        else task.assigned_worker,
        input_packet_ids=request.input_packet_ids,
    )
    _render_delegated_tool_complete(
        ui,
        state,
        tool_name=tool_name,
        call_id=task.task_id,
        summary=summary,
        success=success,
        node=node,
    )
    return _DelegatedNodeResult([], packet if success else None, summary, success)


def _dependencies_completed(node: TaskNode, packets_by_task: dict[str, ContextPacket]) -> bool:
    return all(dep in packets_by_task for dep in node.dependencies)


def _all_dag_tasks_completed(state: ReplState, dag: TaskDAG) -> bool:
    tasks = load_multi_agent_state(state.session.metadata).tasks
    for node in dag.nodes:
        task = tasks.get(node.id)
        if task is None or task.status != TaskStatus.COMPLETED.value:
            return False
    return True


def _mark_task_blocked(state: ReplState, node: TaskNode, *, reason: str) -> None:
    upsert_task_context(
        state.session.metadata,
        TaskContext(
            task_id=node.id,
            role=node.role.value,
            objective=node.objective,
            status=TaskStatus.BLOCKED.value,
            dependencies=node.dependencies,
            assigned_agent_id=node.role.value,
            related_files=node.claimed_resources,
        ),
    )
    append_multi_agent_event(
        state.session.metadata,
        "TASK_BLOCKED",
        task_id=node.id,
        agent_id=node.role.value,
        summary=reason,
    )


def _delegated_node_instructions(
    node: TaskNode,
    *,
    dag: TaskDAG,
    prompt_text: str,
    dependency_packets: tuple[ContextPacket, ...],
) -> str:
    dependency_text = "\n\n".join(render_context_packet(packet) for packet in dependency_packets)
    if not dependency_text:
        dependency_text = "(no completed dependency handoffs)"
    return (
        f"User request:\n{prompt_text}\n\n"
        f"Overall goal:\n{dag.goal}\n\n"
        f"Task id: {node.id}\n"
        f"Task role: {node.role.value}\n"
        f"Task objective:\n{node.objective}\n\n"
        f"Completed dependency handoffs:\n{dependency_text}\n\n"
        "Execute this task now. Use tools for any repository inspection, file edits, tests, or docs updates. "
        "Do not merely restate the task. Return a concise summary with changed files, tests run, and blockers."
    )


def _allowed_tools_for_node(node: TaskNode, registry: ToolRegistry) -> tuple[str, ...]:
    available = {record.name for record in registry.records()}
    requested = tuple(tool for tool in node.allowed_tools if tool in available)
    if requested:
        return requested
    if node.role is AgentRole.PLANNING_ANALYSIS:
        preferred = ("read_file", "glob", "grep", "list_dir", "lsp", "git_status")
    elif node.role is AgentRole.TEST:
        preferred = ("read_file", "write_file", "edit", "apply_patch", "bash", "run_tests", "run_linter", "run_typecheck", "git_status")
    elif node.role is AgentRole.REVIEW:
        preferred = ("git_diff", "read_file", "grep", "lsp", "bash")
    else:
        preferred = _default_execution_worker_tools(registry)
    return tuple(tool for tool in preferred if tool in available) or tuple(available)


def _node_requires_tool_work(node: TaskNode) -> bool:
    if node.role in {AgentRole.EXECUTION, AgentRole.TEST, AgentRole.DOCS}:
        return True
    objective = node.objective.lower()
    return any(marker in objective for marker in ("create", "write", "implement", "refactor", "update", "add"))


def _worker_used_tools(completed: Any | None) -> bool:
    snapshot = getattr(completed, "context_snapshot", None)
    if not isinstance(snapshot, dict):
        return False
    return int(snapshot.get("tool_call_count") or 0) > 0


def _record_delegated_node_result(
    state: ReplState,
    *,
    node: TaskNode,
    summary: str,
    success: bool,
    assigned_agent_id: str | None,
    input_packet_ids: tuple[str, ...],
) -> ContextPacket:
    if node.role is AgentRole.PLANNING_ANALYSIS:
        packet = make_research_complete_packet(
            metadata=state.session.metadata,
            task_id=node.id,
            summary=summary[:1000],
            related_files=node.claimed_resources,
            confidence=0.7 if success else 0.2,
        )
    elif node.role is AgentRole.REVIEW:
        packet = make_review_findings_packet(
            metadata=state.session.metadata,
            task_id=node.id,
            summary=summary[:1000],
            modified_files=(),
            confidence=0.7 if success else 0.2,
        )
    elif node.role is AgentRole.TEST:
        factory = make_test_success_packet if success else make_test_failure_packet
        packet = factory(
            metadata=state.session.metadata,
            task_id=node.id,
            summary=summary[:1000],
            failure_summary=None if success else summary[:1000],
        )
    else:
        packet = make_implementation_complete_packet(
            metadata=state.session.metadata,
            task_id=node.id,
            summary=summary[:1000],
        )
    record_context_packet(state.session.metadata, packet)
    upsert_task_context(
        state.session.metadata,
        TaskContext(
            task_id=node.id,
            role=node.role.value,
            objective=node.objective,
            status=TaskStatus.COMPLETED.value if success else TaskStatus.FAILED.value,
            dependencies=node.dependencies,
            assigned_agent_id=assigned_agent_id,
            input_packet_ids=input_packet_ids,
            output_packet_ids=(packet.packet_id,),
            related_files=node.claimed_resources,
        ),
    )
    upsert_agent_state(
        state.session.metadata,
        AgentSessionState(
            agent_id=node.role.value if node.role is not AgentRole.EXECUTION else f"execution:{node.id}",
            role=node.role.value,
            task_id=node.id,
            status=TaskStatus.COMPLETED.value if success else TaskStatus.FAILED.value,
            working_summary=summary[:800],
            token_estimate=packet.token_estimate,
            message_count=0,
            input_packet_ids=input_packet_ids,
            output_packet_ids=(packet.packet_id,),
        ),
    )
    append_multi_agent_event(
        state.session.metadata,
        "TASK_COMPLETED" if success else "TASK_FAILED",
        task_id=node.id,
        agent_id=assigned_agent_id or node.role.value,
        packet_id=packet.packet_id,
        summary=summary[:500],
    )
    return packet


def _render_delegated_tool_start(
    ui: TerminalUI | None,
    state: ReplState,
    *,
    tool_name: str,
    call_id: str,
    request: Any,
) -> None:
    if ui is None:
        return
    ui.render_event(
        AgentEvent.tool_call_start(
            call_id,
            tool_name,
            {
                "title": request.title,
                "instructions": request.instructions,
                "allowed_tools": list(request.allowed_tools),
                "input_packet_ids": list(request.input_packet_ids),
            },
        ),
        stream_output=state.config.stream_output,
        show_tool_calls=state.config.show_tool_calls,
    )


def _render_delegated_tool_complete(
    ui: TerminalUI | None,
    state: ReplState,
    *,
    tool_name: str,
    call_id: str,
    summary: str,
    success: bool,
    node: TaskNode,
) -> None:
    if ui is None:
        return
    ui.render_event(
        AgentEvent.tool_call_complete(
            ToolResult(
                call_id=call_id,
                tool_name=tool_name,
                output=summary,
                is_error=not success,
                metadata={"task_id": node.id, "role": node.role.value, "status": "completed" if success else "failed"},
            )
        ),
        stream_output=state.config.stream_output,
        show_tool_calls=state.config.show_tool_calls,
    )


async def _run_execution_node_if_enabled(
    state: ReplState,
    *,
    dag: TaskDAG,
    prompt_text: str,
    planner_packet: ContextPacket,
    handoff_packets: tuple[ContextPacket, ...],
    approval_callback: ConfirmationCallback | None,
    auto_confirm: bool,
) -> list[AgentEvent] | None:
    from nexus.runtime.delegation import DelegationRequest, TaskStatus as DelegatedTaskStatus

    runtime = getattr(state, "delegation", None)
    if runtime is None:
        return None
    execution_node = next((node for node in dag.nodes if node.role is AgentRole.EXECUTION), None)
    if execution_node is None:
        return None

    shared_context = tuple(render_context_packet(packet) for packet in (planner_packet, *handoff_packets))
    allowed_tools = execution_node.allowed_tools or _default_execution_worker_tools(state.tool_registry)
    request = DelegationRequest(
        title=f"Execute: {execution_node.id}",
        instructions=(
            f"User request:\n{prompt_text}\n\n"
            f"Overall goal:\n{dag.goal}\n\n"
            f"Execution task:\n{execution_node.objective}\n\n"
            "Implement the requested change in the workspace. Use existing project patterns, keep the "
            "change focused, and run relevant verification when practical. Return a concise summary "
            "including changed files and any tests run."
        ),
        allowed_tools=allowed_tools,
        claimed_resources=execution_node.claimed_resources,
        shared_context=shared_context,
        input_packet_ids=tuple(packet.packet_id for packet in (planner_packet, *handoff_packets)),
    )
    task = await runtime.submit(request)
    upsert_task_context(
        state.session.metadata,
        TaskContext(
            task_id=execution_node.id,
            role=execution_node.role.value,
            objective=execution_node.objective,
            status=TaskStatus.RUNNING.value,
            dependencies=execution_node.dependencies,
            assigned_agent_id=task.assigned_worker,
            input_packet_ids=request.input_packet_ids,
            related_files=execution_node.claimed_resources,
        ),
    )
    append_multi_agent_event(
        state.session.metadata,
        "TASK_STARTED",
        task_id=execution_node.id,
        agent_id=task.assigned_worker or AgentRole.EXECUTION.value,
        summary="Delegated execution task to worker agent.",
    )

    completed, pending_event = await _wait_for_delegated_task_with_approvals(
        state,
        task_id=task.task_id,
        approval_callback=approval_callback,
        auto_confirm=auto_confirm,
    )
    if pending_event is not None:
        return [pending_event]
    if completed is None:
        summary = "Execution worker did not return before timeout."
        status = TaskStatus.FAILED.value
    elif completed.status is DelegatedTaskStatus.COMPLETED:
        summary = completed.result_summary or "Execution worker completed."
        status = TaskStatus.COMPLETED.value
    else:
        summary = completed.error or completed.result_summary or "Execution worker failed."
        status = TaskStatus.FAILED.value

    upsert_task_context(
        state.session.metadata,
        TaskContext(
            task_id=execution_node.id,
            role=execution_node.role.value,
            objective=execution_node.objective,
            status=status,
            dependencies=execution_node.dependencies,
            assigned_agent_id=(
                getattr(completed, "assigned_worker", task.assigned_worker)
                if completed is not None
                else task.assigned_worker
            ),
            input_packet_ids=request.input_packet_ids,
            related_files=execution_node.claimed_resources,
        ),
    )
    append_multi_agent_event(
        state.session.metadata,
        "TASK_COMPLETED" if status == TaskStatus.COMPLETED.value else "TASK_FAILED",
        task_id=execution_node.id,
        agent_id=(
            getattr(completed, "assigned_worker", task.assigned_worker)
            if completed is not None
            else task.assigned_worker
        ),
        summary=summary[:500],
    )
    return [
        AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(message=Message(role="assistant", content=summary)),
        )
    ]


async def _wait_for_delegated_task_with_approvals(
    state: ReplState,
    *,
    task_id: str,
    ui: TerminalUI | None = None,
    approval_callback: ConfirmationCallback | None,
    auto_confirm: bool,
) -> tuple[Any | None, AgentEvent | None]:
    runtime = getattr(state, "delegation", None)
    if runtime is None:
        return None, None
    timeout = float(getattr(state.config, "delegation_execution_timeout_seconds", 600.0))
    deadline = asyncio.get_event_loop().time() + timeout
    handled_permissions: set[str] = set()
    rendered_note_count = 0
    while asyncio.get_event_loop().time() <= deadline:
        task = runtime.tasks.get(task_id)
        if task is None or task.status.is_terminal:
            _render_new_worker_notes(ui, task, rendered_note_count)
            return task, None
        rendered_note_count = _render_new_worker_notes(ui, task, rendered_note_count)

        for pending in runtime.list_pending_permissions():
            if pending.task_id != task_id:
                continue
            decision_id = pending.correlation_id or pending.message_id
            if decision_id in handled_permissions:
                continue
            request = _confirmation_request_from_worker_permission(pending)
            if auto_confirm:
                handled_permissions.add(decision_id)
                await runtime.decide_permission(decision_id, approved=True)
                continue
            if approval_callback is None:
                _render_confirmation_event(ui, state, request)
                return task, AgentEvent(
                    kind=AgentEventType.CONFIRMATION_REQUESTED,
                    payload=request,
                )
            _render_confirmation_event(ui, state, request)
            response = await approval_callback(request)
            handled_permissions.add(decision_id)
            await runtime.decide_permission(decision_id, approved=response.approved)

        await asyncio.sleep(float(getattr(runtime, "poll_interval", 0.05)))
    return runtime.tasks.get(task_id), None


def _render_new_worker_notes(ui: TerminalUI | None, task: Any | None, rendered_count: int) -> int:
    if ui is None or task is None:
        return rendered_count
    notes = list(getattr(task, "notes", []) or [])
    for note in notes[rendered_count:]:
        ui.print_muted(f"{getattr(task, 'assigned_worker', 'worker')} · {getattr(task, 'title', 'task')}: {note}")
    return len(notes)


def _render_confirmation_event(ui: TerminalUI | None, state: ReplState, request: ConfirmationRequest) -> None:
    if ui is None:
        return
    ui.render_event(
        AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=request),
        stream_output=state.config.stream_output,
        show_tool_calls=state.config.show_tool_calls,
    )


def _confirmation_request_from_worker_permission(message) -> ConfirmationRequest:
    action = str(message.payload.get("action", "worker action")).strip()
    reason = str(message.payload.get("reason", "Worker requested coordinator approval.")).strip()
    return ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name=action,
        prompt=f"Worker requests approval to use '{action}'.",
        reason=reason,
        payload={
            "worker_id": message.sender,
            "task_id": message.task_id,
            "decision_id": message.correlation_id or message.message_id,
            "approval_policy": "on-request",
        },
        call_id=message.correlation_id or message.message_id,
        arguments={"worker_id": message.sender, "task_id": message.task_id, "action": action},
    )


def _default_execution_worker_tools(registry: ToolRegistry) -> tuple[str, ...]:
    preferred = (
        "read_file",
        "write_file",
        "edit",
        "insert_edit_into_file",
        "apply_patch",
        "glob",
        "grep",
        "list_dir",
        "lsp",
        "git_status",
        "git_diff",
        "run_tests",
        "run_linter",
        "run_typecheck",
        "run_formatter",
        "bash",
        "todos",
        "memory",
    )
    available = {record.name for record in registry.records()}
    selected = tuple(tool_name for tool_name in preferred if tool_name in available)
    return selected or tuple(record.name for record in registry.records())


def _latest_assistant_summary(events: list[AgentEvent]) -> str:
    for event in reversed(events):
        if event.kind == "model_response":
            message = getattr(event.payload, "message", None)
            content = getattr(message, "content", "") if message is not None else ""
            if content:
                return str(content)
    return ""


def _store_check_artifact(
    state: ReplState,
    *,
    artifact_type: str,
    task_id: str,
    producer_agent: str,
    output: str,
):
    if not output:
        return None
    summary = output.splitlines()[0][:240] if output.splitlines() else output[:240]
    artifact = make_artifact_record(
        metadata=state.session.metadata,
        artifact_type=artifact_type,
        task_id=task_id,
        producer_agent=producer_agent,
        summary=summary,
        content=output,
    )
    append_artifact_record(state.session.metadata, artifact)
    append_multi_agent_event(
        state.session.metadata,
        "ARTIFACT_CREATED",
        task_id=task_id,
        agent_id=producer_agent,
        artifact_id=artifact.artifact_id,
        summary=summary,
    )
    return artifact


def _update_post_check_task_contexts(
    state: ReplState,
    *,
    changed_files: tuple[str, ...],
    artifact_ids: tuple[str, ...],
    verification_failed: bool,
    packets: tuple[str, ...],
) -> None:
    typed = load_multi_agent_state(state.session.metadata)
    verify = typed.tasks.get("verify")
    if verify is not None:
        upsert_task_context(
            state.session.metadata,
            TaskContext(
                task_id=verify.task_id,
                role=verify.role,
                objective=verify.objective,
                status=TaskStatus.FAILED.value if verification_failed else TaskStatus.COMPLETED.value,
                dependencies=verify.dependencies,
                assigned_agent_id=verify.assigned_agent_id,
                input_packet_ids=verify.input_packet_ids,
                output_packet_ids=packets,
                artifact_ids=artifact_ids,
                related_files=verify.related_files,
                modified_files=changed_files,
                repair_iteration=verify.repair_iteration,
            ),
        )
    review = load_multi_agent_state(state.session.metadata).tasks.get("review")
    if review is not None:
        upsert_task_context(
            state.session.metadata,
            TaskContext(
                task_id=review.task_id,
                role=review.role,
                objective=review.objective,
                status=TaskStatus.COMPLETED.value,
                dependencies=review.dependencies,
                assigned_agent_id=review.assigned_agent_id,
                input_packet_ids=review.input_packet_ids,
                output_packet_ids=packets,
                artifact_ids=artifact_ids,
                related_files=review.related_files,
                modified_files=changed_files,
                repair_iteration=review.repair_iteration,
            ),
        )
    for task_id, event_type in (
        ("verify", "TEST_FAILED" if verification_failed else "TEST_PASSED"),
        ("review", "REVIEW_COMPLETED"),
    ):
        append_multi_agent_event(
            state.session.metadata,
            event_type,
            task_id=task_id,
            summary="Post-execution check completed.",
        )


def _increment_repair_iteration(state: ReplState, *, task_id: str, packet_id: str) -> None:
    typed = load_multi_agent_state(state.session.metadata)
    task = typed.tasks.get(task_id)
    if task is None:
        task = TaskContext(
            task_id=task_id,
            role=AgentRole.EXECUTION.value,
            objective="Repair execution task.",
            status=TaskStatus.PENDING.value,
        )
    upsert_task_context(
        state.session.metadata,
        TaskContext(
            task_id=task.task_id,
            role=task.role,
            objective=task.objective,
            status=TaskStatus.PENDING.value,
            dependencies=task.dependencies,
            assigned_agent_id=task.assigned_agent_id,
            input_packet_ids=(*task.input_packet_ids, packet_id),
            output_packet_ids=task.output_packet_ids,
            artifact_ids=task.artifact_ids,
            related_files=task.related_files,
            modified_files=task.modified_files,
            repair_iteration=task.repair_iteration + 1,
        ),
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
        return ""
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
        "You are the Nexus planning and analysis agent. Produce only JSON and do not call tools.\n"
        "Return a task DAG with this shape: "
        '{"goal": "...", "tasks": [{"id": "planning_analysis", "agent": "planning_analysis", '
        '"objective": "...", "depends_on": [], "allowed_tools": []}], '
        '"execution_order": ["planning_analysis"]}.\n'
        f"Use at most {max_steps} tasks. Valid agents: planning_analysis, execution, test, review, docs. "
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
        "planner": "planning_analysis",
        "planning": "planning_analysis",
        "analysis": "planning_analysis",
        "research": "planning_analysis",
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
    typed_state = load_multi_agent_state(state.session.metadata)
    if shared_state.dag is not None:
        typed_state = MultiAgentSessionState(
            schema_version=typed_state.schema_version,
            session_id=getattr(state.session, "session_id", typed_state.session_id),
            objective=shared_state.dag.goal,
            dag=shared_state.dag.to_dict(),
            tasks=typed_state.tasks,
            agents=typed_state.agents,
            packets=typed_state.packets,
            artifacts=typed_state.artifacts,
            events=typed_state.events,
            latest_summary=typed_state.latest_summary,
            counters=typed_state.counters,
        )
    save_multi_agent_state(
        state.session.metadata,
        typed_state,
        mode=getattr(state.config, "agent_mode", "basic"),
        complexity=complexity.value,
        shared_state=shared_state.to_dict(),
    )


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
    lines = ["### Legacy Coordination Plan", "", f"Goal: {dag.goal}", ""]
    by_id = {node.id: node for node in dag.nodes}
    for node_id in dag.execution_order:
        node = by_id[node_id]
        deps = f" after {', '.join(node.dependencies)}" if node.dependencies else ""
        lines.append(f"- `{node.id}` ({node.role.value}{deps}): {node.objective}")
    return "\n".join(lines)


def _print_dag_for_display(ui: TerminalUI, dag: TaskDAG) -> None:
    table = ui.make_table("Legacy Coordination Plan", "Task", "Role", "Depends On", "Objective")
    by_id = {node.id: node for node in dag.nodes}
    for node_id in dag.execution_order:
        node = by_id[node_id]
        table.add_row(
            node.id,
            node.role.value,
            ", ".join(node.dependencies) or "-",
            node.objective,
        )
    ui.print(table)


def _format_post_check_summary(shared_state: SharedState) -> str:
    decision = shared_state.repair_decision
    lines = ["### Multi-Agent Post-Checks", ""]
    lines.append(f"Changed files: {', '.join(shared_state.changed_files) or 'none'}")
    if decision is not None:
        lines.append(f"Repair needed: {'yes' if decision.retry else 'no'}")
        lines.append(f"Reason: {decision.reason}")
    return "\n".join(lines)


def _execution_prompt_with_plan(
    prompt_text: str,
    dag: TaskDAG,
    *,
    handoff_packets: tuple[ContextPacket, ...] = (),
) -> str:
    handoff_text = ""
    if handoff_packets:
        handoff_text = "\n\n[Nexus structured handoff packets]\n" + "\n".join(
            f"- {packet.packet_id} ({packet.packet_type}): {packet.summary[:800]}"
            for packet in handoff_packets
        )
    return (
        f"{prompt_text}\n\n"
        "[Nexus multi-agent supervisor plan]\n"
        f"{json.dumps(dag.to_dict(), indent=2)}\n"
        f"{handoff_text}\n"
        "Execute through the normal Nexus tool and approval flow. Keep the plan as guidance; "
        "do not bypass confirmation or provider-safe history rules."
    )
