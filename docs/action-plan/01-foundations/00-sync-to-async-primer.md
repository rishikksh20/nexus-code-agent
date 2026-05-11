# Before Chapter 1: From Sync REPL To Async Streaming

## Who This Is For

If you have never written `async def` or `await` before, start here. This document builds the same REPL from scratch three times:

1. completely synchronous — no asyncio, works just like a normal Python script
2. async without streaming — same behavior, but now the door is open for concurrent work
3. async with streaming — text appears word by word instead of all at once

Each version explains exactly what changed, why it was changed, and what problem it solves.

After this primer, Chapter 1 will feel natural rather than surprising.

---

## Version 1: The Simplest Possible Sync REPL

No asyncio. No await. Just plain Python.

```python
# app_sync.py


def fake_model_complete(prompt: str) -> str:
    """A fake model that always returns a canned response."""
    if "time" in prompt.lower():
        return "I should call a time tool for that."
    return f"Echo: {prompt}"


def repl() -> None:
    print("Minimal Agent (sync version)")
    print("Type 'quit' to exit.\n")

    while True:
        user_text = input("> ").strip()

        if user_text.lower() in {"quit", "exit"}:
            print("bye")
            return

        response = fake_model_complete(user_text)
        print(response)


if __name__ == "__main__":
    repl()
```

Run it:

```
$ python app_sync.py
Minimal Agent (sync version)
Type 'quit' to exit.

> hello
Echo: hello
> what time is it
I should call a time tool for that.
> quit
bye
```

This works perfectly. It is easy to understand. Every line runs top to bottom with no surprises.

### What Is Missing

Nothing yet. For a one-user, no-network script this is fine.

The problems appear when you try to do any of the following:

- call a real API that takes 1–3 seconds to respond
- stream output token by token so the user sees progress
- run a background file write while waiting for user input
- run two tools at the same time because they are independent

All of those require waiting for something without freezing the whole program. That is exactly what `asyncio` is for.

---

## The Core Problem: Blocking

When Python calls `input()`, it stops and waits. Nothing else can happen.

When a synchronous function calls a slow network API, it stops and waits again. If that call takes 2 seconds, your program is frozen for 2 seconds.

```python
import time


def slow_model_complete(prompt: str) -> str:
    time.sleep(2)  # simulates a real API call
    return f"Response to: {prompt}"


def repl() -> None:
    while True:
        user_text = input("> ").strip()
        if user_text.lower() == "quit":
            return
        # The program is completely stuck for 2 seconds here.
        # Nothing else can run. No progress indicator. No timeout.
        response = slow_model_complete(user_text)
        print(response)
```

For a CLI agent this creates real friction:

- the terminal is frozen; users do not know if it crashed
- you cannot show partial output while the rest is generating
- you cannot run safe read-only tools in parallel to save time
- you cannot abort a slow call cleanly

---

## Version 2: Async REPL — Same Behavior, New Capabilities

Convert the sync version to async. The behavior is identical, but now slow work can yield control instead of blocking.

```python
# app_async.py
import asyncio


async def fake_model_complete(prompt: str) -> str:
    """
    Still fake, but now declared async.
    In a real implementation this would use aiohttp or httpx to call an API
    without blocking the event loop.
    """
    await asyncio.sleep(0)  # yields control; replace with a real awaitable later
    if "time" in prompt.lower():
        return "I should call a time tool for that."
    return f"Echo: {prompt}"


async def repl() -> None:
    print("Minimal Agent (async version)")
    print("Type 'quit' to exit.\n")

    while True:
        user_text = input("> ").strip()  # still blocking; see note below

        if user_text.lower() in {"quit", "exit"}:
            print("bye")
            return

        response = await fake_model_complete(user_text)
        print(response)


if __name__ == "__main__":
    asyncio.run(repl())
```

### What Changed

| | Sync | Async |
|---|---|---|
| Function keyword | `def` | `async def` |
| Call a slow function | `result = fn()` | `result = await fn()` |
| Entry point | `repl()` | `asyncio.run(repl())` |
| Behavior | identical | identical for now |

The output is the same. The difference is that `await` suspends only the current coroutine, not the whole program. Other tasks registered with the event loop can run in the gaps.

### Why `input()` Is Still Blocking

`input()` is a synchronous stdlib call. It blocks the event loop. For a single-user CLI this is acceptable because there is exactly one user and one stream of interaction. The event loop has nothing else to do while waiting for the human to type.

If you needed to handle multiple users at once (a server), you would replace `input()` with `asyncio.StreamReader` or a web framework. For a local developer tool, `input()` is fine.

### Why `asyncio.run(repl())`

`asyncio.run()` creates an event loop, runs the coroutine you give it until it finishes, and then cleans up. It is the standard top-level entry point for any async Python program. You call it exactly once, at the outermost layer.

---

## Version 3: Async With Streaming

Now the difference becomes visible. Streaming lets users see output token by token, like ChatGPT typing. Without async this is not possible because you cannot yield partial output while also being suspended waiting for more.

```python
# app_stream.py
import asyncio
from collections.abc import AsyncIterator


async def fake_model_stream(prompt: str) -> AsyncIterator[str]:
    """
    Simulates a streaming model response.
    In reality this would wrap an SSE or WebSocket stream from an API.
    Each 'chunk' is one piece of text as it becomes available.
    """
    words = f"Echo streaming: {prompt}".split()
    for word in words:
        await asyncio.sleep(0.1)  # simulates the gap between tokens
        yield word + " "


async def repl() -> None:
    print("Minimal Agent (streaming version)")
    print("Type 'quit' to exit.\n")

    while True:
        user_text = input("> ").strip()

        if user_text.lower() in {"quit", "exit"}:
            print("bye")
            return

        # Print each chunk as it arrives instead of waiting for the full response
        async for chunk in fake_model_stream(user_text):
            print(chunk, end="", flush=True)
        print()  # newline after the full response


if __name__ == "__main__":
    asyncio.run(repl())
```

Running it looks like:

```
> hello world
Echo streaming: hello world
```

...but each word appears with a small delay between them, giving visible feedback that the model is working.

### What Changed From Version 2

| | Async (no streaming) | Async (streaming) |
|---|---|---|
| Model returns | a single `str` | an `AsyncIterator[str]` |
| Call syntax | `response = await fn()` | `async for chunk in fn(): ...` |
| Output timing | all at once after full response | word by word as chunks arrive |
| User experience | feels like waiting | feels like watching |

### Why `async for` Instead Of `for`

A regular `for` loop pulls items from a synchronous iterator. `async for` pulls items from an async iterator — one that can `await` between each item. In streaming APIs, each chunk waits for the network to deliver the next piece. Without `await` at each step, you would block on the first chunk and get nothing useful.

---

## The Conversion Path At A Glance

```text
def fn() -> str:              →   async def fn() -> str:
    result = slow_call()      →       result = await slow_call()
    return result             →       return result

def repl():                   →   async def repl():
    result = fn()             →       result = await fn()

fn()                          →   asyncio.run(fn())

for item in sync_gen():       →   async for item in async_gen():
    use(item)                 →       use(item)
```

There are only four mechanical changes:

1. add `async` before `def`
2. add `await` before any call that does real I/O or is itself async
3. wrap the top-level call in `asyncio.run()`
4. change `for` to `async for` when iterating an async generator

---

## What Async Does Not Do

Async is often misunderstood. Clear this up before continuing.

- **Async does not make things faster by itself.** If your code does no I/O and never awaits, `async def` adds zero benefit.
- **Async does not use threads.** There is one thread. The event loop switches between coroutines at `await` points. If one coroutine does CPU-heavy work without any `await`, it still blocks everything else.
- **Async does not fix blocking calls.** If you call a synchronous library (like `requests`, `sqlite3`, or `time.sleep`) inside an async function without wrapping it properly, it blocks the event loop just like before.
- **Async is worth it when you have multiple things waiting at the same time.** Calling a model API, writing a file, and checking a tool result can all be in flight simultaneously if they are async-aware.

---

## What The Agent Harness Needs From Async

By the time you reach Chapter 5 and beyond, the harness will be doing this in a single turn:

1. `await` the model API for a response
2. `async for` chunks from a streaming endpoint while rendering
3. `await` one or more tool executions
4. `await` a file write for the session snapshot
5. `await` hook callbacks before and after tool use

Every one of those operations waits for something external. Without async, they would have to happen one at a time, sequentially, with the program frozen at each step.

With async, the event loop can interleave them — running the next available unit of work while the others are waiting — and your harness stays responsive throughout.

---

## Where To Go From Here

You now understand:

- what a sync REPL looks like and why it is limited
- what `async def`, `await`, and `asyncio.run()` do mechanically
- what streaming adds and why `async for` is required
- when async is worth using and when it is not

Continue to **Chapter 1** to build the real harness skeleton. Every async pattern in that chapter should now make sense because you have seen each one here first.
