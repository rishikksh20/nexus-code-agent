from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.hooks import HookEvent, HookExecutor
from nexus.observability.metrics import RuntimeMetricsCollector


logger = logging.getLogger(__name__)

SENSITIVE_KEYS = {"api_key", "authorization", "token", "cookie", "password"}


class JsonlRuntimeLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def log(self, event: str, payload: dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "payload": redact_payload(payload),
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Failed to write runtime log event %s: %s", event, exc)


def register_default_runtime_hooks(
    hooks: HookExecutor,
    logger: JsonlRuntimeLogger,
    *,
    metrics_collector: RuntimeMetricsCollector | None = None,
) -> None:
    async def _log_user_prompt(payload: dict[str, Any]) -> None:
        prompt = str(payload.get("prompt", ""))
        await logger.log(
            HookEvent.USER_PROMPT_SUBMIT.value,
            {
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "mode": payload.get("mode"),
                "prompt_chars": len(prompt),
                "prompt_preview": prompt[:80],
            },
        )
        if metrics_collector is not None:
            await metrics_collector.record_prompt(payload)

    async def _log_pre_tool(payload: dict[str, Any]) -> None:
        await logger.log(
            HookEvent.PRE_TOOL_USE.value,
            {
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "tool_name": payload.get("tool_name"),
                "tool_source": payload.get("tool_source"),
                "is_mutating": payload.get("is_mutating"),
                "call_id": payload.get("call_id"),
            },
        )
        if metrics_collector is not None:
            await metrics_collector.record_pre_tool(payload)

    async def _log_post_tool(payload: dict[str, Any]) -> None:
        output = str(payload.get("output", ""))
        await logger.log(
            HookEvent.POST_TOOL_USE.value,
            {
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "tool_name": payload.get("tool_name"),
                "tool_source": payload.get("tool_source"),
                "is_mutating": payload.get("is_mutating"),
                "is_error": payload.get("is_error"),
                "duration_ms": payload.get("duration_ms"),
                "call_id": payload.get("call_id"),
                "output_chars": len(output),
                "output_preview": output[:120],
            },
        )
        if metrics_collector is not None:
            await metrics_collector.record_post_tool(payload)

    async def _log_stop(payload: dict[str, Any]) -> None:
        await logger.log(
            HookEvent.STOP.value,
            {
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "message_count": payload.get("message_count"),
            },
        )
        if metrics_collector is not None:
            await metrics_collector.record_stop(payload)

    async def _log_notification(payload: dict[str, Any]) -> None:
        event_name = str(payload.get("event", ""))
        # Each notification sub-type logs only the fields that matter for it.
        if event_name == "model_usage":
            essentials: dict[str, Any] = {
                "event": event_name,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "model": payload.get("model"),
                "prompt_tokens": payload.get("prompt_tokens"),
                "completion_tokens": payload.get("completion_tokens"),
                "total_tokens": payload.get("total_tokens"),
                "estimated_cost_usd": payload.get("estimated_cost_usd"),
            }
        elif event_name in {"confirmation_requested", "tool_denied"}:
            essentials = {
                "event": event_name,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "tool_name": payload.get("tool_name"),
                "reason": payload.get("reason"),
                "call_id": payload.get("call_id"),
            }
        elif event_name == "clarification_requested":
            essentials = {
                "event": event_name,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "tool_name": payload.get("tool_name"),
                "field": payload.get("field"),
                "call_id": payload.get("call_id"),
            }
        else:
            # Unknown notification — log event name + session context only.
            essentials = {
                "event": event_name,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
            }
        await logger.log(HookEvent.NOTIFICATION.value, essentials)
        if metrics_collector is not None:
            await metrics_collector.record_notification(payload)

    hooks.register(HookEvent.USER_PROMPT_SUBMIT, _log_user_prompt)
    hooks.register(HookEvent.PRE_TOOL_USE, _log_pre_tool)
    hooks.register(HookEvent.POST_TOOL_USE, _log_post_tool)
    hooks.register(HookEvent.STOP, _log_stop)
    hooks.register(HookEvent.NOTIFICATION, _log_notification)


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if key.lower() in SENSITIVE_KEYS:
            redacted[key] = "[REDACTED]"
        elif isinstance(value, dict):
            redacted[key] = redact_payload(value)
        elif isinstance(value, list):
            redacted[key] = [redact_payload(item) if isinstance(item, dict) else item for item in value]
        else:
            redacted[key] = value
    return redacted