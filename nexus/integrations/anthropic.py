from __future__ import annotations

from collections.abc import AsyncGenerator
from os import environ
from typing import Any

from nexus.models import (
    Message,
    RuntimeRequest,
    RuntimeResponse,
    StreamEvent,
    StreamEventType,
    TextDelta,
    ToolCall,
    UsageSnapshot,
)
from nexus.config.provider_profiles import ThinkingConfig


class AnthropicAdapter:
    """Translate Nexus runtime objects to Anthropic Messages API payloads."""

    @staticmethod
    def tools(tool_schemas: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for schema in tool_schemas:
            fn = schema.get("function") or {}
            tools.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object"},
                }
            )
        return [tool for tool in tools if tool["name"]]

    @staticmethod
    def messages(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "tool":
                wire.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id or msg.name or "",
                                "content": msg.content or "",
                            }
                        ],
                    }
                )
                continue
            if msg.role == "assistant" and msg.tool_calls:
                content: list[dict[str, Any]] = []
                preserved = msg.provider_state.get("anthropic_content_blocks", [])
                if isinstance(preserved, list):
                    content.extend(
                        dict(block)
                        for block in preserved
                        if isinstance(block, dict) and block.get("type") in {"thinking", "redacted_thinking"}
                    )
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                content.extend(
                    {
                        "type": "tool_use",
                        "id": tc.call_id,
                        "name": tc.tool_name,
                        "input": tc.arguments,
                    }
                    for tc in msg.tool_calls
                )
                wire.append({"role": "assistant", "content": content})
                continue
            role = "assistant" if msg.role == "assistant" else "user"
            if msg.content:
                wire.append({"role": role, "content": msg.content})
        return wire


class AnthropicModelClient:
    """Small native Anthropic SDK client with stream and non-stream support."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        thinking: ThinkingConfig | None = None,
    ) -> None:
        self.api_key = resolve_anthropic_api_key(api_key)
        self.timeout_seconds = timeout_seconds
        self.adapter = AnthropicAdapter()
        self.thinking = thinking or ThinkingConfig()

    async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
        text = ""
        tool_calls: list[ToolCall] = []
        usage: UsageSnapshot | None = None
        provider_state: dict[str, Any] = {}
        finish_reason = "stop"
        async for event in self.chat_completion(request, stream=False):
            if event.type == StreamEventType.TEXT_DELTA and event.text_delta:
                text += event.text_delta.content
            elif event.type == StreamEventType.TOOL_CALL_COMPLETE and event.tool_call:
                tool_calls.append(event.tool_call)
            elif event.type == StreamEventType.MESSAGE_COMPLETE:
                usage = event.usage
                finish_reason = event.finish_reason or finish_reason
                if event.provider_state:
                    provider_state.update(event.provider_state)
            elif event.type == StreamEventType.ERROR:
                raise RuntimeError(event.error or "Anthropic request failed.")
        return RuntimeResponse(
            message=Message(
                role="assistant",
                content=text,
                provider_state=provider_state,
                tool_calls=tuple(tool_calls),
            ),
            tool_calls=tuple(tool_calls),
            usage=usage,
            finish_reason=finish_reason,
        )

    async def chat_completion(
        self,
        request: RuntimeRequest,
        *,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        client = self._client()
        kwargs = self._request_kwargs(request)
        if not stream:
            try:
                response = await client.messages.create(**kwargs)
            except Exception as exc:  # noqa: BLE001
                yield StreamEvent(type=StreamEventType.ERROR, error=str(exc))
                return
            for event in _events_from_message(response, request.model_name):
                yield event
            return

        text_blocks: dict[int, str] = {}
        tool_blocks: dict[int, dict[str, Any]] = {}
        thinking_blocks: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: UsageSnapshot | None = None
        try:
            async with client.messages.stream(**kwargs) as stream_ctx:
                async for event in stream_ctx:
                    event_type = _get(event, "type")
                    if event_type == "content_block_start":
                        idx = int(_get(event, "index", 0))
                        block = _get(event, "content_block", {})
                        if _get(block, "type") == "tool_use":
                            tool_blocks[idx] = {
                                "id": _get(block, "id", ""),
                                "name": _get(block, "name", ""),
                                "input_json": "",
                            }
                        elif _get(block, "type") in {"thinking", "redacted_thinking"}:
                            thinking_blocks[idx] = {
                                "type": _get(block, "type"),
                                "thinking": str(_get(block, "thinking", "")),
                                "signature": str(_get(block, "signature", "")),
                            }
                    elif event_type == "content_block_delta":
                        idx = int(_get(event, "index", 0))
                        delta = _get(event, "delta", {})
                        delta_type = _get(delta, "type")
                        if delta_type == "text_delta":
                            text = str(_get(delta, "text", ""))
                            text_blocks[idx] = text_blocks.get(idx, "") + text
                            yield StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=TextDelta(content=text))
                        elif delta_type == "input_json_delta" and idx in tool_blocks:
                            tool_blocks[idx]["input_json"] += str(_get(delta, "partial_json", ""))
                        elif delta_type == "thinking_delta" and idx in thinking_blocks:
                            thinking_blocks[idx]["thinking"] += str(_get(delta, "thinking", ""))
                        elif delta_type == "signature_delta" and idx in thinking_blocks:
                            thinking_blocks[idx]["signature"] += str(_get(delta, "signature", ""))
                    elif event_type == "message_delta":
                        delta = _get(event, "delta", {})
                        finish_reason = _get(delta, "stop_reason") or finish_reason
                        usage = _anthropic_usage(_get(event, "usage"), request.model_name) or usage
        except Exception as exc:  # noqa: BLE001
            yield StreamEvent(type=StreamEventType.ERROR, error=str(exc))
            return

        for idx in sorted(tool_blocks):
            tool = tool_blocks[idx]
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=tool["id"],
                    tool_name=tool["name"],
                    arguments=_json_dict(tool["input_json"]),
                ),
            )
        provider_state = {
            "anthropic_content_blocks": [
                block for _, block in sorted(thinking_blocks.items())
            ]
        } if thinking_blocks else None
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
            provider_state=provider_state,
        )

    def _request_kwargs(self, request: RuntimeRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model_name,
            "system": request.system_prompt,
            "messages": self.adapter.messages(request.messages),
            "max_tokens": request.max_output_tokens or 4096,
        }
        if not self.thinking.enabled:
            kwargs["temperature"] = request.temperature
        if request.top_p != 1.0:
            kwargs["top_p"] = request.top_p
        tools = self.adapter.tools(request.tool_schemas)
        if tools:
            kwargs["tools"] = tools
        if self.thinking.enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking.budget_tokens or 1024,
            }
        return kwargs

    def _client(self):
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise RuntimeError("Anthropic provider requires the `anthropic` package.") from exc
        return AsyncAnthropic(api_key=self.api_key, timeout=self.timeout_seconds)


def _events_from_message(response: Any, model: str):
    tool_calls: list[ToolCall] = []
    usage = _anthropic_usage(_get(response, "usage"), model)
    finish_reason = _get(response, "stop_reason") or "stop"
    thinking_blocks: list[dict[str, Any]] = []
    for block in _get(response, "content", []) or []:
        block_type = _get(block, "type")
        if block_type == "text":
            yield StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=TextDelta(content=str(_get(block, "text", ""))))
        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(
                    call_id=str(_get(block, "id", "")),
                    tool_name=str(_get(block, "name", "")),
                    arguments=dict(_get(block, "input", {}) or {}),
                )
            )
        elif block_type in {"thinking", "redacted_thinking"}:
            thinking_blocks.append(
                {
                    "type": block_type,
                    "thinking": str(_get(block, "thinking", "")),
                    "signature": str(_get(block, "signature", "")),
                }
            )
    for tc in tool_calls:
        yield StreamEvent(type=StreamEventType.TOOL_CALL_COMPLETE, tool_call=tc)
    provider_state = {"anthropic_content_blocks": thinking_blocks} if thinking_blocks else None
    yield StreamEvent(
        type=StreamEventType.MESSAGE_COMPLETE,
        finish_reason=finish_reason,
        usage=usage,
        provider_state=provider_state,
    )


def _anthropic_usage(usage: Any, model: str) -> UsageSnapshot | None:
    if not usage:
        return None
    prompt = int(_get(usage, "input_tokens", 0) or 0)
    completion = int(_get(usage, "output_tokens", 0) or 0)
    return UsageSnapshot(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        provider="anthropic",
        model=model,
    )


def _json_dict(raw: str) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_raw": value}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def resolve_anthropic_api_key(explicit: str | None = None) -> str | None:
    return explicit or environ.get("ANTHROPIC_API_KEY") or environ.get("API_KEY")
