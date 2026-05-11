# 01-1 — Streaming Responses: Real-Time Token Output

## Prerequisites

Complete [01-agent-loop.md](01-agent-loop.md) first.

*This is a sub-chapter between Chapter 01 and Chapter 02. It can be skipped and revisited after completing Chapter 02 if you want to defer streaming — the rest of the series works without it.*

Chapter 01 built an agent loop where `model_client.complete()` blocks until the model finishes and returns the full response as one string. That works but feels unresponsive — the user stares at a blank terminal until the entire reply is ready.

**Streaming** fixes this. The model sends tokens as it generates them. Users see the answer build word by word.

This chapter upgrades the model client interface to support streaming without changing the core loop's structure.

---

## What you will build

```
agent/
    client.py     ← updated: add stream() method alongside complete()
    events.py     ← updated: AssistantTextDelta carries is_final flag
    agent.py      ← updated: prefer stream() when available, fall back to complete()
main.py           ← updated: renderer prints chunks without newline
```

---

## 1. Why streaming is architecturally separate from the agent loop

The loop cares about **what the model decided** (text or tool call). Streaming is about **how that decision arrives**. These are independent concerns:

```
Without streaming:  complete()  →  full ModelResponse (one shot)
With streaming:     stream()    →  [chunk][chunk][chunk]... ModelResponse (assembled)
```

The loop only needs the final `ModelResponse` to decide what to do next. Streaming is a rendering concern — the REPL shows tokens live. The loop sees the same assembled result either way.

---

## 2. Update `agent/events.py`

Add `is_final` to `AssistantTextDelta` so the renderer knows when the stream ends:

```python
# agent/events.py  — update AssistantTextDelta

@dataclass(slots=True, frozen=True)
class AssistantTextDelta:
    """
    A chunk of assistant text.

    is_final=False  → more chunks are coming (print without newline)
    is_final=True   → last chunk for this turn (print newline after)
    """
    text: str
    is_final: bool = False
```

---

## 3. Add `stream()` to the model client interface

```python
# agent/client.py  — add stream() alongside complete()

from __future__ import annotations
import asyncio
from typing import Any, AsyncIterator
from agent.models import ModelResponse, ToolCall


class ModelClient:
    """
    Base interface for model clients.

    Subclasses must implement complete(). stream() is optional — if not overridden,
    the runtime falls back to complete() and emits one final AssistantTextDelta.
    """

    async def complete(
        self,
        *,
        messages: list[Any],
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> ModelResponse:
        raise NotImplementedError

    async def stream(
        self,
        *,
        messages: list[Any],
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> AsyncIterator[str | ModelResponse]:
        """
        Yield text chunks as they arrive, then a final ModelResponse.

        The contract:
          • yield str items for each arriving text chunk
          • yield exactly one ModelResponse as the last item (contains tool_calls)
          • if the model decides on a tool call, text chunks may be empty

        Default implementation: calls complete() and yields the full text at once.
        """
        response = await self.complete(
            messages=messages, tools=tools, system_prompt=system_prompt
        )
        if response.text:
            yield response.text    # one chunk = entire text
        yield response             # final ModelResponse (possibly with tool_calls)


class DemoModelClient(ModelClient):
    """
    Fake deterministic client for development.
    Simulates streaming by splitting the response into words.
    """

    async def complete(self, *, messages, tools, system_prompt) -> ModelResponse:
        # ...existing implementation unchanged from Chapter 01...
        last = messages[-1] if messages else None
        prompt = last.text.lower().strip() if last else ""

        if "time" in prompt:
            return ModelResponse(tool_calls=[ToolCall(id="tc-001", name="get_time", input={})])
        if "echo" in prompt:
            echo_text = prompt.split("echo", 1)[-1].strip() or "nothing"
            return ModelResponse(tool_calls=[ToolCall(id="tc-002", name="echo", input={"text": echo_text})])
        return ModelResponse(text="I received your message. How can I help further?")

    async def stream(self, *, messages, tools, system_prompt) -> AsyncIterator[str | ModelResponse]:
        """Simulate streaming by yielding words with a tiny delay."""
        response = await self.complete(
            messages=messages, tools=tools, system_prompt=system_prompt
        )
        if response.text:
            words = response.text.split()
            for i, word in enumerate(words):
                await asyncio.sleep(0.05)                  # simulate network latency
                separator = " " if i < len(words) - 1 else ""
                yield word + separator                     # text chunk
        yield response                                     # final ModelResponse
```

---

## 4. Update `Agent.run()` to prefer streaming

```python
# agent/agent.py  — update the model call section of run()

from agent.events import AssistantTextDelta
from agent.models import ModelResponse

class Agent:
    # ...existing init unchanged...

    async def run(self, user_text: str):
        self.messages.append(Message.user(user_text))
        self._turn_count += 1
        # ...hooks, status event...

        system_prompt = self._build_system_prompt(user_text=user_text)

        while True:
            response, text_chunks = await self._call_model_with_stream(
                system_prompt=system_prompt,
            )

            # Emit the text chunks (already yielded live during streaming call)
            # Only emit a final delta if stream() was not used
            if not text_chunks and response.text:
                self.messages.append(Message.assistant(response.text))
                yield AssistantTextDelta(text=response.text, is_final=True)
            elif text_chunks:
                # Text was streamed — store the assembled result
                assembled = "".join(text_chunks)
                if assembled:
                    self.messages.append(Message.assistant(assembled))

            if not response.wants_tool:
                # ...stop hook, return...
                return

            # ...tool execution unchanged...

    async def _call_model_with_stream(
        self, system_prompt: str
    ) -> tuple[ModelResponse, list[str]]:
        """
        Call the model, streaming if supported.

        Returns (ModelResponse, list_of_chunks).
        If the client does not override stream(), list_of_chunks is empty
        and the caller falls back to emitting one AssistantTextDelta.
        """
        chunks: list[str] = []
        response: ModelResponse | None = None

        try:
            async for item in self.model_client.stream(
                messages=self.messages,
                tools=self.tool_registry.schemas(),
                system_prompt=system_prompt,
            ):
                if isinstance(item, str):
                    chunks.append(item)
                    # Yield streaming delta immediately to REPL
                    # We use a nested approach: the generator must yield here
                    # See note below on how to integrate this
                elif isinstance(item, ModelResponse):
                    response = item
        except Exception as exc:
            raise

        return response, chunks
```

**The streaming yield problem:** `_call_model_with_stream` cannot `yield` to the REPL because it is a regular `async def`, not an async generator. The cleanest solution is to inline the streaming loop directly inside `run()`:

```python
# agent/agent.py  — cleaner inline streaming in run()

async def run(self, user_text: str):
    self.messages.append(Message.user(user_text))
    self._turn_count += 1
    # ...hooks...
    system_prompt = self._build_system_prompt(user_text=user_text)

    while True:
        # ── Streaming model call ─────────────────────────────────────────
        chunks: list[str] = []
        response: ModelResponse | None = None

        try:
            async for item in self.model_client.stream(
                messages=self.messages,
                tools=self.tool_registry.schemas(),
                system_prompt=system_prompt,
            ):
                if isinstance(item, str):
                    chunks.append(item)
                    yield AssistantTextDelta(text=item, is_final=False)  # live chunk
                elif isinstance(item, ModelResponse):
                    response = item
        except Exception as exc:
            yield ErrorEvent(message="Model call failed.", details=str(exc))
            return

        # Signal end of stream
        if chunks:
            assembled = "".join(chunks)
            self.messages.append(Message.assistant(assembled))
            yield AssistantTextDelta(text="", is_final=True)   # end-of-stream signal
        elif response and response.text:
            self.messages.append(Message.assistant(response.text))
            yield AssistantTextDelta(text=response.text, is_final=True)

        if not response or not response.wants_tool:
            # ...stop hook...
            return

        # ...tool execution unchanged from Chapter 01...
```

---

## 5. Update the REPL renderer for streaming

```python
# main.py  — updated render() for streaming

import sys

async def render(event: object) -> None:
    if isinstance(event, StatusEvent):
        print(f"  · {event.message}")

    elif isinstance(event, AssistantTextDelta):
        if event.is_final and not event.text:
            # End-of-stream signal — move to next line
            print()                       # newline after streamed content
        elif event.is_final:
            # Last chunk received from non-streaming complete()
            print(f"\nagent> {event.text}\n")
        else:
            # Mid-stream chunk — print inline without newline
            if not _streaming_started():
                sys.stdout.write("\nagent> ")  # prefix only on first chunk
            sys.stdout.write(event.text)
            sys.stdout.flush()             # force immediate display

    elif isinstance(event, ToolExecutionStarted):
        print(f"\n  ⚙ {event.tool_name}({_fmt_args(event.tool_input)})")

    elif isinstance(event, ToolExecutionCompleted):
        icon = "✗" if event.is_error else "✓"
        print(f"  {icon} {event.tool_name} → {event.output[:80]}")

    elif isinstance(event, ErrorEvent):
        print(f"\n[ERROR] {event.message}")


_saw_first_chunk = False

def _streaming_started() -> bool:
    global _saw_first_chunk
    if _saw_first_chunk:
        return True
    _saw_first_chunk = True
    return False

def _reset_stream_state():
    global _saw_first_chunk
    _saw_first_chunk = False

# Call _reset_stream_state() at the start of each repl() iteration
```

---

## 6. The real OpenAI streaming client

When you replace `DemoModelClient` with a real OpenAI client, the `stream()` method maps directly to the `stream=True` parameter:

```python
# agent/openai_client.py  — streaming OpenAI client

from openai import AsyncOpenAI
from agent.models import ModelResponse, ToolCall
from agent.client import ModelClient


class OpenAIStreamingClient(ModelClient):
    def __init__(self, api_key: str, model: str = "gpt-4o") -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def complete(self, *, messages, tools, system_prompt) -> ModelResponse:
        """Non-streaming fallback."""
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=self._to_openai_messages(messages, system_prompt),
            tools=self._to_openai_tools(tools) or None,
        )
        return self._parse_response(resp.choices[0].message)

    async def stream(self, *, messages, tools, system_prompt):
        """Streaming call — yields text chunks then a final ModelResponse."""
        tool_calls_acc: dict[int, dict] = {}
        text_acc: list[str] = []

        async with self._client.chat.completions.stream(
            model=self._model,
            messages=self._to_openai_messages(messages, system_prompt),
            tools=self._to_openai_tools(tools) or None,
        ) as stream:
            async for event in stream:
                for choice in (event.choices or []):
                    delta = choice.delta

                    # Text chunk
                    if delta.content:
                        text_acc.append(delta.content)
                        yield delta.content         # ← live chunk to REPL

                    # Tool call accumulation
                    for tc in (delta.tool_calls or []):
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {"id": tc.id, "name": "", "args": ""}
                        if tc.function.name:
                            tool_calls_acc[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_acc[idx]["args"] += tc.function.arguments

        # Assemble final ModelResponse
        import json
        tool_calls = [
            ToolCall(id=tc["id"], name=tc["name"], input=json.loads(tc["args"] or "{}"))
            for tc in tool_calls_acc.values()
        ]
        yield ModelResponse(text="".join(text_acc), tool_calls=tool_calls)

    def _to_openai_messages(self, messages, system_prompt):
        result = [{"role": "system", "content": system_prompt}]
        for m in messages:
            # ...convert Message objects to OpenAI wire format (same as Chapter 02)...
        return result

    def _to_openai_tools(self, tools):
        return [{"type": "function", "function": t} for t in tools] if tools else []

    def _parse_response(self, msg) -> ModelResponse:
        # Non-streaming parse (same as Chapter 02's adapter)
        import json
        text = msg.content or ""
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, input=json.loads(tc.function.arguments))
            for tc in (msg.tool_calls or [])
        ]
        return ModelResponse(text=text, tool_calls=tool_calls)
```

---

## 7. Common mistakes

### Mistake 1 — Forgetting the end-of-stream newline

```python
# WRONG — cursor stays at end of last token, next print lands on same line
sys.stdout.write(chunk)
sys.stdout.flush()
# missing: print() at end of stream
```

**Fix:** yield one `AssistantTextDelta(text="", is_final=True)` at the end; the renderer calls `print()`.

### Mistake 2 — Storing chunks but not the assembled text

```python
# WRONG — model cannot refer to its own previous streamed response
for chunk in stream:
    yield AssistantTextDelta(text=chunk)
# missing: messages.append(Message.assistant("".join(chunks)))
```

**Fix:** always assemble chunks into one string and append to `self.messages`.

### Mistake 3 — Mixing streaming and tool calls

Tool calls arrive in `ModelResponse.tool_calls`, not as streamed text. The streaming chunks contain only the text portion of the response. If the model calls a tool *and* produces text (thinking aloud), both can arrive — handle them separately.

---

## 8. Checklist before moving on

- [ ] `ModelClient.stream()` is defined as a default that calls `complete()` + yields result
- [ ] `DemoModelClient.stream()` simulates streaming by yielding words with a delay
- [ ] `AssistantTextDelta` has `is_final: bool` flag
- [ ] `Agent.run()` inlines the streaming loop and yields deltas as chunks arrive
- [ ] Assembled text is stored in `self.messages` after streaming completes
- [ ] The REPL renderer prints mid-stream chunks without newlines using `sys.stdout.write()`
- [ ] A final `print()` is called after the stream ends
- [ ] `OpenAIStreamingClient.stream()` accumulates tool call fragments across delta events

---

Next: [02-tools.md](02-tools.md)

*After completing Chapter 02, continue to [02-1-mcp-integration.md](02-1-mcp-integration.md) to connect external tool servers.*

