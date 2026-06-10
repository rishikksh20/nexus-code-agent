from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from collections.abc import AsyncGenerator
from os import environ
from typing import Any
from urllib import error, request as urllib_request
import warnings

from nexus.integrations.cohere import (
    _cohere_strict_reason_tool_names,
    _cohere_strict_tool_schema,
    _cohere_strict_tools_compatible,
    _strip_cohere_strict_reason_arguments,
    _with_cohere_strict_reason_argument,
)
from nexus.integrations.retry import call_with_backoff, retry_delay
from nexus.config.provider_profiles import ThinkingConfig
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


class OpenAICompatibleAdapter:
    """Translate internal runtime types to and from an OpenAI-compatible wire format."""

    def __init__(
        self,
        *,
        provider_name: str = "openai-compatible",
        cohere_compatibility: bool = False,
        thinking_mode: str = "auto",
        reasoning_effort: str = "high",
        thinking: ThinkingConfig | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.cohere_compatibility = cohere_compatibility
        self.thinking_mode = thinking_mode
        self.reasoning_effort = reasoning_effort
        self.thinking = thinking or ThinkingConfig()

    def to_wire_request(self, request: RuntimeRequest) -> dict[str, Any]:
        tools = list(request.tool_schemas)
        cohere_strict_tools = False
        strict_reason_tool_names: set[str] = set()
        if self.cohere_compatibility and _cohere_strict_tools_compatible(tools):
            cohere_strict_tools = True
            tools = [_cohere_compatible_strict_tool_schema(tool) for tool in tools]
            strict_reason_tool_names = _cohere_strict_reason_tool_names(tools)

        messages = [{"role": "system", "content": request.system_prompt}]
        for message in request.messages:
            if message.role == "assistant" and message.tool_calls:
                # Assistant turn that invoked tools — must include tool_calls; content may be null.
                item: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.content or None,
                    "tool_calls": [
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
                    ],
                }
                if self.thinking_mode != "disabled":
                    item["reasoning_content"] = message.reasoning_content or ""
            elif message.role == "tool":
                if not message.tool_call_id:
                    continue
                # Tool-result message — tool_call_id correlates to the assistant's call.
                item = {"role": "tool", "content": message.content}
                if message.tool_call_id:
                    item["tool_call_id"] = message.tool_call_id
                elif message.name:
                    item["name"] = message.name
            elif message.role == "assistant":
                if not (message.content or message.tool_calls):
                    continue
                item = {"role": message.role, "content": message.content}
                if message.name:
                    item["name"] = message.name
                if self.thinking_mode != "disabled" and message.reasoning_content:
                    item["reasoning_content"] = message.reasoning_content
            else:
                item = {"role": message.role, "content": message.content}
                if message.name:
                    item["name"] = message.name
            messages.append(item)

        payload: dict[str, Any] = {
            "model": request.model_name,
            "messages": messages,
            "tools": tools,
            "temperature": request.temperature,
        }
        if request.top_p != 1.0:
            payload["top_p"] = request.top_p
        if request.max_output_tokens is not None:
            token_key = "max_completion_tokens" if self.provider_name == "openai" and self.thinking.enabled else "max_tokens"
            payload[token_key] = request.max_output_tokens
        if self.thinking_mode in {"enabled", "disabled"}:
            payload["thinking"] = {"type": self.thinking_mode}
            if self.thinking_mode == "enabled" and self.thinking.budget_tokens is not None:
                payload["thinking"]["budget_tokens"] = self.thinking.budget_tokens
            if self.thinking_mode == "enabled" and self.reasoning_effort:
                payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def from_wire_response(self, payload: dict[str, Any]) -> RuntimeResponse:
        choice = payload["choices"][0]
        message_payload = choice["message"]
        tool_calls = tuple(
            _tool_call_from_openai_payload(tool_call)
            for tool_call in message_payload.get("tool_calls") or ()
        )
        message = Message(
            role="assistant",
            content=message_payload.get("content") or "",
            reasoning_content=_extract_reasoning_text(message_payload),
            tool_calls=tool_calls,
        )

        usage_payload = payload.get("usage")
        usage = None
        if usage_payload is not None:
            usage = UsageSnapshot(
                prompt_tokens=usage_payload.get("prompt_tokens", 0),
                completion_tokens=usage_payload.get("completion_tokens", 0),
                total_tokens=usage_payload.get("total_tokens", 0),
                estimated_cost_usd=0.0,
                provider=self.provider_name,
                model=str(payload.get("model", "")),
            )

        return RuntimeResponse(
            message=message,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.get("finish_reason", "done"),
        )


class RetryableProviderError(RuntimeError):
    """Transient provider error that should be retried with backoff."""

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


class OpenAICompatibleModelClient:
    """Minimal OpenAI-compatible model client for live provider calls."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str | None = None,
        provider_name: str = "openai-compatible",
        timeout_seconds: float = 120.0,
        retries: int = 3,
        base_delay: float = 0.5,
        jitter: float = 0.2,
        thinking_mode: str = "auto",
        reasoning_effort: str = "high",
        thinking: ThinkingConfig | None = None,
    ) -> None:
        normalized_base = resolve_provider_api_base_url(provider_name, api_base_url)
        if not normalized_base:
            raise ValueError("OpenAI-compatible providers require api_base_url.")
        self.api_base_url = normalized_base
        self.api_key = resolve_provider_api_key(provider_name, api_key)
        self.provider_name = provider_name
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.base_delay = base_delay
        self.jitter = jitter
        self.thinking_mode = thinking_mode
        self.reasoning_effort = reasoning_effort
        self.thinking = thinking or ThinkingConfig()
        self.adapter = OpenAICompatibleAdapter(
            provider_name=provider_name,
            cohere_compatibility=_is_cohere_compatibility_base_url(normalized_base),
            thinking_mode=thinking_mode,
            reasoning_effort=reasoning_effort,
            thinking=self.thinking,
        )

    async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
        wire_payload = self.adapter.to_wire_request(request)
        response_payload = await call_with_backoff(
            lambda: asyncio.to_thread(self._send_request, wire_payload),
            retries=self.retries,
            base_delay=self.base_delay,
            jitter=self.jitter,
            retryable=_is_retryable_provider_error,
        )
        return self.adapter.from_wire_response(response_payload)

    async def chat_completion(
        self,
        request: RuntimeRequest,
        *,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Yield :class:`StreamEvent` objects for a chat completion request.

        When *stream* is ``True`` the provider API is called with
        ``"stream": true`` and SSE chunks are parsed and emitted as they
        arrive.  When *stream* is ``False`` a single blocking request is made
        and the response is wrapped into equivalent :class:`StreamEvent` values
        so callers always see the same interface.

        Both modes retry on transient provider errors (5xx, 429, etc.) using
        exponential back-off so that a single 503 does not surface to the UI.
        """
        wire_payload = self.adapter.to_wire_request(request)

        if stream:
            wire_payload["stream"] = True
            last_error: str | None = None
            for attempt in range(self.retries):
                got_retryable_error = False
                emitted_output = False
                stream_events = self._stream_sse(wire_payload)
                try:
                    async for event in stream_events:
                        if event.type == StreamEventType.ERROR:
                            error_msg = event.error or ""
                            if not emitted_output and _is_retryable_stream_error(error_msg):
                                last_error = error_msg
                                got_retryable_error = True
                                break
                            yield event
                            return
                        if _stream_event_has_assistant_output(event):
                            emitted_output = True
                            yield event
                            continue
                        if event.type == StreamEventType.MESSAGE_COMPLETE:
                            if not emitted_output:
                                async for fallback_event in self._non_stream_events(wire_payload):
                                    yield fallback_event
                                return
                            yield event
                            return
                        yield event
                finally:
                    await stream_events.aclose()
                if not got_retryable_error:
                    return
                if attempt < self.retries - 1:
                    delay = retry_delay(
                        RetryableProviderError(last_error or "Provider request failed."),
                        attempt=attempt,
                        base_delay=self.base_delay,
                        jitter=self.jitter,
                    )
                    await asyncio.sleep(delay)
            # All retries exhausted — surface the last error.
            if last_error and _looks_like_http_500(last_error):
                async for event in self._non_stream_events(wire_payload):
                    yield event
                return
            yield StreamEvent(type=StreamEventType.ERROR, error=last_error or "Provider request failed after retries.")
        else:
            async for event in self._non_stream_events(wire_payload):
                yield event

    async def _non_stream_events(self, wire_payload: dict[str, Any]) -> AsyncGenerator[StreamEvent, None]:
        wire_payload = {**wire_payload}
        wire_payload.pop("stream", None)
        response_payload = await call_with_backoff(
            lambda: asyncio.to_thread(self._send_request, wire_payload),
            retries=self.retries,
            base_delay=self.base_delay,
            jitter=self.jitter,
            retryable=_is_retryable_provider_error,
        )
        runtime_response = self.adapter.from_wire_response(response_payload)
        if runtime_response.message.content:
            yield StreamEvent(
                type=StreamEventType.TEXT_DELTA,
                text_delta=TextDelta(content=runtime_response.message.content),
            )
        for tc in runtime_response.tool_calls:
            yield StreamEvent(type=StreamEventType.TOOL_CALL_COMPLETE, tool_call=tc)
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=runtime_response.finish_reason,
            usage=runtime_response.usage,
            reasoning_content=runtime_response.message.reasoning_content or None,
        )

    async def _stream_sse(self, payload: dict[str, Any]) -> AsyncGenerator[StreamEvent, None]:
        """Read an SSE stream in a background thread and yield parsed StreamEvents."""
        loop = asyncio.get_running_loop()
        # Queue carries parsed JSON dicts, Exception instances, or None sentinel.
        queue: asyncio.Queue[dict[str, Any] | Exception | None] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)
        cancelled = threading.Event()
        response_lock = threading.Lock()
        response_holder: dict[str, Any] = {}

        def _put_threadsafe(item: dict[str, Any] | Exception | None) -> bool:
            if cancelled.is_set():
                return False
            future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            while not cancelled.is_set():
                try:
                    future.result(timeout=0.1)
                    return True
                except concurrent.futures.TimeoutError:
                    continue
                except Exception:
                    return False
            future.cancel()
            return False

        def _close_response() -> None:
            with response_lock:
                resp = response_holder.get("response")
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

        def _reader() -> None:
            try:
                body = json.dumps(payload).encode("utf-8")
                req = urllib_request.Request(
                    _chat_completions_url(self.api_base_url),
                    data=body,
                    headers=_request_headers(self.api_key),
                    method="POST",
                )
                with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    with response_lock:
                        response_holder["response"] = resp
                    for raw in resp:
                        if cancelled.is_set():
                            break
                        line = raw.decode("utf-8").strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line.partition(":")[2].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            if not _put_threadsafe(json.loads(data_str)):
                                break
                        except json.JSONDecodeError:
                            pass
            except error.HTTPError as exc:
                details = _http_error_details(exc)
                if exc.code in {408, 409, 429, 500, 502, 503, 504}:
                    _put_threadsafe(
                        RetryableProviderError(
                            details,
                            status_code=exc.code,
                            retry_after=_retry_after_seconds(exc.headers),
                        ),
                    )
                else:
                    _put_threadsafe(RuntimeError(details))
            except error.URLError as exc:
                _put_threadsafe(
                    RetryableProviderError(f"Provider connection failed: {exc.reason}"),
                )
            except TimeoutError as exc:
                _put_threadsafe(
                    RetryableProviderError(f"Provider request timed out: {exc}"),
                )
            except Exception as exc:  # noqa: BLE001
                _put_threadsafe(exc)
            finally:
                with response_lock:
                    response_holder.pop("response", None)
                _put_threadsafe(None)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()

        # Accumulate tool-call fragments keyed by their stream index.
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        usage: UsageSnapshot | None = None
        finish_reason: str | None = None
        reasoning_content = ""

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    yield StreamEvent(type=StreamEventType.ERROR, error=str(item))
                    return

                chunk: dict[str, Any] = item

                # Usage block (sometimes sent as a standalone chunk or in the last one).
                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage = UsageSnapshot(
                        prompt_tokens=u.get("prompt_tokens", 0),
                        completion_tokens=u.get("completion_tokens", 0),
                        total_tokens=u.get("total_tokens", 0),
                        provider=self.provider_name,
                        model=str(chunk.get("model", "")),
                    )

                choices = chunk.get("choices") or []
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta") or {}
                message = choice.get("message") or {}

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

                if delta.get("content"):
                    yield StreamEvent(
                        type=StreamEventType.TEXT_DELTA,
                        text_delta=TextDelta(content=delta["content"]),
                    )
                elif message.get("content"):
                    yield StreamEvent(
                        type=StreamEventType.TEXT_DELTA,
                        text_delta=TextDelta(content=message["content"]),
                    )
                reasoning_content += _extract_reasoning_text(delta)
                reasoning_content += _extract_reasoning_text(message)

                # Accumulate tool-call argument fragments.
                for tc_delta in delta.get("tool_calls") or []:
                    idx: int = tc_delta.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.get("id"):
                        tool_calls_acc[idx]["id"] = tc_delta["id"]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        tool_calls_acc[idx]["name"] += fn["name"]
                    if fn.get("arguments"):
                        tool_calls_acc[idx]["arguments"] += fn["arguments"]

                for idx, tool_call in enumerate(message.get("tool_calls") or []):
                    if not isinstance(tool_call, dict):
                        continue
                    try:
                        parsed = _tool_call_from_openai_payload(tool_call)
                    except (KeyError, TypeError, json.JSONDecodeError):
                        continue
                    tool_calls_acc[idx] = {
                        "id": parsed.call_id,
                        "name": parsed.tool_name,
                        "arguments": json.dumps(parsed.arguments),
                    }
        finally:
            cancelled.set()
            _close_response()

        # Emit completed tool calls once the stream ends.
        for idx in sorted(tool_calls_acc):
            tc = tool_calls_acc[idx]
            try:
                args: dict[str, Any] = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {"_raw": tc["arguments"]}
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=tc["id"],
                    tool_name=tc["name"],
                    arguments=args,
                ),
            )

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content=reasoning_content or None,
        )

    def _send_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib_request.Request(
            _chat_completions_url(self.api_base_url),
            data=body,
            headers=_request_headers(self.api_key),
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = _http_error_details(exc)
            if exc.code in {408, 409, 429, 500, 502, 503, 504}:
                raise RetryableProviderError(
                    details,
                    status_code=exc.code,
                    retry_after=_retry_after_seconds(exc.headers),
                ) from exc
            raise RuntimeError(details) from exc
        except error.URLError as exc:
            raise RetryableProviderError(f"Provider connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RetryableProviderError(f"Provider request timed out: {exc}") from exc


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

_STREAM_QUEUE_MAXSIZE = 64


def _request_headers(api_key: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Nexus/0.1 (OpenAI-compatible client)",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _stream_events_have_assistant_output(events: list[StreamEvent]) -> bool:
    return any(_stream_event_has_assistant_output(event) for event in events)


def _stream_event_has_assistant_output(event: StreamEvent) -> bool:
    if event.type == StreamEventType.TEXT_DELTA and event.text_delta and event.text_delta.content:
        return True
    if event.type == StreamEventType.TOOL_CALL_COMPLETE and event.tool_call:
        return True
    return False


def _is_retryable_stream_error(error_msg: str) -> bool:
    lowered = error_msg.lower()
    return any(marker in lowered for marker in _RETRYABLE_ERROR_MARKERS)


def _extract_reasoning_text(payload: dict[str, Any]) -> str:
    """Return provider reasoning text from common OpenAI-compatible shapes."""
    for key in ("reasoning_content", "reasoning", "thinking_content"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _cohere_compatible_strict_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    strict_tool = _cohere_strict_tool_schema(tool)
    fn = strict_tool.get("function")
    if isinstance(fn, dict):
        fn["strict"] = True
    return strict_tool


def _tool_call_from_openai_payload(tool_call: dict[str, Any]) -> ToolCall:
    fn = tool_call["function"]
    arguments = fn.get("arguments") or "{}"
    if isinstance(arguments, dict):
        parsed_arguments = arguments
    else:
        parsed_arguments = json.loads(str(arguments))
    return ToolCall(
        call_id=str(tool_call["id"]),
        tool_name=str(fn["name"]),
        arguments=(
            _strip_cohere_strict_reason_arguments(parsed_arguments)
            if isinstance(parsed_arguments, dict)
            else {"_raw": parsed_arguments}
        ),
    )


def _chat_completions_url(api_base_url: str) -> str:
    if api_base_url.endswith("/chat/completions"):
        return api_base_url
    return f"{api_base_url}/chat/completions"


def _is_cohere_compatibility_base_url(api_base_url: str) -> bool:
    normalized = api_base_url.lower()
    return "cohere.ai/compatibility" in normalized or "cohere.com/compatibility" in normalized


def _looks_like_http_500(message: str) -> bool:
    return "http 500" in message.lower()


def _http_error_details(exc: error.HTTPError) -> str:
    raw = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else ""
    if raw:
        return f"Provider request failed with HTTP {exc.code}: {raw}"
    return f"Provider request failed with HTTP {exc.code}."


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


def _is_retryable_provider_error(exc: Exception) -> bool:
    return isinstance(exc, RetryableProviderError)


def resolve_provider_api_key(provider_name: str, explicit_api_key: str | None = None) -> str | None:
    if explicit_api_key:
        return explicit_api_key
    candidates: list[str] = []
    if provider_name == "mistral":
        candidates.append("MISTRAL_API_KEY")
    # API_KEY is the generic shorthand used in .env; check it last so provider-specific
    # vars take priority, but it works as a universal fallback.
    candidates.extend(["NEXUS_API_KEY", "OPENAI_API_KEY", "API_KEY"])
    for key in candidates:
        value = environ.get(key)
        if value:
            if key == "API_KEY":
                warnings.warn(
                    f"Using generic API_KEY for provider '{provider_name}'. "
                    "Prefer a provider-specific environment variable to avoid sending the wrong secret.",
                    RuntimeWarning,
                    stacklevel=2,
                )
            return value
    return None


def resolve_provider_api_base_url(provider_name: str, explicit_api_base_url: str | None = None) -> str:
    normalized = (explicit_api_base_url or "").strip().rstrip("/")
    if normalized:
        return normalized
    if provider_name == "mistral":
        return (environ.get("MISTRAL_BASE_URL") or "https://api.mistral.ai/v1").strip().rstrip("/")
    # BASE_URL is the generic shorthand used in .env — works for openai-compatible and
    # any other provider that has not set a provider-specific env var above.
    base_url_env = environ.get("BASE_URL", "").strip().rstrip("/")
    if base_url_env:
        return base_url_env
    return ""
