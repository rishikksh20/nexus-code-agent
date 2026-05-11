# Chapter 2: Typed Loop And Runtime Models

## Objective

Turn the rough REPL prototype into a real agent runtime with explicit types. This chapter brings in the strongest practical improvement from `openai-code-tutorial`: model messages, tool calls, tool results, and model responses as dataclasses instead of ad hoc dictionaries.

## Why This Chapter Exists

Untyped agent runtimes fail in predictable ways:

- tool payloads drift silently
- result shapes change without warning
- logs are hard to read
- tests compare loose dictionaries instead of stable structures

The solution is not adding a framework. The solution is choosing a small set of runtime objects and making them the backbone of the harness.

## Runtime Data Models

Start with four core models.

```python
from dataclasses import dataclass, field
from typing import Any, Literal


Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True, frozen=True)
class Message:
    role: Role
    content: str
    name: str | None = None


@dataclass(slots=True, frozen=True)
class ToolCall:
    call_id: str
    tool_name: str
    # arguments is read-once when the tool runs; treat as read-only after construction
    arguments: dict[str, Any]


@dataclass(slots=True)  # NOT frozen: metadata is a mutable accumulator
class ToolResult:
    call_id: str
    tool_name: str
    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ModelResponse:
    message: Message
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "done"
```

Use `slots=True` to keep the objects lightweight and predictable. Use `frozen=True` to discourage in-place mutation of runtime state.

## Introduce The Agent Loop

Now create the inner loop. It should yield events so the UI layer can render progress without owning the runtime logic.

```python
from collections.abc import AsyncGenerator


class Agent:
    def __init__(self, model_client) -> None:
        self.model_client = model_client

    async def run(self, messages: list[Message]) -> AsyncGenerator[dict, None]:
        # Python treats any async def with a yield as an async generator.
        # The return annotation AsyncGenerator[dict, None] is more precise
        # than AsyncIterator because it documents the send-type (None) too.
        yield {"event": "thinking_started"}
        response = await self.model_client.complete(messages)
        yield {"event": "model_response", "value": response}

        for tool_call in response.tool_calls:
            yield {"event": "tool_call_requested", "value": tool_call}

        yield {"event": "turn_completed", "stop_reason": response.stop_reason}
```

At this stage the yielded events can still be plain dictionaries, but the payloads inside them should be typed dataclasses. In later chapters you can convert event envelopes into typed classes too.

## Keep The REPL Thin

The REPL should collect input, hand it to the agent, and render events. It should not decide permissions, compact context, or call tools directly.

```python
import asyncio

from models import Message
from runtime.agent import Agent


async def repl(agent: Agent) -> None:
    history: list[Message] = []

    while True:
        user_text = input("> ").strip()
        if user_text.lower() in {"quit", "exit"}:
            return

        history.append(Message(role="user", content=user_text))

        async for event in agent.run(history):
            if event["event"] == "model_response":
                response = event["value"]
                history.append(response.message)
                print(response.message.content)


async def main() -> None:
    from integrations.fake_model import FakeModelClient  # replace with real client later
    model_client = FakeModelClient()
    agent = Agent(model_client)
    await repl(agent)


if __name__ == "__main__":
    asyncio.run(main())
```

## Message History Rules

Adopt these rules early.

1. Message history is append-only for a turn.
2. The model never mutates prior messages.
3. Tool results become messages or context inputs, not hidden side channels.
4. Every tool call must be traceable back to a model response.

These rules make persistence, audits, and replay much easier later.

## Recommended Runtime Package Split

By the end of this chapter, split code into a few stable modules.

```text
agent_harness/
  app.py
  models.py
  runtime/
    agent.py
  integrations/
    fake_model.py
```

Do not add `memory`, `permissions`, or `plugins` directories yet. That is premature at this stage.

## Action Plan

1. Move all runtime payloads into dataclasses.
2. Add an `Agent.run()` async generator.
3. Ensure the REPL only renders runtime events.
4. Make message history append-only for each turn.
5. Keep tool execution out of the REPL even if you only have fake responses for now.

## Validation Checklist

- The model client returns `ModelResponse`, not loose dicts.
- A tool call, if present, is represented by `ToolCall`.
- A completed turn always yields a stop reason.
- The REPL can render a response without needing to inspect internal model details.

## Definition Of Done

Move on only when your code has a clean answer for this question:

Can you print, persist, test, and replay a turn using your typed models without relying on implicit conventions?

If the answer is no, your runtime model is still too loose.