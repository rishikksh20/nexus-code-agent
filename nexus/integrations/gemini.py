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


_GEMINI_UNSUPPORTED_SCHEMA_KEYS = {
    "additionalProperties",
    "additional_properties",
}


class GeminiAdapter:
    """Translate Nexus runtime objects to Google Gen AI SDK payloads."""

    @staticmethod
    def tools(tool_schemas: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        declarations: list[dict[str, Any]] = []
        for schema in tool_schemas:
            fn = schema.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            declarations.append(
                {
                    "name": name,
                    "description": fn.get("description", ""),
                    "parameters": _gemini_schema(fn.get("parameters") or {"type": "object"}),
                }
            )
        return [{"function_declarations": declarations}] if declarations else []

    @staticmethod
    def typed_tools(types: Any, tool_schemas: tuple[dict[str, Any], ...]) -> list[Any]:
        declarations: list[Any] = []
        for schema in tool_schemas:
            fn = schema.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            declarations.append(
                types.FunctionDeclaration(
                    name=name,
                    description=fn.get("description", ""),
                    parameters_json_schema=_gemini_schema(fn.get("parameters") or {"type": "object"}),
                )
            )
        return [types.Tool(function_declarations=declarations)] if declarations else []

    @staticmethod
    def contents(messages: tuple[Message, ...]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "tool":
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "function_response": {
                                    "name": msg.name or msg.tool_call_id or "tool",
                                    "response": {"result": msg.content or ""},
                                }
                            }
                        ],
                    }
                )
                continue
            if msg.role == "assistant" and msg.tool_calls:
                parts: list[dict[str, Any]] = []
                if msg.content:
                    parts.append({"text": msg.content})
                parts.extend(
                    {"function_call": {"name": tc.tool_name, "args": tc.arguments}}
                    for tc in msg.tool_calls
                )
                contents.append({"role": "model", "parts": parts})
                continue
            role = "model" if msg.role == "assistant" else "user"
            if msg.content:
                contents.append({"role": role, "parts": [{"text": msg.content}]})
        return contents

    @staticmethod
    def typed_contents(types: Any, messages: tuple[Message, ...]) -> list[Any]:
        contents: list[Any] = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=msg.name or msg.tool_call_id or "tool",
                                response={"result": msg.content or ""},
                            )
                        ],
                    )
                )
                continue
            if msg.role == "assistant" and msg.tool_calls:
                parts: list[Any] = []
                if msg.content:
                    parts.append(types.Part.from_text(text=msg.content))
                parts.extend(
                    types.Part.from_function_call(name=tc.tool_name, args=tc.arguments)
                    for tc in msg.tool_calls
                )
                contents.append(types.Content(role="model", parts=parts))
                continue
            role = "model" if msg.role == "assistant" else "user"
            if msg.content:
                contents.append(
                    types.Content(role=role, parts=[types.Part.from_text(text=msg.content)])
                )
        return contents


class GeminiModelClient:
    """Small native Google Gen AI SDK client with stream and non-stream support."""

    def __init__(self, *, api_key: str | None = None, api_version: str | None = None) -> None:
        self.api_key = resolve_gemini_api_key(api_key)
        self.api_version = resolve_gemini_api_version(api_version)
        self.adapter = GeminiAdapter()
        self._call_counter = 0

    async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
        text = ""
        tool_calls: list[ToolCall] = []
        usage: UsageSnapshot | None = None
        finish_reason = "stop"
        async for event in self.chat_completion(request, stream=False):
            if event.type == StreamEventType.TEXT_DELTA and event.text_delta:
                text += event.text_delta.content
            elif event.type == StreamEventType.TOOL_CALL_COMPLETE and event.tool_call:
                tool_calls.append(event.tool_call)
            elif event.type == StreamEventType.MESSAGE_COMPLETE:
                usage = event.usage
                finish_reason = event.finish_reason or finish_reason
            elif event.type == StreamEventType.ERROR:
                raise RuntimeError(event.error or "Gemini request failed.")
        return RuntimeResponse(
            message=Message(role="assistant", content=text, tool_calls=tuple(tool_calls)),
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
        client, types = self._client_and_types()
        config = self._config(types, request)
        contents = self.adapter.typed_contents(types, request.messages)

        try:
            if stream:
                stream_iter = await client.aio.models.generate_content_stream(
                    model=request.model_name,
                    contents=contents,
                    config=config,
                )
                async for chunk in stream_iter:
                    for event in self._events_from_response(chunk, request.model_name, final=False):
                        yield event
                yield StreamEvent(type=StreamEventType.MESSAGE_COMPLETE, finish_reason="stop")
            else:
                response = await client.aio.models.generate_content(
                    model=request.model_name,
                    contents=contents,
                    config=config,
                )
                for event in self._events_from_response(response, request.model_name, final=True):
                    yield event
        except Exception as exc:  # noqa: BLE001
            yield StreamEvent(type=StreamEventType.ERROR, error=str(exc))

    def _config(self, types: Any, request: RuntimeRequest) -> Any:
        kwargs: dict[str, Any] = {
            "system_instruction": request.system_prompt,
            "temperature": request.temperature,
        }
        if request.max_output_tokens is not None:
            kwargs["max_output_tokens"] = request.max_output_tokens
        tools = self.adapter.typed_tools(types, request.tool_schemas)
        if tools:
            kwargs["tools"] = tools
        return types.GenerateContentConfig(**kwargs)

    def _events_from_response(self, response: Any, model: str, *, final: bool) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for part in _parts(response):
            text = _get(part, "text")
            if text:
                events.append(StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=TextDelta(content=str(text))))
            function_call = _get(part, "function_call")
            if function_call:
                self._call_counter += 1
                events.append(
                    StreamEvent(
                        type=StreamEventType.TOOL_CALL_COMPLETE,
                        tool_call=ToolCall(
                            call_id=f"gemini-{self._call_counter:04d}",
                            tool_name=str(_get(function_call, "name", "")),
                            arguments=dict(_get(function_call, "args", {}) or {}),
                        ),
                    )
                )
        if final:
            events.append(
                StreamEvent(
                    type=StreamEventType.MESSAGE_COMPLETE,
                    finish_reason=_finish_reason(response),
                    usage=_gemini_usage(response, model),
                )
            )
        return events

    def _client_and_types(self) -> tuple[Any, Any]:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("Gemini provider requires the `google-genai` package.") from exc
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        http_options = self._http_options(types)
        if http_options is not None:
            kwargs["http_options"] = http_options
        return genai.Client(**kwargs), types

    def _http_options(self, types: Any) -> Any:
        if not self.api_version:
            return None
        return types.HttpOptions(api_version=self.api_version)


def _parts(response: Any) -> list[Any]:
    candidates = _get(response, "candidates", []) or []
    if not candidates:
        return []
    content = _get(candidates[0], "content", {})
    return list(_get(content, "parts", []) or [])


def _gemini_usage(response: Any, model: str) -> UsageSnapshot | None:
    usage = _get(response, "usage_metadata")
    if not usage:
        return None
    prompt = int(_get(usage, "prompt_token_count", 0) or 0)
    completion = int(_get(usage, "candidates_token_count", 0) or 0)
    total = int(_get(usage, "total_token_count", prompt + completion) or 0)
    return UsageSnapshot(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        provider="gemini",
        model=model,
    )


def _finish_reason(response: Any) -> str:
    candidates = _get(response, "candidates", []) or []
    if not candidates:
        return "stop"
    return str(_get(candidates[0], "finish_reason", "stop") or "stop").lower()


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _gemini_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    return {
        key: _gemini_schema(value)
        for key, value in schema.items()
        if key not in _GEMINI_UNSUPPORTED_SCHEMA_KEYS
    }


def resolve_gemini_api_key(explicit: str | None = None) -> str | None:
    return explicit or environ.get("GEMINI_API_KEY") or environ.get("GOOGLE_API_KEY") or environ.get("API_KEY")


def resolve_gemini_api_version(explicit: str | None = None) -> str | None:
    return explicit or environ.get("GEMINI_API_VERSION") or environ.get("GOOGLE_GENAI_API_VERSION") or None
