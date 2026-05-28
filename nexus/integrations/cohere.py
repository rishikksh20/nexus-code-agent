from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncGenerator
from os import environ
from typing import Any
from urllib import error, request as urllib_request

from nexus.integrations.retry import is_rate_limit_error, retry_delay
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


logger = logging.getLogger(__name__)

_MAX_COHERE_TOOL_RESULT_CHARS = 20_000
_TOOL_RESULT_TRUNCATION_MARKER = "\n\n[... Cohere tool result truncated ...]\n\n"
_COHERE_STRICT_TOOLS_MAX_FIELDS = 200
_NEXUS_STRICT_TOOL_REASON_FIELD = "_nexus_tool_call_reason"
_NEXUS_STRICT_TOOL_REASON = "Cohere strict tool schema compatibility."
_COHERE_STRICT_UNSUPPORTED_SCHEMA_KEYS = {
    "allOf",
    "maximum",
    "maxItems",
    "maxLength",
    "minItems",
    "minimum",
    "minLength",
    "not",
    "oneOf",
    "uniqueItems",
}


class CohereAdapter:
    """Translate Nexus runtime objects to and from Cohere Chat API v2 payloads."""

    def to_wire_request(self, request: RuntimeRequest) -> dict[str, Any]:
        tools = self.tools(request.tool_schemas)
        strict_tools = _cohere_strict_tools_compatible(tools)
        if strict_tools:
            tools = [_cohere_strict_tool_schema(tool) for tool in tools]
        strict_reason_tool_names = _cohere_strict_reason_tool_names(tools) if strict_tools else set()

        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system_prompt}]
        for message in request.messages:
            if message.role == "assistant" and message.tool_calls:
                item: dict[str, Any] = {"role": "assistant"}
                if message.content:
                    item["content"] = message.content
                item["tool_calls"] = [
                    {
                        "id": tc.call_id,
                        "type": "function",
                        "function": {
                            "name": tc.tool_name,
                            "arguments": json.dumps(
                                _with_cohere_strict_reason_argument(tc.arguments)
                                if tc.tool_name in strict_reason_tool_names
                                else tc.arguments
                            ),
                        },
                    }
                    for tc in message.tool_calls
                ]
            elif message.role == "tool":
                if not message.tool_call_id:
                    continue
                item = {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": _tool_result_content(message.content),
                }
            elif message.role == "assistant":
                if not message.content:
                    continue
                item = {"role": "assistant", "content": message.content}
            else:
                item = {"role": message.role, "content": message.content}
            messages.append(item)

        payload: dict[str, Any] = {
            "model": request.model_name,
            "messages": messages,
            "temperature": request.temperature,
        }
        if tools:
            payload["tools"] = tools
            if strict_tools:
                payload["strict_tools"] = True
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        return payload

    @staticmethod
    def tools(tool_schemas: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for schema in tool_schemas:
            fn = schema.get("function") or {}
            name = str(fn.get("name", "")).strip()
            if not name:
                continue
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters") or {"type": "object"},
                    },
                }
            )
        return tools

    def from_wire_response(self, payload: dict[str, Any], model: str) -> RuntimeResponse:
        message_payload = payload.get("message") or {}
        text = _content_text(message_payload.get("content"))
        tool_calls = tuple(_tool_call_from_payload(tc) for tc in message_payload.get("tool_calls") or ())
        usage = _cohere_usage(payload.get("usage"), model)
        return RuntimeResponse(
            message=Message(role="assistant", content=text, tool_calls=tool_calls),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=str(payload.get("finish_reason") or "done"),
        )


class RetryableCohereProviderError(RuntimeError):
    """Transient Cohere error that should be retried with backoff."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class CohereModelClient:
    """HTTP Cohere Chat API v2 client with stream and non-stream support."""

    def __init__(
        self,
        *,
        api_base_url: str = "",
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        retries: int = 4,
        base_delay: float = 0.5,
        jitter: float = 0.2,
    ) -> None:
        self.api_base_url = resolve_cohere_api_base_url(api_base_url)
        self.api_key = resolve_cohere_api_key(api_key)
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.base_delay = base_delay
        self.jitter = jitter
        self.adapter = CohereAdapter()

    async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
        wire_payload = self.adapter.to_wire_request(request)
        wire_payload["stream"] = False
        summary = _payload_summary(wire_payload)
        logger.debug(
            "cohere.complete.start model=%s messages=%s tools=%s url=%s last_role=%s role_sequence=%s "
            "assistant_tool_calls=%s tool_results=%s last_tool_call_id=%s last_tool_content_chars=%s "
            "last_tool_content_json_object=%s unmatched_tool_result_ids=%s",
            request.model_name,
            summary["message_count"],
            summary["tool_schema_count"],
            _chat_url(self.api_base_url),
            summary["last_role"],
            summary["role_sequence"],
            summary["assistant_tool_call_count"],
            summary["tool_result_count"],
            summary["last_tool_call_id"],
            summary["last_tool_content_chars"],
            summary["last_tool_content_json_object"],
            summary["unmatched_tool_result_ids"],
        )
        _warn_on_payload_shape(summary)
        response_payload = await _call_with_cohere_backoff(
            lambda: asyncio.to_thread(self._send_request, wire_payload),
            retries=self.retries,
            base_delay=self.base_delay,
            jitter=self.jitter,
        )
        response = self.adapter.from_wire_response(response_payload, request.model_name)
        logger.debug(
            "cohere.complete.end model=%s finish_reason=%s text_chars=%s tool_calls=%s",
            request.model_name,
            response.finish_reason,
            len(response.message.content),
            len(response.tool_calls),
        )
        return response

    async def chat_completion(
        self,
        request: RuntimeRequest,
        *,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        wire_payload = self.adapter.to_wire_request(request)
        wire_payload["stream"] = bool(stream)
        summary = _payload_summary(wire_payload)
        logger.debug(
            "cohere.chat_completion.start model=%s stream=%s messages=%s tools=%s url=%s last_role=%s "
            "role_sequence=%s assistant_tool_calls=%s tool_results=%s last_tool_call_id=%s "
            "last_tool_content_chars=%s last_tool_content_json_object=%s unmatched_tool_result_ids=%s",
            request.model_name,
            stream,
            summary["message_count"],
            summary["tool_schema_count"],
            _chat_url(self.api_base_url),
            summary["last_role"],
            summary["role_sequence"],
            summary["assistant_tool_call_count"],
            summary["tool_result_count"],
            summary["last_tool_call_id"],
            summary["last_tool_content_chars"],
            summary["last_tool_content_json_object"],
            summary["unmatched_tool_result_ids"],
        )
        _warn_on_payload_shape(summary)
        if not stream:
            try:
                runtime_response = await self.complete(request)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cohere.chat_completion.non_stream_error error=%s", exc)
                yield StreamEvent(type=StreamEventType.ERROR, error=str(exc))
                return
            if _cohere_finish_reason_is_error(runtime_response.finish_reason):
                logger.warning(
                    "cohere.chat_completion.non_stream_finish_error finish_reason=%s",
                    runtime_response.finish_reason,
                )
                yield StreamEvent(
                    type=StreamEventType.ERROR,
                    error=f"Cohere finished with {runtime_response.finish_reason}.",
                )
                return
            if runtime_response.message.content:
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(content=runtime_response.message.content),
                )
            for tool_call in runtime_response.tool_calls:
                yield StreamEvent(type=StreamEventType.TOOL_CALL_COMPLETE, tool_call=tool_call)
            yield StreamEvent(
                type=StreamEventType.MESSAGE_COMPLETE,
                finish_reason=runtime_response.finish_reason,
                usage=runtime_response.usage,
            )
            return

        last_error: str | None = None
        for attempt in range(self.retries):
            events: list[StreamEvent] = []
            got_retryable_error = False
            logger.debug("cohere.stream.attempt_start attempt=%s retries=%s", attempt + 1, self.retries)
            async for event in self._stream_sse(wire_payload, request.model_name):
                if event.type == StreamEventType.ERROR:
                    error_msg = event.error or ""
                    logger.warning(
                        "cohere.stream.event_error attempt=%s retryable=%s error=%s",
                        attempt + 1,
                        any(marker in error_msg.lower() for marker in _RETRYABLE_ERROR_MARKERS),
                        error_msg,
                    )
                    if any(marker in error_msg.lower() for marker in _RETRYABLE_ERROR_MARKERS):
                        last_error = error_msg
                        got_retryable_error = True
                        break
                    yield event
                    return
                events.append(event)
            if not got_retryable_error:
                logger.debug(
                    "cohere.stream.attempt_success attempt=%s emitted_events=%s",
                    attempt + 1,
                    len(events),
                )
                for event in events:
                    yield event
                return
            if attempt < self.retries - 1:
                delay = _cohere_retry_delay(
                    RetryableCohereProviderError(
                        last_error or "Cohere request failed.",
                        status_code=429 if _looks_rate_limited(last_error or "") else None,
                    ),
                    attempt=attempt,
                    base_delay=self.base_delay,
                    jitter=self.jitter,
                )
                logger.info(
                    "cohere.stream.retry_sleep attempt=%s delay_seconds=%.1f error=%s",
                    attempt + 1,
                    delay,
                    last_error,
                )
                await asyncio.sleep(delay)
        logger.warning("cohere.stream.retries_exhausted error=%s", last_error)
        yield StreamEvent(type=StreamEventType.ERROR, error=last_error or "Cohere request failed after retries.")

    async def _stream_sse(self, payload: dict[str, Any], model: str) -> AsyncGenerator[StreamEvent, None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | Exception | None] = asyncio.Queue()

        def _reader() -> None:
            try:
                body = json.dumps(payload).encode("utf-8")
                req = urllib_request.Request(
                    _chat_url(self.api_base_url),
                    data=body,
                    headers=_request_headers(self.api_key),
                    method="POST",
                )
                with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8").strip()
                        if not line or line.startswith(":") or line.startswith("event:"):
                            continue
                        if line.startswith("data:"):
                            line = line.partition(":")[2].strip()
                        try:
                            loop.call_soon_threadsafe(queue.put_nowait, json.loads(line))
                        except json.JSONDecodeError:
                            pass
            except error.HTTPError as exc:
                details = _http_error_details(exc)
                if exc.code in {408, 409, 429, 500, 502, 503, 504}:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        RetryableCohereProviderError(
                            details,
                            status_code=exc.code,
                            retry_after=_retry_after_seconds(exc.headers),
                        ),
                    )
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, RuntimeError(details))
            except error.URLError as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    RetryableCohereProviderError(f"Cohere connection failed: {exc.reason}"),
                )
            except TimeoutError as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    RetryableCohereProviderError(f"Cohere request timed out: {exc}"),
                )
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()

        tool_calls_acc: dict[int, dict[str, str]] = {}
        usage: UsageSnapshot | None = None
        finish_reason: str | None = None
        event_count = 0

        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                yield StreamEvent(type=StreamEventType.ERROR, error=str(item))
                return

            event = item
            event_count += 1
            event_type = str(event.get("type", ""))
            delta = event.get("delta") or {}
            message = delta.get("message") or {}
            if not _is_streaming_delta_event_type(event_type):
                logger.debug(
                    "cohere.sse.event type=%s index=%s finish_reason=%s error_present=%s",
                    event_type or "(missing)",
                    event.get("index"),
                    delta.get("finish_reason"),
                    bool(delta.get("error")),
                )

            if event_type in {"content-start", "content-delta"}:
                text = _content_delta_text(message.get("content"))
                if text:
                    logger.info("cohere.sse.text_delta type=%s chars=%s", event_type, len(text))
                    yield StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=TextDelta(content=text))
            elif event_type == "tool-call-start":
                idx = int(event.get("index", 0) or 0)
                tool_calls_acc[idx] = _stream_tool_call_seed(message.get("tool_calls"))
                logger.debug(
                    "cohere.sse.tool_call_start index=%s call_id=%s name=%s",
                    idx,
                    tool_calls_acc[idx].get("id"),
                    tool_calls_acc[idx].get("name"),
                )
            elif event_type == "tool-call-delta":
                idx = int(event.get("index", 0) or 0)
                tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                tool_call_delta = _first_tool_call(message.get("tool_calls"))
                if tool_call_delta.get("id"):
                    tool_calls_acc[idx]["id"] = str(tool_call_delta["id"])
                fn = tool_call_delta.get("function") or {}
                if fn.get("name"):
                    tool_calls_acc[idx]["name"] += str(fn["name"])
                if fn.get("arguments"):
                    tool_calls_acc[idx]["arguments"] += str(fn["arguments"])
                logger.info(
                    "cohere.sse.tool_call_delta index=%s call_id=%s name=%s argument_chars=%s",
                    idx,
                    tool_calls_acc[idx].get("id"),
                    tool_calls_acc[idx].get("name"),
                    len(tool_calls_acc[idx].get("arguments", "")),
                )
            elif event_type == "message-end":
                finish_reason = str(delta.get("finish_reason") or finish_reason or "done")
                if delta.get("error"):
                    logger.warning(
                        "cohere.sse.message_end_error finish_reason=%s error=%s",
                        finish_reason,
                        delta.get("error"),
                    )
                    yield StreamEvent(type=StreamEventType.ERROR, error=str(delta["error"]))
                    return
                if _cohere_finish_reason_is_error(finish_reason):
                    logger.warning("cohere.sse.message_end_finish_error finish_reason=%s", finish_reason)
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"Cohere finished with {finish_reason}.",
                    )
                    return
                usage = _cohere_usage(delta.get("usage"), model) or usage

        if event_count == 0:
            logger.warning("cohere.sse.stream_closed_without_events")
        elif finish_reason is None:
            logger.warning("cohere.sse.stream_closed_without_message_end events=%s", event_count)

        for idx in sorted(tool_calls_acc):
            tool = tool_calls_acc[idx]
            logger.debug(
                "cohere.sse.tool_call_complete index=%s call_id=%s name=%s argument_chars=%s",
                idx,
                tool.get("id"),
                tool.get("name"),
                len(tool.get("arguments", "")),
            )
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=tool["id"],
                    tool_name=tool["name"],
                    arguments=_strip_cohere_strict_reason_arguments(_json_dict(tool["arguments"])),
                ),
            )
        logger.debug(
            "cohere.sse.message_complete finish_reason=%s tool_calls=%s usage_present=%s",
            finish_reason,
            len(tool_calls_acc),
            usage is not None,
        )
        yield StreamEvent(type=StreamEventType.MESSAGE_COMPLETE, finish_reason=finish_reason, usage=usage)

    def _send_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            _chat_url(self.api_base_url),
            data=body,
            headers=_request_headers(self.api_key),
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = _http_error_details(exc)
            if exc.code in {408, 409, 429, 500, 502, 503, 504}:
                raise RetryableCohereProviderError(
                    details,
                    status_code=exc.code,
                    retry_after=_retry_after_seconds(exc.headers),
                ) from exc
            raise RuntimeError(details) from exc
        except error.URLError as exc:
            raise RetryableCohereProviderError(f"Cohere connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RetryableCohereProviderError(f"Cohere request timed out: {exc}") from exc


async def _call_with_cohere_backoff(
    operation,
    *,
    retries: int,
    base_delay: float,
    jitter: float,
):
    for attempt in range(retries):
        try:
            return await operation()
        except Exception as exc:
            if not _is_retryable_cohere_error(exc):
                raise
            if attempt == retries - 1:
                raise
            await asyncio.sleep(
                _cohere_retry_delay(exc, attempt=attempt, base_delay=base_delay, jitter=jitter)
            )
    raise RuntimeError("Cohere retry loop exhausted unexpectedly.")


def _cohere_retry_delay(
    exc: Exception,
    *,
    attempt: int,
    base_delay: float = 0.5,
    jitter: float = 0.2,
) -> float:
    if not is_rate_limit_error(exc):
        return retry_delay(exc, attempt=attempt, base_delay=base_delay, jitter=jitter)

    cooldown = 10.0 + (5.0 * attempt)
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        return max(float(retry_after), cooldown)
    return cooldown


def _looks_rate_limited(message: str) -> bool:
    lowered = message.lower()
    return "429" in lowered or "rate limit" in lowered or "rate_limit" in lowered


_RETRYABLE_ERROR_MARKERS = (
    "503",
    "502",
    "504",
    "429",
    "500",
    "408",
    "409",
    "connection failed",
    "timed out",
    "timeout",
)


def _request_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _chat_url(api_base_url: str) -> str:
    normalized = api_base_url.rstrip("/")
    if normalized.endswith("/v2/chat"):
        return normalized
    if normalized.endswith("/v2"):
        return f"{normalized}/chat"
    return f"{normalized}/v2/chat"


def _http_error_details(exc: error.HTTPError) -> str:
    raw = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else ""
    if raw:
        return f"Cohere request failed with HTTP {exc.code}: {raw}"
    return f"Cohere request failed with HTTP {exc.code}."


def _retry_after_seconds(headers: Any) -> float | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _is_streaming_delta_event_type(event_type: str) -> bool:
    return event_type in {"content-start", "content-delta", "tool-call-delta"}


def _is_retryable_cohere_error(exc: Exception) -> bool:
    return isinstance(exc, RetryableCohereProviderError)


def _tool_call_from_payload(payload: dict[str, Any]) -> ToolCall:
    fn = payload.get("function") or {}
    return ToolCall(
        call_id=str(payload.get("id", "")),
        tool_name=str(fn.get("name", "")),
        arguments=_strip_cohere_strict_reason_arguments(_json_dict(str(fn.get("arguments") or ""))),
    )


def _cohere_strict_tools_compatible(tools: list[dict[str, Any]]) -> bool:
    if not tools:
        return False
    strict_tools = [_cohere_strict_tool_schema(tool) for tool in tools]
    return (
        sum(_schema_field_count(_function_parameters(tool)) for tool in strict_tools)
        <= _COHERE_STRICT_TOOLS_MAX_FIELDS
    )


def _cohere_strict_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    strict_tool = json.loads(json.dumps(tool))
    fn = strict_tool.setdefault("function", {})
    parameters = fn.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    fn["parameters"] = _cohere_strict_schema(parameters)
    return strict_tool


def _cohere_strict_reason_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        parameters = fn.get("parameters")
        if not isinstance(parameters, dict):
            continue
        required = parameters.get("required")
        if isinstance(required, list) and _NEXUS_STRICT_TOOL_REASON_FIELD in required:
            name = str(fn.get("name", "")).strip()
            if name:
                names.add(name)
    return names


def _cohere_strict_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_cohere_strict_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _COHERE_STRICT_UNSUPPORTED_SCHEMA_KEYS:
            continue
        cleaned[key] = _cohere_strict_schema(value)

    if cleaned.get("type") == "object":
        properties = cleaned.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        required = cleaned.get("required")
        required_names = [str(item) for item in required] if isinstance(required, list) else []
        if not required_names:
            properties = {
                **properties,
                _NEXUS_STRICT_TOOL_REASON_FIELD: {
                    "type": "string",
                    "description": "Brief reason for calling this tool.",
                },
            }
            required_names = [_NEXUS_STRICT_TOOL_REASON_FIELD]
        cleaned["properties"] = properties
        cleaned["required"] = required_names

    return cleaned


def _function_parameters(tool: dict[str, Any]) -> dict[str, Any]:
    fn = tool.get("function") if isinstance(tool, dict) else None
    parameters = fn.get("parameters") if isinstance(fn, dict) else None
    return parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}


def _schema_field_count(schema: Any) -> int:
    if isinstance(schema, list):
        return sum(_schema_field_count(item) for item in schema)
    if not isinstance(schema, dict):
        return 0
    count = 0
    properties = schema.get("properties")
    if isinstance(properties, dict):
        count += len(properties)
    for value in schema.values():
        count += _schema_field_count(value)
    return count


def _with_cohere_strict_reason_argument(arguments: dict[str, Any]) -> dict[str, Any]:
    if _NEXUS_STRICT_TOOL_REASON_FIELD in arguments:
        return arguments
    return {**arguments, _NEXUS_STRICT_TOOL_REASON_FIELD: _NEXUS_STRICT_TOOL_REASON}


def _strip_cohere_strict_reason_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            _strip_cohere_strict_reason_arguments(value)
            if isinstance(value, dict)
            else [
                _strip_cohere_strict_reason_arguments(item) if isinstance(item, dict) else item
                for item in value
            ]
            if isinstance(value, list)
            else value
        )
        for key, value in arguments.items()
        if key != _NEXUS_STRICT_TOOL_REASON_FIELD
    }


def _tool_result_content(content: str) -> str:
    original_chars = len(content)
    if original_chars > _MAX_COHERE_TOOL_RESULT_CHARS:
        logger.warning(
            "cohere.tool_result.truncated original_chars=%s max_chars=%s",
            original_chars,
            _MAX_COHERE_TOOL_RESULT_CHARS,
        )
        return json.dumps(
            {
                "result": _truncate_middle(content, _MAX_COHERE_TOOL_RESULT_CHARS),
                "truncated": True,
                "original_chars": original_chars,
            }
        )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return json.dumps({"result": content})
    if isinstance(parsed, dict):
        return content
    return json.dumps({"result": parsed})


def _truncate_middle(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    marker = _TOOL_RESULT_TRUNCATION_MARKER
    budget = max(0, max_chars - len(marker))
    head_chars = budget // 2
    tail_chars = budget - head_chars
    return f"{value[:head_chars]}{marker}{value[-tail_chars:] if tail_chars else ''}"


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    role_sequence: list[str] = []
    assistant_tool_call_ids: set[str] = set()
    assistant_tool_call_count = 0
    tool_result_ids: list[str] = []
    last_tool_content = ""
    last_tool_content_is_json_object = False

    for message in messages:
        if not isinstance(message, dict):
            role_sequence.append("(invalid)")
            continue
        role = str(message.get("role", ""))
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            role_sequence.append(f"{role}:tool_calls={len(tool_calls)}")
            assistant_tool_call_count += len(tool_calls)
            for tool_call in tool_calls:
                if isinstance(tool_call, dict) and tool_call.get("id"):
                    assistant_tool_call_ids.add(str(tool_call["id"]))
            continue
        if role == "tool":
            tool_call_id = str(message.get("tool_call_id", ""))
            role_sequence.append(f"{role}:{tool_call_id[:12]}")
            tool_result_ids.append(tool_call_id)
            last_tool_content = str(message.get("content", ""))
            last_tool_content_is_json_object = _is_json_object(last_tool_content)
            continue
        role_sequence.append(role or "(missing)")

    unmatched = [tool_id for tool_id in tool_result_ids if tool_id not in assistant_tool_call_ids]
    last = messages[-1] if messages and isinstance(messages[-1], dict) else {}
    return {
        "message_count": len(messages),
        "tool_schema_count": len(payload.get("tools") or []),
        "last_role": str(last.get("role", "")) if isinstance(last, dict) else "",
        "role_sequence": " > ".join(role_sequence),
        "assistant_tool_call_count": assistant_tool_call_count,
        "tool_result_count": len(tool_result_ids),
        "last_tool_call_id": tool_result_ids[-1] if tool_result_ids else "",
        "last_tool_content_chars": len(last_tool_content),
        "last_tool_content_json_object": last_tool_content_is_json_object,
        "unmatched_tool_result_ids": ",".join(unmatched),
    }


def _warn_on_payload_shape(summary: dict[str, Any]) -> None:
    if summary.get("unmatched_tool_result_ids"):
        logger.warning(
            "cohere.payload.unmatched_tool_result_ids ids=%s role_sequence=%s",
            summary["unmatched_tool_result_ids"],
            summary["role_sequence"],
        )
    if summary.get("last_role") == "tool" and not summary.get("last_tool_content_json_object"):
        logger.warning(
            "cohere.payload.last_tool_content_not_json_object call_id=%s chars=%s",
            summary.get("last_tool_call_id", ""),
            summary.get("last_tool_content_chars", 0),
        )


def _is_json_object(value: str) -> bool:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict)


def _cohere_finish_reason_is_error(finish_reason: str | None) -> bool:
    return str(finish_reason or "").strip().upper() in {"ERROR", "TIMEOUT"}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _content_delta_text(content: Any) -> str:
    if isinstance(content, dict):
        return str(content.get("text") or "")
    return ""


def _first_tool_call(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return value[0] if value and isinstance(value[0], dict) else {}
    return value if isinstance(value, dict) else {}


def _stream_tool_call_seed(value: Any) -> dict[str, str]:
    tool_call = _first_tool_call(value)
    fn = tool_call.get("function") or {}
    return {
        "id": str(tool_call.get("id", "")),
        "name": str(fn.get("name", "")),
        "arguments": str(fn.get("arguments") or ""),
    }


def _json_dict(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_raw": value}


def _cohere_usage(usage: Any, model: str) -> UsageSnapshot | None:
    if not isinstance(usage, dict):
        return None
    tokens = usage.get("tokens") or {}
    prompt = int(tokens.get("input_tokens", 0) or 0)
    completion = int(tokens.get("output_tokens", 0) or 0)
    return UsageSnapshot(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        provider="cohere",
        model=model,
    )


def resolve_cohere_api_key(explicit: str | None = None) -> str | None:
    return (
        explicit
        or environ.get("COHERE_API_KEY")
        or environ.get("CO_API_KEY")
        or environ.get("NEXUS_API_KEY")
        or environ.get("API_KEY")
    )


def resolve_cohere_api_base_url(explicit: str | None = None) -> str:
    return (
        explicit
        or environ.get("COHERE_BASE_URL")
        or environ.get("CO_API_BASE_URL")
        or "https://api.cohere.com"
    ).strip().rstrip("/")
