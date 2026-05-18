from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from nexus.context import TokenEstimator


_METADATA_KEY = "multi_agent_context"
_MULTI_AGENT_KEY = "multi_agent"
_SCHEMA_VERSION = 1
_PACKET_LIMIT = 50
_ARTIFACT_LIMIT = 50
_EVENT_LIMIT = 200
_ARTIFACT_CONTENT_LIMIT = 12_000


class ContextScope(str, Enum):
    ISOLATED = "isolated"
    SHARED = "shared"
    HANDOFF = "handoff"


@dataclass(slots=True, frozen=True)
class ContextPacket:
    packet_id: str
    source_agent: str
    target_agent: str
    summary: str
    schema_version: int = _SCHEMA_VERSION
    packet_type: str = "handoff"
    task_id: str | None = None
    related_files: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()
    behavior_changes: tuple[str, ...] = ()
    recommended_tests: tuple[str, ...] = ()
    failure_summary: str | None = None
    artifact_ids: tuple[str, ...] = ()
    confidence: float | None = None
    artifacts: tuple[str, ...] = ()
    token_estimate: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "schema_version": self.schema_version,
            "packet_type": self.packet_type,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "task_id": self.task_id,
            "summary": self.summary,
            "related_files": list(self.related_files),
            "modified_files": list(self.modified_files),
            "behavior_changes": list(self.behavior_changes),
            "recommended_tests": list(self.recommended_tests),
            "failure_summary": self.failure_summary,
            "artifact_ids": list(self.artifact_ids),
            "confidence": self.confidence,
            "artifacts": list(self.artifacts),
            "token_estimate": self.token_estimate,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ContextPacket":
        return cls(
            packet_id=str(payload.get("packet_id") or uuid4().hex[:12]),
            schema_version=int(payload.get("schema_version") or _SCHEMA_VERSION),
            packet_type=str(payload.get("packet_type") or "handoff"),
            source_agent=str(payload.get("source_agent") or ""),
            target_agent=str(payload.get("target_agent") or ""),
            task_id=_optional_str(payload.get("task_id")),
            summary=str(payload.get("summary") or ""),
            related_files=_tuple_of_str(payload.get("related_files")),
            modified_files=_tuple_of_str(payload.get("modified_files")),
            behavior_changes=_tuple_of_str(payload.get("behavior_changes")),
            recommended_tests=_tuple_of_str(payload.get("recommended_tests")),
            failure_summary=_optional_str(payload.get("failure_summary")),
            artifact_ids=_tuple_of_str(payload.get("artifact_ids")),
            confidence=_optional_float(payload.get("confidence")),
            artifacts=_tuple_of_str(payload.get("artifacts")),
            token_estimate=int(payload.get("token_estimate") or 0),
            created_at=str(payload.get("created_at") or datetime.now(UTC).isoformat()),
        )


@dataclass(slots=True, frozen=True)
class AgentContextRecord:
    agent_id: str
    role: str
    scope: ContextScope
    summary: str
    token_estimate: int
    message_count: int = 0
    shared_inputs: tuple[str, ...] = ()
    handoff_outputs: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    tool_call_count: int = 0
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "scope": self.scope.value,
            "summary": self.summary,
            "token_estimate": self.token_estimate,
            "message_count": self.message_count,
            "shared_inputs": list(self.shared_inputs),
            "handoff_outputs": list(self.handoff_outputs),
            "allowed_tools": list(self.allowed_tools),
            "tool_call_count": self.tool_call_count,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True, frozen=True)
class TaskContext:
    task_id: str
    role: str
    objective: str
    status: str = "pending"
    dependencies: tuple[str, ...] = ()
    assigned_agent_id: str | None = None
    input_packet_ids: tuple[str, ...] = ()
    output_packet_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    related_files: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "objective": self.objective,
            "status": self.status,
            "dependencies": list(self.dependencies),
            "assigned_agent_id": self.assigned_agent_id,
            "input_packet_ids": list(self.input_packet_ids),
            "output_packet_ids": list(self.output_packet_ids),
            "artifact_ids": list(self.artifact_ids),
            "related_files": list(self.related_files),
            "modified_files": list(self.modified_files),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TaskContext":
        return cls(
            task_id=str(payload.get("task_id") or payload.get("id") or ""),
            role=str(payload.get("role") or "execution"),
            objective=str(payload.get("objective") or ""),
            status=str(payload.get("status") or "pending"),
            dependencies=_tuple_of_str(payload.get("dependencies") or payload.get("depends_on")),
            assigned_agent_id=_optional_str(payload.get("assigned_agent_id") or payload.get("assigned_agent")),
            input_packet_ids=_tuple_of_str(payload.get("input_packet_ids") or payload.get("input_packets")),
            output_packet_ids=_tuple_of_str(payload.get("output_packet_ids") or payload.get("output_packets")),
            artifact_ids=_tuple_of_str(payload.get("artifact_ids")),
            related_files=_tuple_of_str(payload.get("related_files")),
            modified_files=_tuple_of_str(payload.get("modified_files")),
            updated_at=str(payload.get("updated_at") or datetime.now(UTC).isoformat()),
        )


@dataclass(slots=True, frozen=True)
class AgentSessionState:
    agent_id: str
    role: str
    task_id: str
    status: str
    working_summary: str
    token_estimate: int = 0
    message_count: int = 0
    allowed_tools: tuple[str, ...] = ()
    input_packet_ids: tuple[str, ...] = ()
    output_packet_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    tool_call_count: int = 0
    last_error: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "task_id": self.task_id,
            "status": self.status,
            "working_summary": self.working_summary,
            "token_estimate": self.token_estimate,
            "message_count": self.message_count,
            "allowed_tools": list(self.allowed_tools),
            "input_packet_ids": list(self.input_packet_ids),
            "output_packet_ids": list(self.output_packet_ids),
            "artifact_ids": list(self.artifact_ids),
            "tool_call_count": self.tool_call_count,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentSessionState":
        summary = str(payload.get("working_summary") or payload.get("summary") or "")
        raw_input_packet_ids = payload.get("input_packet_ids")
        if raw_input_packet_ids is None:
            raw_input_packet_ids = _packet_ids_only(payload.get("shared_inputs"))
        return cls(
            agent_id=str(payload.get("agent_id") or ""),
            role=str(payload.get("role") or "worker"),
            task_id=str(payload.get("task_id") or ""),
            status=str(payload.get("status") or "unknown"),
            working_summary=summary,
            token_estimate=int(payload.get("token_estimate") or 0),
            message_count=int(payload.get("message_count") or 0),
            allowed_tools=_tuple_of_str(payload.get("allowed_tools")),
            input_packet_ids=_tuple_of_str(raw_input_packet_ids),
            output_packet_ids=_tuple_of_str(payload.get("output_packet_ids") or payload.get("handoff_outputs")),
            artifact_ids=_tuple_of_str(payload.get("artifact_ids")),
            tool_call_count=int(payload.get("tool_call_count") or 0),
            last_error=_optional_str(payload.get("last_error")),
            updated_at=str(payload.get("updated_at") or datetime.now(UTC).isoformat()),
        )

    def to_context_record(self) -> AgentContextRecord:
        return AgentContextRecord(
            agent_id=self.agent_id,
            role=self.role,
            scope=ContextScope.ISOLATED if self.role == "worker" else ContextScope.HANDOFF,
            summary=self.working_summary,
            token_estimate=self.token_estimate,
            message_count=self.message_count,
            shared_inputs=self.input_packet_ids,
            handoff_outputs=self.output_packet_ids,
            allowed_tools=self.allowed_tools,
            tool_call_count=self.tool_call_count,
        )


@dataclass(slots=True, frozen=True)
class ArtifactRecord:
    artifact_id: str
    artifact_type: str
    task_id: str | None
    producer_agent: str
    summary: str
    content: str = ""
    path: str | None = None
    token_estimate: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "task_id": self.task_id,
            "producer_agent": self.producer_agent,
            "summary": self.summary,
            "content": self.content,
            "path": self.path,
            "token_estimate": self.token_estimate,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ArtifactRecord":
        return cls(
            artifact_id=str(payload.get("artifact_id") or uuid4().hex[:12]),
            artifact_type=str(payload.get("artifact_type") or "generic"),
            task_id=_optional_str(payload.get("task_id")),
            producer_agent=str(payload.get("producer_agent") or ""),
            summary=str(payload.get("summary") or ""),
            content=str(payload.get("content") or "")[:_ARTIFACT_CONTENT_LIMIT],
            path=_optional_str(payload.get("path")),
            token_estimate=int(payload.get("token_estimate") or 0),
            created_at=str(payload.get("created_at") or datetime.now(UTC).isoformat()),
        )


@dataclass(slots=True, frozen=True)
class MultiAgentEvent:
    event_id: str
    event_type: str
    summary: str = ""
    task_id: str | None = None
    agent_id: str | None = None
    packet_id: str | None = None
    artifact_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "summary": self.summary,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "packet_id": self.packet_id,
            "artifact_id": self.artifact_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MultiAgentEvent":
        return cls(
            event_id=str(payload.get("event_id") or uuid4().hex[:12]),
            event_type=str(payload.get("event_type") or "UNKNOWN"),
            summary=str(payload.get("summary") or ""),
            task_id=_optional_str(payload.get("task_id")),
            agent_id=_optional_str(payload.get("agent_id")),
            packet_id=_optional_str(payload.get("packet_id")),
            artifact_id=_optional_str(payload.get("artifact_id")),
            created_at=str(payload.get("created_at") or datetime.now(UTC).isoformat()),
        )


@dataclass(slots=True, frozen=True)
class RollingSummary:
    summary_version: int
    objective: str
    important_decisions: tuple[str, ...] = ()
    implemented_features: tuple[str, ...] = ()
    known_failures: tuple[str, ...] = ()
    pending_work: tuple[str, ...] = ()
    active_constraints: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_version": self.summary_version,
            "objective": self.objective,
            "important_decisions": list(self.important_decisions),
            "implemented_features": list(self.implemented_features),
            "known_failures": list(self.known_failures),
            "pending_work": list(self.pending_work),
            "active_constraints": list(self.active_constraints),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RollingSummary":
        return cls(
            summary_version=int(payload.get("summary_version") or 1),
            objective=str(payload.get("objective") or ""),
            important_decisions=_tuple_of_str(payload.get("important_decisions")),
            implemented_features=_tuple_of_str(payload.get("implemented_features")),
            known_failures=_tuple_of_str(payload.get("known_failures")),
            pending_work=_tuple_of_str(payload.get("pending_work")),
            active_constraints=_tuple_of_str(payload.get("active_constraints")),
        )


@dataclass(slots=True, frozen=True)
class MultiAgentSessionState:
    schema_version: int = _SCHEMA_VERSION
    session_id: str = ""
    objective: str = ""
    tasks: dict[str, TaskContext] = field(default_factory=dict)
    agents: dict[str, AgentSessionState] = field(default_factory=dict)
    packets: list[ContextPacket] = field(default_factory=list)
    artifacts: dict[str, ArtifactRecord] = field(default_factory=dict)
    events: list[MultiAgentEvent] = field(default_factory=list)
    latest_summary: RollingSummary | None = None
    counters: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "objective": self.objective,
            "tasks": {task_id: task.to_dict() for task_id, task in self.tasks.items()},
            "agents": {agent_id: agent.to_dict() for agent_id, agent in self.agents.items()},
            "packets": [packet.to_dict() for packet in self.packets],
            "artifacts": {artifact_id: artifact.to_dict() for artifact_id, artifact in self.artifacts.items()},
            "events": [event.to_dict() for event in self.events],
            "latest_summary": self.latest_summary.to_dict() if self.latest_summary else None,
            "counters": dict(self.counters),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MultiAgentSessionState":
        raw_tasks = payload.get("tasks", {})
        tasks = {
            str(task_id): TaskContext.from_dict(task_payload)
            for task_id, task_payload in raw_tasks.items()
            if isinstance(task_payload, dict)
        } if isinstance(raw_tasks, dict) else {}
        raw_agents = payload.get("agents", {})
        agents = {
            str(agent_id): AgentSessionState.from_dict(agent_payload)
            for agent_id, agent_payload in raw_agents.items()
            if isinstance(agent_payload, dict)
        } if isinstance(raw_agents, dict) else {}
        raw_packets = payload.get("packets", [])
        packets = [
            ContextPacket.from_dict(packet_payload)
            for packet_payload in raw_packets
            if isinstance(packet_payload, dict)
        ] if isinstance(raw_packets, list) else []
        raw_artifacts = payload.get("artifacts", {})
        artifacts = {
            str(artifact_id): ArtifactRecord.from_dict(artifact_payload)
            for artifact_id, artifact_payload in raw_artifacts.items()
            if isinstance(artifact_payload, dict)
        } if isinstance(raw_artifacts, dict) else {}
        raw_events = payload.get("events", [])
        events = [
            MultiAgentEvent.from_dict(event_payload)
            for event_payload in raw_events
            if isinstance(event_payload, dict)
        ] if isinstance(raw_events, list) else []
        raw_summary = payload.get("latest_summary")
        counters = payload.get("counters", {})
        return cls(
            schema_version=int(payload.get("schema_version") or _SCHEMA_VERSION),
            session_id=str(payload.get("session_id") or ""),
            objective=str(payload.get("objective") or ""),
            tasks=tasks,
            agents=agents,
            packets=packets[-_PACKET_LIMIT:],
            artifacts=artifacts,
            events=events[-_EVENT_LIMIT:],
            latest_summary=RollingSummary.from_dict(raw_summary) if isinstance(raw_summary, dict) else None,
            counters={str(key): int(value) for key, value in counters.items()} if isinstance(counters, dict) else {},
        )


def record_agent_context(metadata: dict[str, Any], record: AgentContextRecord) -> None:
    payload = _ensure_payload(metadata)
    agents = payload.setdefault("agents", {})
    if isinstance(agents, dict):
        agents[record.agent_id] = record.to_dict()
    state = load_multi_agent_state(metadata)
    agents_state = dict(state.agents)
    agents_state[record.agent_id] = AgentSessionState(
        agent_id=record.agent_id,
        role=record.role,
        task_id="",
        status="recorded",
        working_summary=record.summary,
        token_estimate=record.token_estimate,
        message_count=record.message_count,
        allowed_tools=record.allowed_tools,
        input_packet_ids=_packet_ids_only(record.shared_inputs),
        output_packet_ids=record.handoff_outputs,
        tool_call_count=record.tool_call_count,
    )
    save_multi_agent_state(metadata, _replace_state(state, agents=agents_state))


def record_context_packet(metadata: dict[str, Any], packet: ContextPacket) -> None:
    append_context_packet(metadata, packet)


def get_context_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    payload = metadata.get(_METADATA_KEY)
    return payload if isinstance(payload, dict) else {"agents": {}, "packets": []}


def make_context_packet(
    *,
    source_agent: str,
    target_agent: str,
    summary: str,
    artifacts: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
    packet_type: str = "handoff",
    task_id: str | None = None,
    related_files: tuple[str, ...] = (),
    modified_files: tuple[str, ...] = (),
    behavior_changes: tuple[str, ...] = (),
    recommended_tests: tuple[str, ...] = (),
    failure_summary: str | None = None,
    artifact_ids: tuple[str, ...] = (),
    confidence: float | None = None,
) -> ContextPacket:
    estimator = TokenEstimator()
    packet_id = _next_metadata_id(metadata, "packet") if metadata is not None else f"packet-{uuid4().hex[:8]}"
    fields = [
        summary,
        *artifacts,
        *related_files,
        *modified_files,
        *behavior_changes,
        *recommended_tests,
    ]
    if failure_summary:
        fields.append(failure_summary)
    return ContextPacket(
        packet_id=packet_id,
        packet_type=packet_type,
        source_agent=source_agent,
        target_agent=target_agent,
        task_id=task_id,
        summary=summary,
        related_files=related_files,
        modified_files=modified_files,
        behavior_changes=behavior_changes,
        recommended_tests=recommended_tests,
        failure_summary=failure_summary,
        artifact_ids=artifact_ids,
        confidence=confidence,
        artifacts=artifacts,
        token_estimate=sum(estimator.estimate(item) for item in fields if item),
    )


def make_artifact_record(
    *,
    metadata: dict[str, Any] | None = None,
    artifact_type: str,
    task_id: str | None,
    producer_agent: str,
    summary: str,
    content: str = "",
    path: str | None = None,
) -> ArtifactRecord:
    estimator = TokenEstimator()
    artifact_id = _next_metadata_id(metadata, "artifact") if metadata is not None else f"artifact-{uuid4().hex[:8]}"
    capped_content = content[:_ARTIFACT_CONTENT_LIMIT]
    return ArtifactRecord(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        task_id=task_id,
        producer_agent=producer_agent,
        summary=summary,
        content=capped_content,
        path=path,
        token_estimate=estimator.estimate(summary) + estimator.estimate(capped_content),
    )


def load_multi_agent_state(metadata: dict[str, Any]) -> MultiAgentSessionState:
    payload = metadata.get(_MULTI_AGENT_KEY)
    if isinstance(payload, dict) and isinstance(payload.get("state"), dict):
        return MultiAgentSessionState.from_dict(payload["state"])
    return MultiAgentSessionState()


def save_multi_agent_state(
    metadata: dict[str, Any],
    state: MultiAgentSessionState,
    *,
    mode: str | None = None,
    complexity: str | None = None,
) -> None:
    payload = metadata.setdefault(_MULTI_AGENT_KEY, {})
    if not isinstance(payload, dict):
        payload = {}
        metadata[_MULTI_AGENT_KEY] = payload
    if mode is not None:
        payload["mode"] = mode
    else:
        payload.setdefault("mode", "off")
    if complexity is not None:
        payload["complexity"] = complexity
    elif "complexity" not in payload:
        payload["complexity"] = "-"
    payload["state"] = state.to_dict()
    _project_context_payload(metadata, state)


def append_context_packet(metadata: dict[str, Any], packet: ContextPacket) -> None:
    state = load_multi_agent_state(metadata)
    packets = [existing for existing in state.packets if existing.packet_id != packet.packet_id]
    packets.append(packet)
    packets = packets[-_PACKET_LIMIT:]
    save_multi_agent_state(metadata, _replace_state(state, packets=packets))
    _append_context_payload(metadata, packet)


def append_artifact_record(metadata: dict[str, Any], artifact: ArtifactRecord) -> None:
    state = load_multi_agent_state(metadata)
    artifacts = dict(state.artifacts)
    artifacts[artifact.artifact_id] = artifact
    if len(artifacts) > _ARTIFACT_LIMIT:
        keep_ids = list(artifacts)[-_ARTIFACT_LIMIT:]
        artifacts = {artifact_id: artifacts[artifact_id] for artifact_id in keep_ids}
    save_multi_agent_state(metadata, _replace_state(state, artifacts=artifacts))


def upsert_task_context(metadata: dict[str, Any], task_context: TaskContext) -> None:
    state = load_multi_agent_state(metadata)
    tasks = dict(state.tasks)
    tasks[task_context.task_id] = task_context
    save_multi_agent_state(metadata, _replace_state(state, tasks=tasks))


def upsert_agent_state(metadata: dict[str, Any], agent_state: AgentSessionState) -> None:
    state = load_multi_agent_state(metadata)
    agents = dict(state.agents)
    agents[agent_state.agent_id] = agent_state
    save_multi_agent_state(metadata, _replace_state(state, agents=agents))


def append_multi_agent_event(
    metadata: dict[str, Any],
    event_type: str,
    *,
    summary: str = "",
    task_id: str | None = None,
    agent_id: str | None = None,
    packet_id: str | None = None,
    artifact_id: str | None = None,
) -> MultiAgentEvent:
    event = MultiAgentEvent(
        event_id=_next_metadata_id(metadata, "event"),
        event_type=event_type,
        summary=summary,
        task_id=task_id,
        agent_id=agent_id,
        packet_id=packet_id,
        artifact_id=artifact_id,
    )
    state = load_multi_agent_state(metadata)
    events = [*state.events, event][-_EVENT_LIMIT:]
    save_multi_agent_state(metadata, _replace_state(state, events=events))
    return event


def render_context_packet(packet: ContextPacket) -> str:
    parts = [f"{packet.packet_id} {packet.packet_type} {packet.source_agent}->{packet.target_agent}"]
    if packet.task_id:
        parts.append(f"task={packet.task_id}")
    parts.append(packet.summary)
    if packet.modified_files:
        parts.append("modified=" + ", ".join(packet.modified_files[:8]))
    if packet.related_files:
        parts.append("related=" + ", ".join(packet.related_files[:8]))
    if packet.failure_summary:
        parts.append("failure=" + packet.failure_summary[:300])
    if packet.artifact_ids:
        parts.append("artifacts=" + ", ".join(packet.artifact_ids[:8]))
    return " | ".join(part for part in parts if part)

def multi_agent_carry_over_lines(metadata: dict[str, Any]) -> list[str]:
    state = load_multi_agent_state(metadata)
    if state.packets or state.tasks or state.artifacts:
        lines: list[str] = []
        modified_files: list[str] = []
        for task in state.tasks.values():
            modified_files.extend(task.modified_files)
        for packet in state.packets:
            modified_files.extend(packet.modified_files)
        if modified_files:
            lines.append("Multi-agent changed files: " + ", ".join(dict.fromkeys(modified_files[:12])))
        for packet in state.packets[-3:]:
            if packet.packet_type in {"test_failure", "review_findings"}:
                lines.append("Latest multi-agent handoff: " + render_context_packet(packet)[:500])
        if lines:
            return lines
    return []


def _ensure_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    payload = metadata.setdefault(_METADATA_KEY, {"agents": {}, "packets": []})
    if not isinstance(payload, dict):
        payload = {"agents": {}, "packets": []}
        metadata[_METADATA_KEY] = payload
    payload.setdefault("agents", {})
    payload.setdefault("packets", [])
    return payload


def _append_context_payload(metadata: dict[str, Any], packet: ContextPacket) -> None:
    payload = _ensure_payload(metadata)
    packets = payload.setdefault("packets", [])
    if isinstance(packets, list):
        packets[:] = [item for item in packets if not (isinstance(item, dict) and item.get("packet_id") == packet.packet_id)]
        packets.append(packet.to_dict())
        del packets[:-20]


def _project_context_payload(metadata: dict[str, Any], state: MultiAgentSessionState) -> None:
    payload = _ensure_payload(metadata)
    agents = payload.setdefault("agents", {})
    if isinstance(agents, dict):
        for agent_id, agent_state in state.agents.items():
            agents[agent_id] = agent_state.to_context_record().to_dict()
    payload["packets"] = [packet.to_dict() for packet in state.packets[-20:]]


def _replace_state(state: MultiAgentSessionState, **changes: Any) -> MultiAgentSessionState:
    values = {
        "schema_version": state.schema_version,
        "session_id": state.session_id,
        "objective": state.objective,
        "tasks": state.tasks,
        "agents": state.agents,
        "packets": state.packets,
        "artifacts": state.artifacts,
        "events": state.events,
        "latest_summary": state.latest_summary,
        "counters": state.counters,
    }
    values.update(changes)
    return MultiAgentSessionState(**values)


def _next_metadata_id(metadata: dict[str, Any] | None, kind: str) -> str:
    if metadata is None:
        return f"{kind}-{uuid4().hex[:8]}"
    state = load_multi_agent_state(metadata)
    counters = dict(state.counters)
    next_value = int(counters.get(kind, 0)) + 1
    counters[kind] = next_value
    save_multi_agent_state(metadata, _replace_state(state, counters=counters))
    return f"{kind}-{next_value:04d}"


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _packet_ids_only(value: Any) -> tuple[str, ...]:
    return tuple(item for item in _tuple_of_str(value) if _looks_like_packet_id(item))


def _looks_like_packet_id(value: str) -> bool:
    text = value.strip()
    return text.startswith("packet-") or text.startswith("legacy-packet")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
