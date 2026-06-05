from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.hooks import HookEvent, HookExecutor
from nexus.observability.metrics import RuntimeMetricsCollector


logger = logging.getLogger(__name__)
TEXT_RUNTIME_LOG_FILENAME = "console.log.txt"

SENSITIVE_KEYS = {"api_key", "authorization", "token", "cookie", "password"}
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"\b([A-Za-z0-9_/-]{24,}\.[A-Za-z0-9_/-]{24,}\.[A-Za-z0-9_/-]{16,})\b"),
    re.compile(r"\b([A-Za-z0-9+/]{32,}={0,2})\b"),
)


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


def configure_root_text_logging(*, level: int, log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / TEXT_RUNTIME_LOG_FILENAME
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=level,
        handlers=[stream_handler, file_handler],
        force=True,
    )
    logging.captureWarnings(True)
    return log_path


def register_default_runtime_hooks(
    hooks: HookExecutor,
    logger: JsonlRuntimeLogger,
    *,
    metrics_collector: RuntimeMetricsCollector | None = None,
) -> None:
    async def _log_turn_start(payload: dict[str, Any]) -> None:
        await logger.log(
            HookEvent.TURN_START.value,
            {
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "trace_id": payload.get("trace_id"),
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "mode": payload.get("mode"),
                "agent_mode": payload.get("agent_mode"),
                "status": payload.get("status"),
            },
        )

    async def _log_turn_end(payload: dict[str, Any]) -> None:
        await logger.log(
            HookEvent.TURN_END.value,
            {
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "trace_id": payload.get("trace_id"),
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "status": payload.get("status"),
                "duration_ms": payload.get("duration_ms"),
                "tool_calls": payload.get("tool_calls"),
                "response_chars": len(str(payload.get("response", ""))),
                "error": payload.get("error"),
            },
        )

    async def _log_context_compaction(payload: dict[str, Any]) -> None:
        await logger.log(
            HookEvent.CONTEXT_COMPACTION.value,
            {
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "trace_id": payload.get("trace_id"),
                "messages_before_prune": payload.get("messages_before_prune"),
                "messages_before_compaction": payload.get("messages_before_compaction"),
                "messages_after": payload.get("messages_after"),
                "pruned_tool_results": payload.get("pruned_tool_results"),
                "compacted": payload.get("compacted"),
                "carry_over_entries": payload.get("carry_over_entries"),
            },
        )
        if metrics_collector is not None:
            await metrics_collector.record_context_compaction(payload)

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
        elif event_name == "clarification_answered":
            essentials = {
                "event": event_name,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "tool_name": payload.get("tool_name"),
                "call_id": payload.get("call_id"),
                "answer_type": payload.get("answer_type"),
                "selected_option_id": payload.get("selected_option_id"),
                "answer_length": payload.get("answer_length"),
            }
        elif event_name == "model_start":
            essentials = {
                "event": event_name,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "trace_id": payload.get("trace_id"),
                "model_call_id": payload.get("model_call_id"),
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "turn_index": payload.get("turn_index"),
                "actor": payload.get("actor"),
                "message_count": payload.get("message_count"),
                "tool_schema_count": payload.get("tool_schema_count"),
                "system_prompt_chars": payload.get("system_prompt_chars"),
            }
        elif event_name == "model_end":
            output = str(payload.get("output", ""))
            essentials = {
                "event": event_name,
                "session_id": payload.get("session_id"),
                "turn_id": payload.get("turn_id"),
                "trace_id": payload.get("trace_id"),
                "model_call_id": payload.get("model_call_id"),
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "finish_reason": payload.get("finish_reason"),
                "tool_call_count": payload.get("tool_call_count"),
                "output_chars": len(output),
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

    hooks.register(HookEvent.TURN_START, _log_turn_start)
    hooks.register(HookEvent.TURN_END, _log_turn_end)
    hooks.register(HookEvent.CONTEXT_COMPACTION, _log_context_compaction)
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
            redacted[key] = [
                redact_payload(item)
                if isinstance(item, dict)
                else _redact_text(item)
                if isinstance(item, str)
                else item
                for item in value
            ]
        elif isinstance(value, str):
            redacted[key] = _redact_text(value)
        else:
            redacted[key] = value
    return redacted


def _redact_text(value: str) -> str:
    redacted = value
    redacted = _SECRET_VALUE_PATTERNS[0].sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    for pattern in _SECRET_VALUE_PATTERNS[1:]:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
