from __future__ import annotations

from typing import Any


_NOTIFICATION_OUTPUT_KEYS = {
    "status",
    "output",
    "error",
    "reason",
    "field",
    "risk_level",
    "finish_reason",
    "tool_call_count",
    "usage",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "estimated_cost_usd",
    "duration_ms",
}


def describe_turn_observation(payload: dict[str, Any]) -> str:
    session_id = str(payload.get("session_id", "") or "").strip() or "unknown-session"
    status = str(payload.get("status", "") or "").strip()
    text = (
        f"Nexus turn trace for session {session_id}, covering the full user request, model, tool, "
        "and runtime event lifecycle."
    )
    return f"{text} Current status: {status}." if status else text


def describe_model_observation(payload: dict[str, Any], *, phase: str | None = None) -> str:
    provider = str(payload.get("provider", "") or "").strip() or "unknown-provider"
    model = str(payload.get("model", "") or "").strip() or "unknown-model"
    actor = str(payload.get("actor", "") or "").strip()
    if phase == "start":
        text = (
            f"LLM generation started for provider {provider} and model {model}, capturing the serialized "
            "prompt state and provider request payload."
        )
    elif phase == "end":
        text = (
            f"LLM generation finished for provider {provider} and model {model}, capturing assistant output, "
            "finish reason, and usage details."
        )
    else:
        text = f"LLM generation lifecycle for provider {provider} and model {model}."
    return f"{text} Actor: {actor}." if actor else text


def describe_tool_observation(payload: dict[str, Any], *, phase: str | None = None) -> str:
    tool_name = str(payload.get("tool_name", "") or "").strip() or "unknown-tool"
    source = str(payload.get("tool_source", "") or "").strip()
    if phase == "end":
        text = f"Tool execution completed for {tool_name}, capturing result payload, timing, and error state."
    else:
        text = f"Tool execution span for {tool_name}, capturing input arguments and execution context."
    return f"{text} Source: {source}." if source else text


def describe_notification_event(event_name: str, payload: dict[str, Any]) -> str:
    normalized = str(event_name or payload.get("event", "") or "notification").strip().lower()
    if normalized == "model_start":
        return "Provider request started with prompt, messages, and generation parameters attached as input payload."
    if normalized == "model_end":
        return "Provider request finished with assistant output, finish reason, and usage payload attached as output."
    if normalized == "model_error":
        return "Provider or streaming execution failed before a successful assistant response completed."
    if normalized == "model_usage":
        return "Token and estimated cost accounting emitted for the completed provider response."
    if normalized == "confirmation_requested":
        return "Approval checkpoint emitted before a gated tool call can continue."
    if normalized == "clarification_requested":
        return "Clarification checkpoint emitted because required user input is missing or ambiguous."
    if normalized == "tool_denied":
        return "Tool execution was denied by approval policy or an earlier user refusal."
    if normalized == "mcp_server_error":
        server_name = str(payload.get("server_name", "") or "").strip()
        suffix = f" Server: {server_name}." if server_name else ""
        return f"MCP server interaction failed and emitted an error notification.{suffix}"
    return "Runtime notification captured for event-wise observability and trace reconstruction."


def split_notification_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    input_payload: dict[str, Any] = {}
    output_payload: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _NOTIFICATION_OUTPUT_KEYS:
            output_payload[key] = value
            continue
        input_payload[key] = value
    return input_payload, output_payload or None


__all__ = [
    "describe_model_observation",
    "describe_notification_event",
    "describe_tool_observation",
    "describe_turn_observation",
    "split_notification_payload",
]