from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncGenerator
from os import environ
from typing import Any
from urllib import error, request as urllib_request

from nexus.integrations.retry import call_with_backoff, retry_delay
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

    def __init__(self, *, provider_name: str = "openai-compatible") -> None:
        self.provider_name = provider_name

    def to_wire_request(self, request: RuntimeRequest) -> dict[str, Any]:
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
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in message.tool_calls
                    ],
                }
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
            else:
                item = {"role": message.role, "content": message.content}
                if message.name:
                    item["name"] = message.name
            messages.append(item)

        payload: dict[str, Any] = {
            "model": request.model_name,
            "messages": messages,
            "tools": list(request.tool_schemas),
            "temperature": request.temperature,
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        return payload

    def from_wire_response(self, payload: dict[str, Any]) -> RuntimeResponse:
        choice = payload["choices"][0]
        message_payload = choice["message"]
        tool_calls = tuple(
            ToolCall(
                call_id=tool_call["id"],
                tool_name=tool_call["function"]["name"],
                arguments=json.loads(tool_call["function"]["arguments"]),
            )
            for tool_call in message_payload.get("tool_calls") or ()
        )
        message = Message(
            role="assistant",
            content=message_payload.get("content") or "",
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
        self.adapter = OpenAICompatibleAdapter(provider_name=provider_name)

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
                events: list[StreamEvent] = []
                got_retryable_error = False
                async for event in self._stream_sse(wire_payload):
                    if event.type == StreamEventType.ERROR:
                        error_msg = event.error or ""
                        if any(marker in error_msg.lower() for marker in _RETRYABLE_ERROR_MARKERS):
                            last_error = error_msg
                            got_retryable_error = True
                            break
                        # Non-retryable: pass through immediately.
                        yield event
                        return
                    events.append(event)
                if not got_retryable_error:
                    for ev in events:
                        yield ev
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
            yield StreamEvent(type=StreamEventType.ERROR, error=last_error or "Provider request failed after retries.")
        else:
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
            )

    async def _stream_sse(self, payload: dict[str, Any]) -> AsyncGenerator[StreamEvent, None]:
        """Read an SSE stream in a background thread and yield parsed StreamEvents."""
        loop = asyncio.get_running_loop()
        # Queue carries parsed JSON dicts, Exception instances, or None sentinel.
        queue: asyncio.Queue[dict[str, Any] | Exception | None] = asyncio.Queue()

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
                    for raw in resp:
                        line = raw.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            loop.call_soon_threadsafe(queue.put_nowait, json.loads(data_str))
                        except json.JSONDecodeError:
                            pass
            except error.HTTPError as exc:
                details = _http_error_details(exc)
                if exc.code in {408, 409, 429, 500, 502, 503, 504}:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        RetryableProviderError(
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
                    RetryableProviderError(f"Provider connection failed: {exc.reason}"),
                )
            except TimeoutError as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    RetryableProviderError(f"Provider request timed out: {exc}"),
                )
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()

        # Accumulate tool-call fragments keyed by their stream index.
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        usage: UsageSnapshot | None = None
        finish_reason: str | None = None

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

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

            if delta.get("content"):
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(content=delta["content"]),
                )

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


def _request_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _chat_completions_url(api_base_url: str) -> str:
    if api_base_url.endswith("/chat/completions"):
        return api_base_url
    return f"{api_base_url}/chat/completions"


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
