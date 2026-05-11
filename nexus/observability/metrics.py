from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class RuntimeMetricsCollector:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._metrics: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "totals": {
                "prompt_submissions": 0,
                "tool_calls_started": 0,
                "tool_calls_completed": 0,
                "tool_errors": 0,
                "confirmation_requests": 0,
                "clarification_requests": 0,
                "tool_denials": 0,
                "stop_events": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
            },
            "tools": {},
            "sessions": {},
        }

    async def record_prompt(self, payload: dict[str, Any]) -> None:
        self._metrics["totals"]["prompt_submissions"] += 1
        session = self._session(payload.get("session_id"))
        session["prompt_submissions"] += 1
        session["last_event_at"] = datetime.now(UTC).isoformat()
        await self._flush()

    async def record_pre_tool(self, payload: dict[str, Any]) -> None:
        self._metrics["totals"]["tool_calls_started"] += 1
        tool = self._tool(payload.get("tool_name"))
        tool["started"] += 1
        session = self._session(payload.get("session_id"))
        session["tool_calls_started"] += 1
        session["last_event_at"] = datetime.now(UTC).isoformat()
        await self._flush()

    async def record_post_tool(self, payload: dict[str, Any]) -> None:
        self._metrics["totals"]["tool_calls_completed"] += 1
        if payload.get("is_error"):
            self._metrics["totals"]["tool_errors"] += 1
        tool = self._tool(payload.get("tool_name"))
        tool["completed"] += 1
        if payload.get("is_error"):
            tool["errors"] += 1
        session = self._session(payload.get("session_id"))
        session["tool_calls_completed"] += 1
        if payload.get("is_error"):
            session["tool_errors"] += 1
        session["last_event_at"] = datetime.now(UTC).isoformat()
        await self._flush()

    async def record_notification(self, payload: dict[str, Any]) -> None:
        event_name = str(payload.get("event", "")).strip()
        session = self._session(payload.get("session_id")) if payload.get("session_id") else None
        if event_name == "model_usage":
            self._metrics["totals"]["prompt_tokens"] += int(payload.get("prompt_tokens", 0))
            self._metrics["totals"]["completion_tokens"] += int(payload.get("completion_tokens", 0))
            self._metrics["totals"]["total_tokens"] += int(payload.get("total_tokens", 0))
            self._metrics["totals"]["estimated_cost_usd"] = round(
                self._metrics["totals"]["estimated_cost_usd"] + float(payload.get("estimated_cost_usd", 0.0)),
                6,
            )
            if session is not None:
                session["prompt_tokens"] += int(payload.get("prompt_tokens", 0))
                session["completion_tokens"] += int(payload.get("completion_tokens", 0))
                session["total_tokens"] += int(payload.get("total_tokens", 0))
                session["estimated_cost_usd"] = round(
                    session["estimated_cost_usd"] + float(payload.get("estimated_cost_usd", 0.0)),
                    6,
                )
        elif event_name == "confirmation_requested":
            self._metrics["totals"]["confirmation_requests"] += 1
            self._tool(payload.get("tool_name"))["confirmations"] += 1
        elif event_name == "clarification_requested":
            self._metrics["totals"]["clarification_requests"] += 1
            self._tool(payload.get("tool_name"))["clarifications"] += 1
        elif event_name == "tool_denied":
            self._metrics["totals"]["tool_denials"] += 1
            self._tool(payload.get("tool_name"))["denials"] += 1
        if session is not None:
            session["last_event_at"] = datetime.now(UTC).isoformat()
        await self._flush()

    async def record_stop(self, payload: dict[str, Any]) -> None:
        self._metrics["totals"]["stop_events"] += 1
        session = self._session(payload.get("session_id"))
        session["stops"] += 1
        session["last_message_count"] = int(payload.get("message_count", 0))
        session["last_event_at"] = datetime.now(UTC).isoformat()
        await self._flush()

    def _tool(self, tool_name: Any) -> dict[str, Any]:
        name = str(tool_name or "unknown")
        tool_metrics = self._metrics["tools"].setdefault(
            name,
            {"started": 0, "completed": 0, "errors": 0, "confirmations": 0, "clarifications": 0, "denials": 0},
        )
        return tool_metrics

    def _session(self, session_id: Any) -> dict[str, Any]:
        name = str(session_id or "unknown")
        session_metrics = self._metrics["sessions"].setdefault(
            name,
            {
                "prompt_submissions": 0,
                "tool_calls_started": 0,
                "tool_calls_completed": 0,
                "tool_errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
                "stops": 0,
                "last_message_count": 0,
                "last_event_at": "",
            },
        )
        return session_metrics

    async def _flush(self) -> None:
        self._metrics["generated_at"] = datetime.now(UTC).isoformat()
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._metrics, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Failed to write runtime metrics: %s", exc)