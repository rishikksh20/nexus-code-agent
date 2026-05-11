from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from nexus.hooks import HookEvent, HookExecutor


logger = logging.getLogger(__name__)


class ConfirmationState(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    EXECUTED = "executed"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


class DangerLevel(str, Enum):
    SAFE = "safe"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(slots=True, frozen=True)
class RollbackPlan:
    supported: bool
    summary: str


@dataclass(slots=True)
class DangerousActionRecord:
    action_id: str
    action_name: str
    scope: str
    state: ConfirmationState
    reason: str
    requested_by: str
    danger_level: str
    rollback: RollbackPlan
    session_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None
    tool_call_id: str | None = None
    timestamp: str = datetime.now(UTC).isoformat()


class JsonlAuditTrail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def write(self, record: DangerousActionRecord) -> None:
        payload = asdict(record)
        payload["timestamp"] = datetime.now(UTC).isoformat()
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Failed to write audit trail record %s: %s", record.action_name, exc)


def register_audit_hooks(hooks: HookExecutor, trail: JsonlAuditTrail) -> None:
    async def _on_notification(payload: dict[str, Any]) -> None:
        event_name = str(payload.get("event", "")).strip()
        if event_name not in {"confirmation_requested", "tool_denied"}:
            return
        state = ConfirmationState.REQUESTED if event_name == "confirmation_requested" else ConfirmationState.DENIED
        await trail.write(
            DangerousActionRecord(
                action_id=str(payload.get("call_id") or payload.get("tool_name") or "action"),
                action_name=str(payload.get("tool_name") or event_name),
                scope=str(payload.get("arguments", {}).get("path") if isinstance(payload.get("arguments"), dict) else payload.get("tool_name", "")),
                state=state,
                reason=str(payload.get("reason", "")),
                requested_by=str(payload.get("session_id", "unknown-session")),
                danger_level=classify_danger(str(payload.get("tool_name", "")), payload.get("arguments") or {}).value,
                rollback=rollback_plan(str(payload.get("tool_name", "")), payload.get("arguments") or {}),
                session_id=str(payload.get("session_id")) if payload.get("session_id") else None,
                turn_id=str(payload.get("turn_id")) if payload.get("turn_id") else None,
                trace_id=str(payload.get("trace_id")) if payload.get("trace_id") else None,
                tool_call_id=str(payload.get("call_id")) if payload.get("call_id") else None,
            )
        )

    async def _on_post_tool(payload: dict[str, Any]) -> None:
        if not payload.get("is_mutating"):
            return
        await trail.write(
            DangerousActionRecord(
                action_id=str(payload.get("call_id") or payload.get("tool_name") or "action"),
                action_name=str(payload.get("tool_name", "tool")),
                scope=str(payload.get("arguments", {}).get("path") if isinstance(payload.get("arguments"), dict) else payload.get("tool_name", "")),
                state=ConfirmationState.EXECUTED,
                reason="Mutating action executed.",
                requested_by=str(payload.get("session_id", "unknown-session")),
                danger_level=classify_danger(str(payload.get("tool_name", "")), payload.get("arguments") or {}).value,
                rollback=rollback_plan(str(payload.get("tool_name", "")), payload.get("arguments") or {}),
                session_id=str(payload.get("session_id")) if payload.get("session_id") else None,
                turn_id=str(payload.get("turn_id")) if payload.get("turn_id") else None,
                trace_id=str(payload.get("trace_id")) if payload.get("trace_id") else None,
                tool_call_id=str(payload.get("call_id")) if payload.get("call_id") else None,
            )
        )

    hooks.register(HookEvent.NOTIFICATION, _on_notification)
    hooks.register(HookEvent.POST_TOOL_USE, _on_post_tool)


def classify_danger(tool_name: str, arguments: dict[str, Any]) -> DangerLevel:
    del arguments
    if tool_name in {"get_time", "read_file", "glob", "search_memory", "skill"}:
        return DangerLevel.SAFE
    if tool_name in {"write_note", "write_file"}:
        return DangerLevel.HIGH
    if tool_name in {"bash", "run_command", "delete_file"}:
        return DangerLevel.CRITICAL
    return DangerLevel.LOW


def rollback_plan(tool_name: str, arguments: dict[str, Any]) -> RollbackPlan:
    if tool_name == "write_note":
        path = str(arguments.get("path", "created note")).strip() or "created note"
        return RollbackPlan(supported=True, summary=f"Delete or replace {path} if the write should be reverted.")
    return RollbackPlan(supported=False, summary="No rollback plan is currently available.")