"""Ollama native API client.

Uses Ollama's ``/api/chat`` endpoint instead of the OpenAI-compatible shim.
The native endpoint has more reliable tool-call support for locally-served
models (llama3, qwen, mistral-nemo, phi4, etc.).

Key differences from the OpenAI-compatible wire format
-------------------------------------------------------
* **Streaming**: newline-delimited JSON (NDJSON) instead of SSE ``data:`` lines.
* **Tool calls**: No ``id`` field — we generate stable IDs via a counter.
* **Temperature / options**: lives inside ``{"options": {"temperature": …}}``
  rather than at the top level.
* **Tool results**: ``{"role": "tool", "content": "…"}`` — no ``tool_call_id``
  required (Ollama matches by position).
* **Done signal**: ``"done": true`` chunk instead of ``finish_reason``.

Usage (config.toml / .env)::

    PROVIDER=ollama
    MODEL=llama3.1
    BASE_URL=http://localhost:11434   # optional, this is the default
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
from collections.abc import AsyncGenerator
from os import environ
from typing import Any
from urllib import error, request as urllib_request

from nexus.integrations.retry import call_with_backoff
from nexus.config.provider_profiles import ThinkingConfig
from nexus.models import (
    Message,
    RuntimeRequest,
    StreamEvent,
    StreamEventType,
    TextDelta,
    ToolCall,
    UsageSnapshot,
)


# ---------------------------------------------------------------------------
# Wire-format adapter  (RuntimeRequest → Ollama JSON, Ollama JSON → StreamEvent)
# ---------------------------------------------------------------------------

class OllamaAdapter:
    """Translate between Nexus internal types and Ollama's native API format."""

    def __init__(self, *, model_name: str) -> None:
        self.model_name = model_name

    # ------------------------------------------------------------------
    # Request serialisation
    # ------------------------------------------------------------------

    def to_wire_request(self, request: RuntimeRequest, *, stream: bool) -> dict[str, Any]:
        """Build the JSON body for ``POST /api/chat``."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.system_prompt}
        ]
        for msg in request.messages:
            messages.append(self._serialise_message(msg))

        payload: dict[str, Any] = {
            "model": request.model_name,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": request.temperature, "top_p": request.top_p},
        }
        thinking = request.thinking
        if getattr(thinking, "enabled", False):
            effort = str(getattr(thinking, "reasoning_effort", "") or "")
            payload["think"] = effort if effort in {"low", "medium", "high"} else True
        if request.tool_schemas:
            payload["tools"] = list(request.tool_schemas)
        if request.max_output_tokens is not None:
            payload["options"]["num_predict"] = request.max_output_tokens
        return payload

    def _serialise_message(self, msg: Message) -> dict[str, Any]:
        if msg.role == "assistant" and msg.tool_calls:
            item = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"function": {"name": tc.tool_name, "arguments": tc.arguments}}
                    for tc in msg.tool_calls
                ],
            }
            if msg.reasoning_content:
                item["thinking"] = msg.reasoning_content
            return item
        if msg.role == "tool":
            # Ollama doesn't require tool_call_id — plain content is enough.
            return {"role": "tool", "content": msg.content or ""}
        item: dict[str, Any] = {"role": msg.role, "content": msg.content or ""}
        if msg.role == "assistant" and msg.reasoning_content:
            item["thinking"] = msg.reasoning_content
        return item

    # ------------------------------------------------------------------
    # Response parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_usage(chunk: dict[str, Any], model: str) -> UsageSnapshot | None:
        """Extract token counts from a ``done=true`` chunk."""
        if not chunk.get("done"):
            return None
        prompt_tokens = chunk.get("prompt_eval_count", 0)
        completion_tokens = chunk.get("eval_count", 0)
        return UsageSnapshot(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated_cost_usd=0.0,   # local model — no cost
            provider="ollama",
            model=model,
        )


# ---------------------------------------------------------------------------
# Retryable error sentinel
# ---------------------------------------------------------------------------

class _RetryableOllamaError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class OllamaModelClient:
    """Native Ollama client with full streaming and tool-call support.

    Parameters
    ----------
    base_url:
        Ollama server root, e.g. ``http://localhost:11434``.
        Defaults to ``OLLAMA_HOST`` env var or ``http://localhost:11434``.
    model_name:
        Model to use (e.g. ``"llama3.1"``, ``"qwen2.5-coder"``).
    timeout_seconds:
        Per-chunk read timeout for streaming responses.
    retries:
        Number of retry attempts on transient errors.
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        model_name: str = "",
        timeout_seconds: float = 120.0,
        retries: int = 3,
        base_delay: float = 0.5,
        jitter: float = 0.2,
        thinking: ThinkingConfig | None = None,
    ) -> None:
        resolved = (base_url or "").strip().rstrip("/")
        if not resolved:
            resolved = (
                environ.get("OLLAMA_HOST", "")
                or environ.get("BASE_URL", "")
                or "http://localhost:11434"
            ).strip().rstrip("/")
        # Accept both bare host and /v1 suffix users may have forgotten to remove.
        if resolved.endswith("/v1"):
            resolved = resolved[:-3]
        self.base_url = resolved
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.base_delay = base_delay
        self.jitter = jitter
        self.thinking = thinking or ThinkingConfig()
        self.adapter = OllamaAdapter(model_name=model_name)
        self._call_counter = 0

    # ------------------------------------------------------------------
    # Public interface (mirrors OpenAICompatibleModelClient)
    # ------------------------------------------------------------------

    async def chat_completion(
        self,
        request: RuntimeRequest,
        *,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Yield :class:`StreamEvent` objects for a single conversational turn.

        Uses Ollama's native ``/api/chat`` endpoint which has better tool-call
        support than the OpenAI-compatible shim.
        """
        if stream:
            last_error: str | None = None
            for attempt in range(self.retries):
                events: list[StreamEvent] = []
                got_retry = False
                async for event in self._stream_ndjson(request):
                    if event.type == StreamEventType.ERROR:
                        err = event.error or ""
                        if _is_transient(err):
                            last_error = err
                            got_retry = True
                            break
                        yield event
                        return
                    events.append(event)
                if not got_retry:
                    for ev in events:
                        yield ev
                    return
                if attempt < self.retries - 1:
                    delay = self.base_delay * (2 ** attempt) + random.uniform(0, self.jitter)
                    await asyncio.sleep(delay)
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error=last_error or "Ollama request failed after retries.",
            )
        else:
            # Non-streaming: single request, wrap result as StreamEvents.
            wire_payload = self.adapter.to_wire_request(request, stream=False)
            try:
                response_json = await call_with_backoff(
                    lambda: asyncio.to_thread(self._post_json, wire_payload),
                    retries=self.retries,
                    base_delay=self.base_delay,
                    jitter=self.jitter,
                    retryable=lambda e: isinstance(e, _RetryableOllamaError),
                )
            except Exception as exc:
                yield StreamEvent(type=StreamEventType.ERROR, error=str(exc))
                return

            msg = response_json.get("message") or {}
            content: str = msg.get("content") or ""
            thinking_content: str = msg.get("thinking") or ""
            raw_tool_calls: list[dict[str, Any]] = msg.get("tool_calls") or []

            if content:
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(content=content),
                )
            for raw_tc in raw_tool_calls:
                tc = self._parse_tool_call(raw_tc)
                if tc is not None:
                    yield StreamEvent(type=StreamEventType.TOOL_CALL_COMPLETE, tool_call=tc)

            usage = self.adapter.parse_usage(response_json, request.model_name)
            done_reason = response_json.get("done_reason") or (
                "tool_calls" if raw_tool_calls else "stop"
            )
            yield StreamEvent(
                type=StreamEventType.MESSAGE_COMPLETE,
                finish_reason=done_reason,
                usage=usage,
                reasoning_content=thinking_content or None,
            )

    # ------------------------------------------------------------------
    # Streaming implementation
    # ------------------------------------------------------------------

    async def _stream_ndjson(self, request: RuntimeRequest) -> AsyncGenerator[StreamEvent, None]:
        """Read Ollama's NDJSON stream and emit :class:`StreamEvent` objects."""
        wire_payload = self.adapter.to_wire_request(request, stream=True)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | Exception | None] = asyncio.Queue()

        def _reader() -> None:
            try:
                body = json.dumps(wire_payload).encode("utf-8")
                req = urllib_request.Request(
                    self._chat_url(),
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    for raw_line in resp:
                        line = raw_line.decode("utf-8").strip()
                        if not line:
                            continue
                        try:
                            loop.call_soon_threadsafe(queue.put_nowait, json.loads(line))
                        except json.JSONDecodeError:
                            pass
            except error.HTTPError as exc:
                details = _http_error_details(exc)
                if exc.code in {408, 409, 429, 500, 502, 503, 504}:
                    loop.call_soon_threadsafe(queue.put_nowait, _RetryableOllamaError(details))
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, RuntimeError(details))
            except error.URLError as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    _RetryableOllamaError(f"Ollama connection failed: {exc.reason}"),
                )
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()

        usage: UsageSnapshot | None = None
        finish_reason: str | None = None
        # Accumulate tool calls — Ollama emits them in a single done=true chunk
        # or in the final message delta.
        pending_tool_calls: list[dict[str, Any]] = []
        thinking_content = ""

        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                yield StreamEvent(type=StreamEventType.ERROR, error=str(item))
                return

            chunk: dict[str, Any] = item
            msg = chunk.get("message") or {}
            content: str = msg.get("content") or ""
            thinking_chunk: str = msg.get("thinking") or ""
            raw_tool_calls: list[dict[str, Any]] = msg.get("tool_calls") or []

            if content:
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(content=content),
                )
            if thinking_chunk:
                thinking_content += thinking_chunk

            if raw_tool_calls:
                pending_tool_calls.extend(raw_tool_calls)

            if chunk.get("done"):
                usage = self.adapter.parse_usage(chunk, request.model_name)
                done_reason = chunk.get("done_reason") or (
                    "tool_calls" if pending_tool_calls else "stop"
                )
                finish_reason = done_reason

        # Emit tool calls after stream ends (Ollama delivers them all at once).
        for raw_tc in pending_tool_calls:
            tc = self._parse_tool_call(raw_tc)
            if tc is not None:
                yield StreamEvent(type=StreamEventType.TOOL_CALL_COMPLETE, tool_call=tc)

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content=thinking_content or None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_tool_call(self, raw: dict[str, Any]) -> ToolCall | None:
        """Convert an Ollama tool-call dict to a :class:`ToolCall`.

        Ollama does **not** provide an ``id`` field — we generate a stable one.
        """
        fn = raw.get("function") or {}
        name: str = fn.get("name", "")
        if not name:
            return None
        arguments = fn.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"_raw": arguments}
        # Generate a short deterministic-looking ID.
        self._call_counter += 1
        call_id = f"ollama-{self._call_counter:04d}"
        return ToolCall(call_id=call_id, tool_name=name, arguments=arguments)

    def _chat_url(self) -> str:
        return f"{self.base_url}/api/chat"

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            self._chat_url(),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            details = _http_error_details(exc)
            if exc.code in {408, 409, 429, 500, 502, 503, 504}:
                raise _RetryableOllamaError(details) from exc
            raise RuntimeError(details) from exc
        except error.URLError as exc:
            raise _RetryableOllamaError(f"Ollama connection failed: {exc.reason}") from exc


def _http_error_details(exc: error.HTTPError) -> str:
    raw = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else ""
    if raw:
        return f"Ollama request failed with HTTP {exc.code}: {raw}"
    return f"Ollama request failed with HTTP {exc.code}."


def _is_transient(error_msg: str) -> bool:
    return any(
        marker in error_msg
        for marker in ("503", "502", "504", "429", "500", "408", "409", "connection failed")
    )


def resolve_ollama_base_url(explicit: str | None = None) -> str:
    """Return the Ollama base URL, stripping any /v1 suffix."""
    url = (explicit or "").strip().rstrip("/")
    if not url:
        url = (
            environ.get("OLLAMA_HOST", "")
            or environ.get("BASE_URL", "")
            or "http://localhost:11434"
        ).strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url
