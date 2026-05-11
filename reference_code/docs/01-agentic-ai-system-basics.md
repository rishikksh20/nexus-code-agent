# 01. Agentic AI System Basics: Flow, Streaming, Async, and Response Handling

This guide explains the **top-level code outside the `basic/` directory** in a logical build-up order.

The goal is to help you understand how this small agent-style AI system works from end to end:

1. where execution starts,
2. how a request is prepared,
3. how the OpenAI-compatible client is created,
4. how streaming and non-streaming responses differ,
5. why `async` is used,
6. how `@dataclass` is used to model events,
7. and how the final output printed by `uv run main.py` maps back to the code.

---

## 1. Scope of this guide

This document covers these files:

- `main.py`
- `core/client/llm_client.py`
- `core/client/datatype.py`
- `pyproject.toml`

It does **not** cover code inside `basic/`.

---

## 2. What this code is trying to do

At a high level, this project is building a small foundation for an **agentic AI system**.

Even though this top-level code is still minimal, it already contains the core pieces that many agent systems need:

- an **entry point** (`main.py`),
- an **LLM client wrapper** (`LLMClient`),
- a **standard internal event format** (`StreamEvent`, `TextDelta`, `TokenUsage`),
- support for both **streaming** and **non-streaming** model responses,
- and **retry handling** for API failures.

This is important in agentic systems because you usually do not want application code to depend directly on raw SDK responses. Instead, you want:

- one place that talks to the model provider,
- one format that the rest of your system understands,
- and one predictable flow for partial outputs, final outputs, and errors.

That is exactly what this code begins to establish.

---

## 3. Architecture and core concepts

Before going deeper into each file, it helps to see the system as a **small AI runtime pipeline** rather than just a few Python functions.

This code is not yet a full autonomous agent with planning, memory, and tools, but it already shows the **core architectural pattern** used by many agentic AI systems:

- an application layer asks for work,
- a client layer talks to the model provider,
- a response layer converts provider output into internal events,
- and the caller consumes those events in a consistent way.

In other words, the bigger picture is not just “call the API and print the answer.”
It is actually:

1. define a request format,
2. send the request through a model client,
3. normalize provider responses into your own data model,
4. expose those results as events,
5. let the rest of the application react to those events.

That architecture is important because agent systems usually become complex very quickly. Once you add tools, memory, retries, guardrails, and UI updates, a direct “raw SDK everywhere” approach becomes hard to maintain.

### 3.1 System layers and responsibilities

You can understand this project as four simple layers.

#### Layer 1: application layer
This is `main.py`.

Its job is to:

- decide what messages to send,
- choose whether to stream,
- consume events,
- and decide how to present output.

This layer should stay simple. It should not need to know the low-level details of the provider SDK.

#### Layer 2: model access layer
This is `LLMClient` in `core/client/llm_client.py`.

Its job is to:

- create the provider client,
- choose the model,
- send requests,
- retry on transient failures,
- and translate provider responses into internal application events.

This layer acts like a boundary between your application and the external AI provider.

#### Layer 3: internal event/data contract
This is `TextDelta`, `TokenUsage`, `EventType`, and `StreamEvent` in `core/client/datatype.py`.

Its job is to define the **language your own application speaks internally**.

Instead of exposing raw provider chunk objects everywhere, the code converts them into a stable internal shape. That is a foundational idea in larger AI systems.

#### Layer 4: provider layer
This is the actual remote model service, accessed through `AsyncOpenAI` with:

- an API key,
- a `base_url`,
- a model name,
- and a chat-completions request.

Even though the Python SDK is from `openai`, the remote endpoint is Mistral through an OpenAI-compatible interface.

### 3.2 Request lifecycle and event flow

At the conceptual level, one request flows like this:

`user input` → `message list` → `LLMClient` → `provider API` → `raw response/chunks` → `StreamEvent` objects → `caller/UI/logging`

That flow is more important than any single method in the file, because it shows the real architecture.

The key design choice is that the rest of the app consumes **events**, not provider-specific objects.

That gives you flexibility later to:

- swap providers,
- add logging,
- stream to a terminal or web UI,
- store usage metrics,
- or insert tool-calling logic between events.

### 3.3 Core concepts behind this architecture

#### Concept 1: provider abstraction
`LLMClient` hides the provider-specific SDK call details. This means application code does not need to know how `chat.completions.create(...)` works internally.

#### Concept 2: event-driven output
The system exposes output as `StreamEvent` objects. This is a natural fit for AI applications because generation is often progressive, not all-at-once.

#### Concept 3: unified response model
Streaming and non-streaming are different at the API level, but this project gives them a common output contract. That makes downstream code simpler.

#### Concept 4: async-first thinking
LLM systems spend a lot of time waiting on network and streamed tokens. Async is therefore not just a Python feature here; it is part of the architecture.

#### Concept 5: observability and control
The inclusion of `TokenUsage`, `finish_reason`, and explicit error events shows that the system is designed not only to get text back, but also to understand how a request ended and what it cost.

### 3.4 Why this bigger picture matters in agentic AI

In a true agentic system, a single user request may later involve:

- multiple model calls,
- tool invocations,
- retries,
- intermediate reasoning steps,
- memory lookups,
- and UI updates while work is in progress.

The architecture in this project is valuable because it already points in that direction.

It separates:

- **what the app wants to do**,
- **how the provider is called**,
- and **how results are represented internally**.

That separation is one of the main building blocks of maintainable AI systems.

---

## 4. Project structure in simple words

### `main.py`
This is the runner. It creates a client, prepares messages, calls the model, and prints events.

### `core/client/llm_client.py`
This is the OpenAI-compatible wrapper around the external LLM API. It knows:

- how to create the SDK client,
- which model to call,
- how to send messages,
- how to handle streaming and non-streaming responses,
- and how to convert provider responses into internal `StreamEvent` objects.

### `core/client/datatype.py`
This defines the internal data structures used by the system:

- `TextDelta`
- `EventType`
- `TokenUsage`
- `StreamEvent`

These act like the common language of the app.

### `pyproject.toml`
This shows the project dependency setup. Right now the key dependency is:

- `openai>=2.33.0`

Even though the SDK package name is `openai`, the code points it to a **Mistral** endpoint by changing `base_url`.

---

## 5. The execution flow from start to finish

Here is the complete runtime flow in one quick view:

1. `uv run main.py` starts the program.
2. Python runs `asyncio.run(main())`.
3. `main()` creates an `LLMClient`.
4. `main()` builds the chat message list.
5. `main()` calls `client.chat_completion(...)`.
6. `LLMClient.chat_completion()` creates or reuses an `AsyncOpenAI` client.
7. It prepares request arguments like model name, messages, and stream mode.
8. It tries the request with retry logic.
9. If `stream=True`, it yields partial `TEXT_DELTA` events as chunks arrive.
10. At the end, it yields one `MESSAGE_COMPLETE` event.
11. If `stream=False`, it waits for the whole response and yields one `MESSAGE_COMPLETE` event.
12. `main.py` prints each event.
13. Finally, `Done` is printed.

You can think of the system like this:

`main.py` → `LLMClient` → external LLM API → raw SDK response → internal `StreamEvent` objects → terminal output

---

## 6. Step-by-step walkthrough of `main.py`

Current code:

- imports `asyncio`
- imports `LLMClient`
- defines an async `main()` function
- creates a message list
- loops over events returned by `chat_completion`
- prints them

### Key parts

#### 5.1 `async def main()`
`main()` is asynchronous because the model call is network I/O. The program must wait for a remote API response, so using async allows it to pause efficiently instead of blocking the whole execution model.

#### 5.2 Creating the client
```python
client = LLMClient()
```
This creates a lightweight wrapper object. At this point the real SDK client is not yet created. That happens lazily later in `get_client()`.

#### 5.3 Preparing messages
```python
messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the capital of France?"},
]
```
This is a standard chat-style message format used by OpenAI-compatible APIs.

- `system` sets behavior/instructions
- `user` contains the actual prompt

#### 5.4 Consuming the response
```python
async for event in client.chat_completion(message=messages, stream=True):
    print(event)
```
This is very important.

`chat_completion()` does **not** return plain text. It returns an **async generator** that yields `StreamEvent` objects.

That means the caller can process the response progressively.

If streaming is enabled, multiple events may arrive.
If streaming is disabled, usually only one final event arrives.

#### 5.5 Program startup
```python
asyncio.run(main())
```
This creates the event loop, runs `main()`, and closes the loop when done.

---

## 7. Why `async` is used here

This is one of the most important design choices in the code.

### 6.1 LLM calls are network-bound
The program is calling a remote model API over HTTP. That means there is waiting involved:

- waiting for connection,
- waiting for headers,
- waiting for tokens/chunks,
- waiting for the full response.

If you use synchronous code, the current thread waits in a blocking way.
If you use asynchronous code, the function can yield control while waiting.

### 6.2 Why this matters for agent systems
In a real agentic application, async becomes even more valuable because you may want to:

- stream model output to the UI,
- call tools,
- do multiple API calls,
- handle timeouts,
- process events in real time,
- or run concurrent tasks.

Async makes these patterns much easier and more efficient.

### 6.3 Where async appears in this code

#### `async def main()`
The app entry function is async.

#### `async def close()`
Closing the SDK client may involve async cleanup.

#### `async def chat_completion(...)`
The main model request method is async because it performs network requests.

#### `async def _stream_response(...)`
Streaming requires asynchronously iterating over arriving chunks.

#### `async def _non_stream_response(...)`
Even a non-streaming API call still needs async because it waits for the server response.

#### `async for ...`
This is used twice:

- once by your app to consume events,
- once inside `_stream_response()` to consume streamed API chunks.

### 6.4 Why `AsyncGenerator` is a good fit
`chat_completion()` is typed as an `AsyncGenerator[StreamEvent, None]`.

That means:

- it is asynchronous,
- it can yield many values over time,
- and each value is a `StreamEvent`.

This is a strong design for agentic systems because events are a natural way to model progress.

---

## 8. Understanding `@dataclass` in this project

The code uses `@dataclass` to create lightweight data containers.

A dataclass automatically gives you useful behavior such as:

- an `__init__` method,
- a readable `__repr__`,
- value storage,
- and easier object construction.

That is why when you print a `StreamEvent`, Python shows a helpful structured output like:

```python
StreamEvent(type=<EventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content='The'), error=None, finish_reason=None, usage=None)
```

Without dataclasses, you would usually need to write more boilerplate code by hand.

### 7.1 `TextDelta`
```python
@dataclass
class TextDelta:
    content: str
```
This is a tiny wrapper around a piece of generated text.

In streaming mode, each chunk of text becomes a `TextDelta`.

Example:

- `"The"`
- `" capital of France"`
- `" is **Paris"`

These are partial pieces of the full answer.

It also defines:
```python
def __str__(self):
    return self.content
```
So if you print a `TextDelta` directly, you get just its text content.

### 7.2 `TokenUsage`
```python
@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0
```
This stores token accounting information returned by the provider.

Why it matters:

- cost tracking,
- monitoring,
- debugging,
- optimization.

It also defines `__add__`, so usage from multiple calls could be added together later.
That is useful in agent systems where a single user request may trigger several model calls.

### 7.3 `StreamEvent`
```python
@dataclass
class StreamEvent:
    type: EventType
    text_delta: TextDelta | None = None
    error: str | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None
```
This is the central event object.

It standardizes all outputs into one shape.

Depending on the situation, a `StreamEvent` may represent:

- a text chunk,
- a completed message,
- or an error.

This is a very common agent-system design pattern: instead of returning raw provider objects, you return your own internal event model.

### 7.4 `EventType`
`EventType` defines the event categories:

- `TEXT_DELTA`
- `MESSAGE_COMPLETE`
- `ERROR`

This lets the rest of the app react to events by type instead of guessing from raw payloads.

Example mental model:

- `TEXT_DELTA` → "new streamed text arrived"
- `MESSAGE_COMPLETE` → "generation finished"
- `ERROR` → "the request failed"

---

## 9. The OpenAI SDK usage in this code

The project depends on:

```toml
openai>=2.33.0
```

In `llm_client.py`, the code uses:

```python
from openai import AsyncOpenAI
```

### 8.1 Why `AsyncOpenAI` is used
This is the async version of the client. It matches the async design of the rest of the app.

### 8.2 Why this is called “OpenAI-compatible”
The code uses the `openai` Python SDK, but the `base_url` is configured as:

```python
base_url="https://api.mistral.ai/v1"
```

So the project is using the OpenAI client interface to talk to a provider that exposes an OpenAI-compatible API surface.

This is a powerful pattern because it gives you:

- a familiar SDK,
- minimal code changes,
- portability across compatible providers.

### 8.3 How the client object is created
Inside `get_client()`:

```python
self._client = AsyncOpenAI(
    api_key="...",
    base_url="https://api.mistral.ai/v1",
)
```

This method lazily creates the client only when needed.

Benefits of lazy initialization:

- no connection setup until required,
- one reusable client instance,
- cleaner object lifecycle.

### 8.4 The actual model request
The important request is:

```python
response = await client.chat.completions.create(**kwargs)
```

This sends a chat completion request to the configured provider.

The `kwargs` contain:

- `model`
- `messages`
- `stream`

In this code:

```python
kwargs = {
    "model": "mistral-medium-latest",
    "messages": message,
    "stream": stream,
}
```

---

## 10. `LLMClient` explained in sequence

`LLMClient` is the heart of this top-level code.

---

### 9.1 `__init__`
```python
def __init__(self) -> None:
    self._client: AsyncOpenAI | None = None
    self._max_attempts = 3
```

It stores:

- `_client`: the cached SDK client
- `_max_attempts`: retry limit

This means the wrapper is designed not just to call the API, but also to handle resilience.

---

### 9.2 `get_client()`
This method creates the SDK client once and reuses it.

Flow:

1. If `_client` is `None`, create a new `AsyncOpenAI` client.
2. Otherwise, return the existing client.

This avoids recreating the SDK object for every request.

---

### 9.3 `close()`
This method closes the underlying async client.

That matters because network clients often maintain resources such as:

- open connections,
- connection pools,
- HTTP session state.

Even though `main.py` does not call `close()` yet, it is an important lifecycle method for a production-grade app.

---

### 9.4 `chat_completion()`
This is the orchestration method.

Signature:

```python
async def chat_completion(self, message: list[dict[str, Any]], stream: bool = True) -> AsyncGenerator[StreamEvent, None]
```

This says:

- input: a list of chat messages,
- option: streaming on/off,
- output: an async generator of `StreamEvent` objects.

#### What it does internally

1. Gets the client.
2. Builds the request arguments.
3. Runs a retry loop.
4. Branches into streaming or non-streaming path.
5. Converts provider output into internal events.
6. Yields those events back to the caller.

This method is important because it hides provider-specific details from the rest of the app.

---

## 11. Streaming path vs non-streaming path

This is the most important conceptual difference in the code.

---

### 10.1 When `stream=True`
The code enters:

```python
async for event in self._stream_response(client, kwargs):
    yield event
```

That means the caller receives output incrementally.

#### What happens in `_stream_response()`

1. It sends the request with `stream=True`.
2. The provider returns an async stream of chunks.
3. The code loops through each chunk.
4. If a chunk contains text content, it yields a `TEXT_DELTA` event.
5. It tracks `finish_reason` when available.
6. It collects usage if present.
7. When the stream ends, it yields one final `MESSAGE_COMPLETE` event.

#### Why streaming is useful
Streaming is useful when you want:

- lower perceived latency,
- live UI updates,
- progress visibility,
- more interactive user experience,
- event-driven agent pipelines.

The user starts seeing output before the entire generation is complete.

#### What the caller sees
Instead of receiving one big answer, the caller sees multiple smaller events.

Example:

- `The`
- ` capital of France`
- ` is **Paris`
- `**.`
- ` 🇫`
- `🇷`
- `✨`
- final completion event

That is why your streamed sample output prints many `TEXT_DELTA` lines before the final `MESSAGE_COMPLETE` line.

---

### 10.2 When `stream=False`
The code enters:

```python
event = await self._non_stream_response(client, kwargs)
yield event
```

Here the code waits until the full model response is ready.

#### What happens in `_non_stream_response()`

1. It sends the request with `stream=False`.
2. The provider returns a complete response object.
3. The code extracts the first choice.
4. It reads `choice.message.content`.
5. It converts that full text into a `TextDelta`.
6. It collects usage metadata.
7. It returns one `MESSAGE_COMPLETE` event.

#### Why non-streaming is useful
Non-streaming is useful when you want:

- simple control flow,
- a single final value,
- easier post-processing,
- batch-style workflows,
- or you do not need live updates.

#### What the caller sees
Usually only one event is printed:

- one `MESSAGE_COMPLETE` containing the whole generated answer

---

## 12. Why the project uses one event format for both modes

This is a very good design idea.

Even though streaming and non-streaming work differently at the provider level, the rest of the app can consume both through the same internal abstraction: `StreamEvent`.

That means downstream code can do things like:

- inspect `event.type`,
- append `event.text_delta.content` when present,
- react to completion,
- handle usage,
- display errors.

Without caring too much about provider-specific response structures.

This is exactly the kind of abstraction that becomes valuable as an AI agent grows more complex.

---

## 13. Request and response flow in detail

Let us trace one request step by step.

### 12.1 Input from `main.py`
`main.py` provides:

```python
[
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "What is the capital of France?"},
]
```

### 12.2 `chat_completion()` packages it
The client wrapper prepares:

- `model="mistral-medium-latest"`
- `messages=[...]`
- `stream=True` or `False`

### 12.3 SDK sends HTTP request
`AsyncOpenAI` sends the request to:

- `https://api.mistral.ai/v1`

using the OpenAI-compatible chat completions API.

### 12.4 Provider sends raw response
The provider returns either:

- a streamed sequence of chunks, or
- one completed response object.

### 12.5 Wrapper converts raw provider output into internal objects
The wrapper does not expose the raw SDK object directly.
Instead it converts information into:

- `TextDelta`
- `TokenUsage`
- `StreamEvent`

### 12.6 `main.py` prints those internal objects
That is why terminal output is showing `StreamEvent(...)` rather than raw JSON or raw SDK types.

---

## 14. Understanding the streamed example output

Given output for `stream=True`:

```text
rishikesh@pop-os:~/dev/exp/ai-agent-from-scratch$ uv run main.py
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content='The'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content=' capital of France'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content=' is **Paris'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content='**.'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content=' 🇫'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content='🇷'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content='✨'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.MESSAGE_COMPLETE: 'message_complete'>, text_delta=None, error=None, finish_reason='stop', usage=TokenUsage(prompt_tokens=18, completion_tokens=19, total_tokens=37, cache_tokens=0))
Done
```

### What this means line by line

#### First seven `TEXT_DELTA` events
These are partial chunks coming from `_stream_response()`.

For each chunk:

- `type=<EventType.TEXT_DELTA: 'text_delta'>` means this event contains newly arrived text.
- `text_delta=TextDelta(content='...')` is the newly generated piece.
- `finish_reason=None` means generation is not marked complete yet.
- `usage=None` means token usage is not being emitted on those earlier chunk events.

If you concatenate the text pieces, you reconstruct the answer:

`The` + ` capital of France` + ` is **Paris` + `**.` + ` 🇫` + `🇷` + `✨`

Combined result:

`The capital of France is **Paris**. 🇫🇷✨`

#### Final `MESSAGE_COMPLETE` event
This marks the end of the response stream.

- `type=<EventType.MESSAGE_COMPLETE: 'message_complete'>` means generation finished.
- `finish_reason='stop'` means the model stopped normally.
- `usage=TokenUsage(...)` gives token counts.

Notice that this final event does **not** contain the full text in `text_delta`.
In the current implementation, the final streaming event carries completion metadata, not the reconstructed full message.

So if an app wants the final text in streaming mode, it usually needs to accumulate all previous `TEXT_DELTA` values itself.

That is a very common streaming design.

---

## 15. Understanding the non-streamed example output

Given output for `stream=False`:

```text
rishikesh@pop-os:~/dev/exp/ai-agent-from-scratch$ uv run main.py
StreamEvent(type=<StreamEventType.MESSAGE_COMPLETE: 'message_complete'>, text_delta=TextDelta(content='The capital of France is **Paris**. 🇫🇷✨\n\nWould you like to know more about Paris or France?'), error=None, finish_reason='stop', usage=TokenUsage(prompt_tokens=18, completion_tokens=31, total_tokens=49, cache_tokens=0))
Done
```

### What this means
Only one event is printed because the full answer is returned at once.

- `type=<EventType.MESSAGE_COMPLETE: 'message_complete'>` means there are no incremental chunks; this is the completed answer.
- `text_delta=TextDelta(content='...')` contains the entire assistant response.
- `finish_reason='stop'` means the model stopped normally.
- `usage=TokenUsage(...)` gives token counts.

### Key contrast with streaming mode
In non-streaming mode:

- the app gets the whole text in one shot,
- no incremental text events are printed,
- token usage arrives together with the final message,
- control flow is simpler.

---

## 16. Retry and error handling

Inside `chat_completion()`, the code retries on API-related exceptions.

Handled exceptions include:

- `RateLimitError`
- `APIConnectionError`
- `APIError`

The retry strategy uses:

```python
await asyncio.sleep(2 ** attempt)
```

That is exponential backoff.

### Why exponential backoff is useful
If the API is overloaded or temporarily unavailable, immediate retry is often a bad idea. Backoff helps by spacing out retries:

- attempt 0 → wait 1 second
- attempt 1 → wait 2 seconds
- attempt 2 → wait 4 seconds

This improves resilience and reduces pressure on the service.

### How errors are surfaced
If retries are exhausted, the code yields:

```python
StreamEvent(type=StreamEventType.ERROR, error="...")
```

This is good because errors are expressed in the same event channel as normal outputs.

That means the caller can handle everything through one interface.

---

## 17. Why this matters for agentic AI systems

Even though the code is small, it already demonstrates several core ideas used in real agentic systems.

### 17.1 Separation of concerns
- `main.py` handles application flow.
- `LLMClient` handles provider communication.
- dataclasses handle internal data structure.

### 17.2 Internal event abstraction
The rest of the app works with `StreamEventType`, not raw provider payloads.

### 17.3 Streaming support
Agentic apps often need live partial output for:

- chat UIs,
- tool execution updates,
- long-running reasoning flows,
- user feedback loops.

### 17.4 Async-first design
Agent workflows often involve many waiting points. Async keeps the architecture ready for growth.

### 17.5 Usage tracking
Token usage becomes important when building production AI systems.

---

## 18. Mental model: how to think about this code

A good way to think about this code is:

### Layer 1: application layer
`main.py`

This decides what to ask and what to do with emitted events.

### Layer 2: model access layer
`LLMClient`

This isolates all provider communication details.

### Layer 3: internal data contract
`StreamEvent`, `TextDelta`, `TokenUsage`, `StreamEventType`

These create a stable format the rest of the system can trust.

### Layer 4: provider API
Mistral endpoint exposed through an OpenAI-compatible SDK interface.

This layered design is a strong starting point for building larger agentic systems.

---

## 19. A compact flow summary

### Streaming mode

1. send request
2. receive chunks
3. convert each chunk into `TEXT_DELTA`
4. yield events one by one
5. emit final `MESSAGE_COMPLETE`

### Non-streaming mode

1. send request
2. wait for full result
3. convert full result into one `MESSAGE_COMPLETE`
4. yield the final event

---

## 20. Practical takeaways

If you are learning from this code, the main ideas to remember are:

- `async` is used because model calls are network I/O.
- `AsyncGenerator` is used because responses may arrive over time.
- `@dataclass` is used to make internal event objects simple and readable.
- streaming gives partial text early; non-streaming gives the full text once.
- the OpenAI Python SDK can be used with compatible providers by changing `base_url`.
- wrapping provider responses into your own event types is a strong design for agent systems.

---

## 21. Final summary

This top-level code is a small but useful foundation for an agentic AI architecture.

It already demonstrates the key ingredients of a real-world AI system:

- a clear entry point,
- a reusable LLM client wrapper,
- async network handling,
- streaming and non-streaming response support,
- retry logic,
- token usage tracking,
- and structured internal event models.

If you continue building this system, these same ideas will scale into more advanced agent patterns such as:

- tool calling,
- memory,
- planning,
- multi-step workflows,
- UI updates,
- and orchestration across multiple model calls.

So even though the current code is small, the design direction is already very much aligned with how agentic AI systems are commonly structured.

---

## 22. Beginner Coding Guide: hands-on examples

This section gives you small, standalone, copy-pasteable Python examples for every key concept in this project.

You do not need to run the full project to understand these. You can paste each example into a Python file or a REPL and run it immediately.

---

### 22.1 Python `@dataclass` basics

In the project, `StreamEvent`, `TextDelta`, and `TokenUsage` are all dataclasses.

Without a dataclass you would write a class manually like this:

```python
# Without @dataclass — lots of repetitive code
class TextDelta:
    def __init__(self, content: str):
        self.content = content

    def __repr__(self):
        return f"TextDelta(content={self.content!r})"
```

With a dataclass, Python writes the same boilerplate automatically:

```python
from dataclasses import dataclass

# With @dataclass — clean and short
@dataclass
class TextDelta:
    content: str
```

Both are equivalent. But the second version is far shorter and more readable.

**Try it now** — paste into any `.py` file and run it:

```python
from dataclasses import dataclass

@dataclass
class TextDelta:
    content: str

chunk = TextDelta(content="Hello world")

print(chunk)          # TextDelta(content='Hello world')
print(chunk.content)  # Hello world
print(type(chunk))    # <class '__main__.TextDelta'>
```

**What you will see:**

```text
TextDelta(content='Hello world')
Hello world
<class '__main__.TextDelta'>
```

Now do the same for `StreamEvent`. Notice how multiple fields with optional values work:

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class TextDelta:
    content: str

@dataclass
class StreamEvent:
    type: str                       # required field
    text_delta: Optional[TextDelta] = None  # optional, default None
    error: Optional[str] = None     # optional, default None
    finish_reason: Optional[str] = None

# Creating a text chunk event
chunk_event = StreamEvent(
    type="text_delta",
    text_delta=TextDelta(content="The capital")
)
print(chunk_event)

# Creating a completion event
done_event = StreamEvent(
    type="message_complete",
    finish_reason="stop"
)
print(done_event)
```

**What you will see:**

```text
StreamEvent(type='text_delta', text_delta=TextDelta(content='The capital'), error=None, finish_reason=None)
StreamEvent(type='message_complete', text_delta=None, error=None, finish_reason='stop')
```

That is everything the project does — just wrapping raw provider chunks into structured objects.

---

### 22.2 Python `enum` basics (what `EventType` is)

`EventType` is an `Enum`. It is a way of naming fixed categories so you never mistype a string.

```python
from enum import Enum

class StreamEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    MESSAGE_COMPLETE = "message_complete"
    ERROR = "error"

# Using the enum
event_type = StreamEventType.TEXT_DELTA

print(event_type)           # StreamEventType.TEXT_DELTA
print(event_type.value)     # text_delta
print(event_type == "text_delta")  # True  ← because it inherits from str
```

Without an enum you might check strings directly like `if type == "text_delta"` which is error-prone.
With an enum you do `if event.type == StreamEventType.TEXT_DELTA` which is clear and safe.

---

### 22.3 `async` and `await` basics

`async` and `await` are Python's way of doing non-blocking I/O.

Think of it like waiting for a restaurant order:

- **synchronous**: you stand at the counter doing nothing until your food arrives.
- **asynchronous**: you sit down, and while your food is being prepared, you can read the menu, talk, or do something else.

Here is the simplest possible example:

```python
import asyncio

async def fetch_answer():
    # Simulating waiting for an API response
    print("Sending request...")
    await asyncio.sleep(1)  # imagine this is a network call
    print("Response arrived!")
    return "The capital of France is Paris."

async def main():
    answer = await fetch_answer()
    print(answer)

asyncio.run(main())
```

**What you will see:**

```text
Sending request...
Response arrived!
The capital of France is Paris.
```

- `async def` marks a function as asynchronous.
- `await` pauses execution of the current function until the awaited thing is done.
- `asyncio.run(main())` starts the event loop and runs your async code.

**Why the project uses this:**
The model API is a real network call that can take 0.5–5 seconds. `await` lets the program pause and not block while waiting.

---

### 22.4 Generator vs async generator

A regular generator produces values one at a time using `yield`.
An async generator does the same thing but can also `await` inside it.

**Regular generator:**

```python
def count_up(n):
    for i in range(n):
        yield i           # produce one value at a time

for number in count_up(5):
    print(number)
# prints: 0 1 2 3 4
```

**Async generator** (what `_stream_response` is):

```python
import asyncio

async def stream_words(text):
    for word in text.split():
        await asyncio.sleep(0.1)   # simulate arriving chunk
        yield word                 # produce one word at a time

async def main():
    async for word in stream_words("The capital of France is Paris"):
        print(word)

asyncio.run(main())
```

**What you will see**, appearing one word at a time:

```text
The
capital
of
France
is
Paris
```

That is exactly what `_stream_response()` does — except it gets words from the real API instead of a local string, and it wraps each word in a `StreamEvent(type=TEXT_DELTA, text_delta=TextDelta(content="..."))`.

---

### 22.5 Building a `StreamEvent` manually and inspecting it

You can construct the same internal objects the project uses, entirely without calling any API.

This helps you understand what `print(event)` is printing when you run the project.

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# --- These mirror the actual datatype.py file ---

class StreamEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    MESSAGE_COMPLETE = "message_complete"
    ERROR = "error"

@dataclass
class TextDelta:
    content: str

@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0

@dataclass
class StreamEvent:
    type: StreamEventType
    text_delta: Optional[TextDelta] = None
    error: Optional[str] = None
    finish_reason: Optional[str] = None
    usage: Optional[TokenUsage] = None

# --- Simulate a streaming response ---

stream_chunks = ["The", " capital", " of France", " is **Paris**."]

for text in stream_chunks:
    event = StreamEvent(
        type=StreamEventType.TEXT_DELTA,
        text_delta=TextDelta(content=text),
    )
    print(event)

# Simulate the final event
final_event = StreamEvent(
    type=StreamEventType.MESSAGE_COMPLETE,
    finish_reason="stop",
    usage=TokenUsage(prompt_tokens=18, completion_tokens=10, total_tokens=28),
)
print(final_event)
```

**What you will see:**

```text
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content='The'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content=' capital'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content=' of France'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.TEXT_DELTA: 'text_delta'>, text_delta=TextDelta(content=' is **Paris**.'), error=None, finish_reason=None, usage=None)
StreamEvent(type=<StreamEventType.MESSAGE_COMPLETE: 'message_complete'>, text_delta=None, error=None, finish_reason='stop', usage=TokenUsage(prompt_tokens=18, completion_tokens=10, total_tokens=28, cache_tokens=0))
```

This is **identical in shape** to what `uv run main.py` prints, just with fake local data instead of a real API call.

---

### 22.6 Accumulating streamed text into a final string

In streaming mode, the text arrives in pieces. If you want to reconstruct the full answer, you collect `TEXT_DELTA` events yourself.

This is a very common pattern in real apps:

```python
# Pretend these come from the real async for loop
fake_events = [
    StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=TextDelta(content="The")),
    StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=TextDelta(content=" capital")),
    StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=TextDelta(content=" of France")),
    StreamEvent(type=StreamEventType.TEXT_DELTA, text_delta=TextDelta(content=" is Paris.")),
    StreamEvent(type=StreamEventType.MESSAGE_COMPLETE, finish_reason="stop"),
]

full_text = ""  # start with empty string

for event in fake_events:
    if event.type == StreamEventType.TEXT_DELTA and event.text_delta:
        full_text += event.text_delta.content   # accumulate
        print(f"[partial] {full_text}")

    elif event.type == StreamEventType.MESSAGE_COMPLETE:
        print(f"\n[done]    {full_text}")
```

**What you will see:**

```text
[partial] The
[partial] The capital
[partial] The capital of France
[partial] The capital of France is Paris.

[done]    The capital of France is Paris.
```

This pattern is the foundation for streaming chat UIs, terminal spinners, or live progress displays.

---

### 22.7 Modifying `main.py` for your own experiments

Once you understand the system, here are a few easy changes you can make to `main.py` and immediately see how they affect output.

#### Experiment 1: Switch to non-streaming

Change this line:

```python
stream=True,
```

to:

```python
stream=False,
```

Instead of many `TEXT_DELTA` events you will see one `MESSAGE_COMPLETE` event containing the full answer.

#### Experiment 2: Ask a different question

Change the user message:

```python
{"role": "user", "content": "What is the capital of France?"},
```

to anything you want:

```python
{"role": "user", "content": "Explain recursion in one sentence."},
```

#### Experiment 3: Print only the text, not the full event object

Change:

```python
print(event)
```

to:

```python
if event.text_delta:
    print(event.text_delta.content, end="", flush=True)
```

Now instead of seeing raw `StreamEvent(...)` objects, you will see the AI response text printed live as it arrives — just like a real chat UI.

#### Experiment 4: Collect the full answer and print it at the end

```python
async def main():
    client = LLMClient()
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is the capital of France?"},
    ]

    full_text = ""

    async for event in client.chat_completion(message=messages, stream=True):
        if event.type.value == "text_delta" and event.text_delta:
            full_text += event.text_delta.content
        elif event.type.value == "message_complete":
            print(f"\nFull answer: {full_text}")
            if event.usage:
                print(f"Tokens used: {event.usage.total_tokens}")
```

This is the most common real-world usage pattern.

#### Experiment 5: React to errors

Wrap the event loop so you also handle errors gracefully:

```python
async for event in client.chat_completion(message=messages, stream=True):
    if event.type.value == "error":
        print(f"Something went wrong: {event.error}")
        break
    elif event.type.value == "text_delta" and event.text_delta:
        print(event.text_delta.content, end="", flush=True)
```

This is a real resilience pattern you will use in production agent code.

---

### 22.8 Reading the full annotated `main.py`

Here is the full `main.py` with beginner-friendly line-by-line comments:

```python
import asyncio                  # Python's async runtime — needed to run async code
from core.client.llm_client import LLMClient  # our wrapper class

async def main():               # async because we wait on network calls

    client = LLMClient()        # create the wrapper (SDK not opened yet)

    messages = [
        # The system message tells the model how to behave
        {"role": "system", "content": "You are a helpful assistant."},

        # The user message is the actual question
        {"role": "user", "content": "What is the capital of France?"},
    ]

    # chat_completion() is an async generator.
    # Every time the API sends a chunk, one StreamEvent is yielded here.
    async for event in client.chat_completion(
        message=messages,
        stream=True,    # True = receive token by token; False = wait for full reply
    ):
        print(event)    # print the raw event object

    print("Done")       # runs after the last event is received

# This line starts the async event loop and runs main()
asyncio.run(main())
```

---

## 23. Note for future improvement

One implementation detail worth remembering: the current code hardcodes the API key in `core/client/llm_client.py`.

That works for a local experiment, but in a real project it is better to load secrets from environment variables or a secure secret manager.

Also, in streaming mode, the caller must accumulate `TEXT_DELTA` events if it wants the full final text string.
That is normal, but it is useful to know when building a UI or logger.


