from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from nexus.context import TokenEstimator
from nexus.models import Message


_METADATA_KEY = "multi_agent_context"


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
    artifacts: tuple[str, ...] = ()
    token_estimate: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "summary": self.summary,
            "artifacts": list(self.artifacts),
            "token_estimate": self.token_estimate,
            "created_at": self.created_at,
        }


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


def record_agent_context(metadata: dict[str, Any], record: AgentContextRecord) -> None:
    payload = _ensure_payload(metadata)
    agents = payload.setdefault("agents", {})
    if isinstance(agents, dict):
        agents[record.agent_id] = record.to_dict()


def record_context_packet(metadata: dict[str, Any], packet: ContextPacket) -> None:
    payload = _ensure_payload(metadata)
    packets = payload.setdefault("packets", [])
    if isinstance(packets, list):
        packets.append(packet.to_dict())
        del packets[:-20]


def get_context_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    payload = metadata.get(_METADATA_KEY)
    return payload if isinstance(payload, dict) else {"agents": {}, "packets": []}


def summarize_messages(messages: list[Message], *, limit: int = 240) -> str:
    if not messages:
        return "No local messages yet."
    recent = messages[-4:]
    text = " | ".join(f"{message.role}: {message.content.strip()[:120]}" for message in recent if message.content)
    return text[:limit] if text else "No text content in recent messages."


def estimate_messages(messages: list[Message]) -> int:
    estimator = TokenEstimator()
    return sum(estimator.estimate(message.content) for message in messages)


def make_context_packet(
    *,
    source_agent: str,
    target_agent: str,
    summary: str,
    artifacts: tuple[str, ...] = (),
) -> ContextPacket:
    estimator = TokenEstimator()
    packet_id = f"{source_agent}-to-{target_agent}-{abs(hash((summary, artifacts))) % 1_000_000:06d}"
    return ContextPacket(
        packet_id=packet_id,
        source_agent=source_agent,
        target_agent=target_agent,
        summary=summary,
        artifacts=artifacts,
        token_estimate=estimator.estimate(summary) + sum(estimator.estimate(item) for item in artifacts),
    )


def multi_agent_carry_over_lines(metadata: dict[str, Any]) -> list[str]:
    payload = metadata.get("multi_agent")
    if not isinstance(payload, dict):
        return []
    shared_state = payload.get("shared_state")
    if not isinstance(shared_state, dict):
        return []
    lines: list[str] = []
    changed_files = shared_state.get("changed_files")
    if isinstance(changed_files, list) and changed_files:
        lines.append("Multi-agent changed files: " + ", ".join(str(path) for path in changed_files[:12]))
    verification = shared_state.get("verification_results")
    if isinstance(verification, list) and verification:
        lines.append("Latest verification summary: " + str(verification[-1])[:500])
    review = shared_state.get("review_findings")
    if isinstance(review, list) and review:
        lines.append("Latest review feedback: " + str(review[-1])[:500])
    repair = shared_state.get("repair_decision")
    if isinstance(repair, dict):
        lines.append(
            "Latest repair decision: "
            f"retry={repair.get('retry')}; reason={repair.get('reason', '-')}"
        )
    return lines


def _ensure_payload(metadata: dict[str, Any]) -> dict[str, Any]:
    payload = metadata.setdefault(_METADATA_KEY, {"agents": {}, "packets": []})
    if not isinstance(payload, dict):
        payload = {"agents": {}, "packets": []}
        metadata[_METADATA_KEY] = payload
    payload.setdefault("agents", {})
    payload.setdefault("packets", [])
    return payload
