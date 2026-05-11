# 01 — The Agent Loop: Structure, Data Shapes, and Events

## Prerequisites

Complete [00-agent-basics.md](00-agent-basics.md) first.

You should already have a working `agent.py` with:
- a `while True` REPL loop
- a `fake_model()` that returns `{"type": "text", ...}` or `{"type": "tool_call", ...}`
- a `TOOLS` registry dict
- a `get_time` tool

This chapter keeps the same algorithm but replaces raw dicts and loose functions with **proper data classes**, a **tool abstraction**, and a lightweight **event system**. By the end you will have a mini-runtime that feels like a real agent harness.

---

## What you will build

By the end of this chapter your project will look like this:

```
agent/
    models.py      ← Message, ToolCall, ModelResponse, ToolResult dataclasses
    tools.py       ← BaseTool ABC + ToolRegistry + concrete tool implementations
    events.py      ← Runtime event dataclasses
    client.py      ← DemoModelClient (fake) with a clear interface
    agent.py       ← Agent class + async run() generator + REPL
    main.py        ← Entry point
```

You will be able to run it with:

```bash
python main.py
```

---

## 1. The two loops — keep them separate

One of the most common beginner mistakes is collapsing two different loops into one.

There are **always** two distinct loops in a CLI agent:

```
┌──────────────────────────────────────────────────┐
│  REPL LOOP  (outer)                              │
│  Manages the human session                       │
│                                                  │
│   while True:                                    │
│       prompt = input("you> ")                    │
│       if quit → break                            │
│       await agent.run(prompt)   ◀── one turn     │
│                                                  │
│  ┌───────────────────────────────────────────┐   │
│  │  AGENT LOOP  (inner)                      │   │
│  │  Manages one task until the model is done │   │
│  │                                           │   │
│  │   while True:                             │   │
│  │       response = model.complete(...)      │   │
│  │       if tool_call → run tool → append    │   │
│  │       else → return (turn complete)       │   │
│  └───────────────────────────────────────────┘   │
└──────────────────────────────────────────────────┘
```

| Loop | Responsibility |
|---|---|
| REPL | Human input, session lifetime, rendering output |
| Agent | Model calls, tool execution, conversation state |

Keep them in separate functions. Never mix their concerns.

---

## 2. Create the data models

Create `agent/models.py`. These four dataclasses replace every raw `dict` from Chapter 0.

```python
# agent/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Message:
    """One turn in the conversation history."""
    role: str                        # "user" | "assistant"
    content: list[dict[str, Any]]   # structured content blocks

    # ── Constructors ──────────────────────────────────────────────────────────

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=[{"type": "text", "text": text}])

    @classmethod
    def assistant(cls, text: str) -> Message:
        return cls(role="assistant", content=[{"type": "text", "text": text}])

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Message:
        """Feed a tool's output back into the conversation."""
        return cls(
            role="user",
            content=[
                {
                    "type": "tool_result",
                    "tool_call_id": tool_call_id,
                    "content": content,
                }
            ],
        )

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def text(self) -> str:
        """Return the first text block, or empty string."""
        for block in self.content:
            if block.get("type") == "text":
                return block.get("text", "")
        return ""


@dataclass(slots=True)
class ToolCall:
    """A structured action request from the model."""
    id: str               # unique per call — used to match the result later
    name: str             # which tool to invoke
    input: dict[str, Any] # arguments the model provided


@dataclass(slots=True)
class ModelResponse:
    """Everything the model returned in one turn."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tool(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass(slots=True, frozen=True)
class ToolResult:
    """The outcome of running one tool."""
    output: str
    is_error: bool = False
```

### Why four separate types instead of one big dict?

| Problem with raw dicts | How typed dataclasses fix it |
|---|---|
| Typos in key names (`"tool_cals"`) fail silently | IDE autocomplete + `AttributeError` on typos |
| No obvious place to put helper logic | Methods live on the class (`Message.user()`, `.text`) |
| Unclear what fields a dict is expected to have | The dataclass definition is the documentation |
| Hard to `isinstance`-check in event handlers | Works naturally with `isinstance` |

The `slots=True` option makes attribute access slightly faster and prevents accidental new attributes — a good habit for high-frequency objects.

---

## 3. Create the tool abstraction

Create `agent/tools.py`.

### 3a. `ToolResult` is already in models.py. Now add `BaseTool`

```python
# agent/tools.py
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any
from agent.models import ToolResult


class BaseTool(ABC):
    """
    Every tool in the registry must inherit from this class.

    The three class-level attributes tell the model:
    - what the tool is called   (name)
    - what the tool does        (description)
    - what arguments it expects (input_schema — JSON Schema format)
    """
    name: str
    description: str
    input_schema: dict[str, Any]

    @abstractmethod
    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Run the tool with the given arguments. Always return a ToolResult."""
        ...

    def schema(self) -> dict[str, Any]:
        """Return the model-facing schema for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
```

**Why `async def execute`?**

Real tools often do I/O — read a file, call an API, query a database. Making `execute` async from the start means you never have to refactor later. For CPU-only tools, `async` adds almost no overhead.

**Why `input_schema`?**

When you connect a real LLM, you pass the schema to the model so it knows what arguments to generate. JSON Schema is the industry standard format for this (used by OpenAI, Anthropic, Google, and others).

### 3b. Implement a concrete tool

```python
# agent/tools.py  (continued)
import datetime


class GetTimeTool(BaseTool):
    name = "get_time"
    description = "Returns the current date and time in UTC."
    input_schema = {
        "type": "object",
        "properties": {},   # no arguments required
        "required": [],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        now = datetime.datetime.now(datetime.timezone.utc)
        return ToolResult(output=now.strftime("%Y-%m-%d %H:%M:%S UTC"))


class EchoTool(BaseTool):
    """A simple tool for testing. Echoes whatever text is passed."""
    name = "echo"
    description = "Echoes the provided text back to the user."
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to echo."}
        },
        "required": ["text"],
    }

    async def execute(self, arguments: dict[str, Any]) -> ToolResult:
        text = arguments.get("text", "")
        return ToolResult(output=f"Echo: {text}")
```

### 3c. The tool registry

```python
# agent/tools.py  (continued)

class ToolRegistry:
    """
    Maps tool names to tool instances.
    The agent loop uses this to look up which tool to run.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """Return all tool schemas — passed to the model each turn."""
        return [tool.schema() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())


def default_registry() -> ToolRegistry:
    """Build and return a registry pre-loaded with the default tools."""
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    registry.register(EchoTool())
    return registry
```

**Why a registry instead of just a dict?**

A plain `dict` works, but a registry class gives you:

- a single place to add validation (reject duplicate names, check schema format)
- `.schemas()` method — keeps the loop from knowing anything about schema shape
- easy to extend with `unregister`, `list`, `reload` later

---

## 4. Create the event types

Create `agent/events.py`.

Events are how the runtime communicates progress **without** printing directly. The loop emits events; the REPL decides how to display them. This keeps rendering logic completely out of the agent core.

```python
# agent/events.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class StatusEvent:
    """General status message from the runtime."""
    message: str


@dataclass(slots=True, frozen=True)
class ToolExecutionStarted:
    """Emitted just before a tool is called."""
    tool_name: str
    tool_input: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ToolExecutionCompleted:
    """Emitted after a tool finishes, whether success or error."""
    tool_name: str
    output: str
    is_error: bool = False


@dataclass(slots=True, frozen=True)
class AssistantTextDelta:
    """A chunk (or full block) of assistant text."""
    text: str


@dataclass(slots=True, frozen=True)
class ErrorEvent:
    """Something went wrong inside the runtime."""
    message: str
    details: str = ""


# A union type for type-checking the renderer
AgentEvent = (
    StatusEvent
    | ToolExecutionStarted
    | ToolExecutionCompleted
    | AssistantTextDelta
    | ErrorEvent
)
```

**Why `frozen=True` on events?**

Events are facts about the past. Freezing them prevents anyone from accidentally mutating an event after it has been emitted — a subtle bug that is very hard to track down in async code.

---

## 5. Create the model client

Create `agent/client.py`.

The model client has one job: receive the current conversation and tool list, and return a `ModelResponse`. The loop does not care whether the client calls OpenAI, Anthropic, a local Ollama server, or a fake function — it only cares about the return type.

```python
# agent/client.py
from __future__ import annotations
from typing import Any
from agent.models import ModelResponse, ToolCall


class DemoModelClient:
    """
    A deterministic fake model for development and testing.

    Replace this class with a real API client when you are ready.
    The interface (one async `complete` method) stays the same.
    """

    async def complete(
        self,
        *,
        messages: list[Any],
        tools: list[dict[str, Any]],
        system_prompt: str,
    ) -> ModelResponse:
        # Read the last message text
        last = messages[-1] if messages else None
        prompt = last.text.lower().strip() if last else ""

        # Route to a tool if the prompt matches
        if "time" in prompt:
            return ModelResponse(
                tool_calls=[ToolCall(id="tc-001", name="get_time", input={})]
            )

        if "echo" in prompt:
            # Extract the text after "echo"
            echo_text = prompt.split("echo", 1)[-1].strip() or "nothing to echo"
            return ModelResponse(
                tool_calls=[
                    ToolCall(id="tc-002", name="echo", input={"text": echo_text})
                ]
            )

        # Default: plain text response
        return ModelResponse(text="I received your message. How can I help further?")
```

**The real client contract:**

When you replace `DemoModelClient` with a real provider, you only need to:

1. Call the provider API
2. Parse the response JSON into `ModelResponse` + `ToolCall` objects
3. Return it

The rest of the agent never changes. This is the entire value of having a typed contract.

---

## 6. Build the Agent class

Create `agent/agent.py`. This is the core of the chapter.

```python
# agent/agent.py
from __future__ import annotations
from typing import AsyncGenerator, Any

from agent.models import Message, ModelResponse
from agent.tools import ToolRegistry
from agent.events import (
    AgentEvent,
    AssistantTextDelta,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)


class Agent:
    """
    Owns the conversation history and drives one task turn to completion.

    Usage:
        agent = Agent(client, registry, system_prompt="You are a helpful assistant.")
        async for event in agent.run("What time is it?"):
            await render(event)
    """

    def __init__(
        self,
        model_client: Any,
        tool_registry: ToolRegistry,
        system_prompt: str = "You are a helpful assistant.",
    ) -> None:
        self.model_client = model_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.messages: list[Message] = []   # grows across turns

    async def run(self, user_text: str) -> AsyncGenerator[AgentEvent, None]:
        """
        Run one user turn to completion.

        Yields AgentEvent objects as work progresses.
        The caller renders them — this method never prints anything.
        """
        # 1. Add the user message to history
        self.messages.append(Message.user(user_text))
        yield StatusEvent(message="Thinking...")

        # 2. The inner agent loop
        while True:
            # Ask the model what to do next
            try:
                response: ModelResponse = await self.model_client.complete(
                    messages=self.messages,
                    tools=self.tool_registry.schemas(),
                    system_prompt=self.system_prompt,
                )
            except Exception as exc:
                yield ErrorEvent(message="Model call failed.", details=str(exc))
                return

            # 3. If the model returned text, store it and emit it
            if response.text:
                self.messages.append(Message.assistant(response.text))
                yield AssistantTextDelta(text=response.text)

            # 4. If no tool calls, the turn is complete
            if not response.wants_tool:
                return

            # 5. Execute each requested tool
            for tool_call in response.tool_calls:
                yield ToolExecutionStarted(
                    tool_name=tool_call.name,
                    tool_input=tool_call.input,
                )

                tool = self.tool_registry.get(tool_call.name)
                if tool is None:
                    result_text = f"Error: tool '{tool_call.name}' is not registered."
                    is_error = True
                else:
                    try:
                        result = await tool.execute(tool_call.input)
                        result_text = result.output
                        is_error = result.is_error
                    except Exception as exc:
                        result_text = f"Tool raised an exception: {exc}"
                        is_error = True

                # 6. Feed the result back into the conversation
                self.messages.append(
                    Message.tool_result(tool_call.id, result_text)
                )

                yield ToolExecutionCompleted(
                    tool_name=tool_call.name,
                    output=result_text,
                    is_error=is_error,
                )
            # Loop back → ask the model again with updated history
```

### Annotated walkthrough of `run()`

```
Step 1  messages.append(Message.user(...))
        ↓
Step 2  while True:
        ↓
Step 3      response = await model_client.complete(messages, tools, system_prompt)
        ↓
Step 4      if response.text → store + yield AssistantTextDelta
        ↓
Step 5      if no tool_calls → return   ← turn is done
        ↓
Step 6      for each tool_call:
               yield ToolExecutionStarted
               result = await tool.execute(tool_call.input)
               messages.append(Message.tool_result(...))   ← critical step
               yield ToolExecutionCompleted
        ↓
        loop back to Step 3
```

**The critical step** is `messages.append(Message.tool_result(...))`. Without it, the model's next call would have no knowledge that a tool was run or what it returned. The model would repeat the tool call forever (or hallucinate the result). Always feed tool output back into history.

---

## 7. Create the REPL and entry point

Create `agent/main.py`:

```python
# main.py
import asyncio
from agent.agent import Agent
from agent.client import DemoModelClient
from agent.tools import default_registry
from agent.events import (
    AssistantTextDelta,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)


# ── Renderer ──────────────────────────────────────────────────────────────────

async def render(event: object) -> None:
    """
    Translate runtime events into terminal output.
    This is the ONLY place where print() is called.
    """
    if isinstance(event, StatusEvent):
        print(f"  · {event.message}")

    elif isinstance(event, ToolExecutionStarted):
        args = event.tool_input or {}
        args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "no args"
        print(f"  ⚙ running {event.tool_name}({args_str})")

    elif isinstance(event, ToolExecutionCompleted):
        icon = "✗" if event.is_error else "✓"
        print(f"  {icon} {event.tool_name} → {event.output}")

    elif isinstance(event, AssistantTextDelta):
        print(f"\nagent> {event.text}\n")

    elif isinstance(event, ErrorEvent):
        print(f"\n[ERROR] {event.message}")
        if event.details:
            print(f"        {event.details}")


# ── REPL ──────────────────────────────────────────────────────────────────────

async def repl(agent: Agent) -> None:
    """
    The outer loop. Manages the human session.
    Hands one prompt at a time to agent.run().
    """
    print("Agent ready. Available tools:", agent.tool_registry.names())
    print("Type 'quit' to exit.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return

        if user_input in {"quit", "exit", "q"}:
            print("Goodbye.")
            return

        if not user_input:
            continue

        # Run one full turn and render each event as it arrives
        async for event in agent.run(user_input):
            await render(event)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    client = DemoModelClient()
    registry = default_registry()
    agent = Agent(
        model_client=client,
        tool_registry=registry,
        system_prompt="You are a helpful assistant with access to tools.",
    )
    await repl(agent)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 8. Create the package `__init__` files

```bash
mkdir -p agent
touch agent/__init__.py
```

Your final file structure:

```
agent/
    __init__.py
    models.py
    tools.py
    events.py
    client.py
    agent.py
main.py
```

---

## 9. Run it

```bash
python main.py
```

Expected session:

```
Agent ready. Available tools: ['get_time', 'echo']
Type 'quit' to exit.

you> what time is it?
  · Thinking...
  ⚙ running get_time(no args)
  ✓ get_time → 2026-04-24 10:44:21 UTC

agent> (empty — the fake model returns no text after a tool call)

you> echo hello world
  · Thinking...
  ⚙ running echo(text='hello world')
  ✓ echo → Echo: hello world

you> tell me something
  · Thinking...

agent> I received your message. How can I help further?

you> quit
Goodbye.
```

---

## 10. Common mistakes and how to fix them

### Mistake 1 — Not feeding tool results back

```python
# WRONG — model never learns what the tool returned
result = await tool.execute(tool_call.input)
# missing: self.messages.append(Message.tool_result(...))
```

**Fix:** always append a `Message.tool_result(tool_call.id, result.output)` immediately after every tool execution.

### Mistake 2 — Printing inside the agent loop

```python
# WRONG — hardcodes terminal output in the runtime
async def run(self, user_text):
    ...
    print(f"Tool result: {result.output}")  # ← don't do this
```

**Fix:** yield an event instead. Let the REPL's renderer decide how to display it.

### Mistake 3 — Missing the inner loop

```python
# WRONG — only handles one tool call, then stops
response = await model_client.complete(...)
if response.wants_tool:
    result = await tool.execute(...)
    messages.append(Message.tool_result(...))
# no loop → model never sees the result
```

**Fix:** wrap the `complete → tool → append` cycle in `while True` and only `return` when `response.wants_tool` is `False`.

### Mistake 4 — Unhandled unknown tool

```python
# WRONG — crashes if model requests a tool that is not registered
tool = self.tool_registry.get(tool_call.name)
result = await tool.execute(...)   # AttributeError if tool is None
```

**Fix:** check for `None` before calling execute, append an error `ToolResult`, and yield an `ErrorEvent`.

---

## 11. Exercises

**Exercise A — Add a tool that requires an argument**

Create a `MathTool` that accepts `{"expression": "2 + 2"}` and evaluates it safely using Python's `ast.literal_eval` (not `eval`). Register it and update `DemoModelClient` to route "calculate" prompts to it.

**Exercise B — Add turn counting**

Add a `turn_count: int` attribute to `Agent` that increments each time `run()` is called. Emit it inside a `StatusEvent` at the start of each turn: `"Thinking... (turn 3)"`.

**Exercise C — Handle multiple tool calls in one response**

`ModelResponse.tool_calls` is a list — a real model can request several tools in one turn. Make sure your loop iterates all of them, not just the first. Write a test with a fake model that returns two tool calls at once.

**Exercise D — Swap in a real model**

Install `openai`:

```bash
pip install openai
```

Write a `OpenAIModelClient` with the same `async def complete(...)` interface. It should:

1. Convert `self.messages` to the OpenAI wire format
2. Call `client.chat.completions.create(...)`
3. Parse the response into `ModelResponse` and `ToolCall` objects

Pass it to `Agent` in place of `DemoModelClient`. Nothing else in the codebase changes.

---

## 12. What the full picture looks like now

```
you> what time is it?
      │
      ▼
  REPL loop
  agent.run("what time is it?")
      │
      ▼
  Agent.run()
    messages.append(user msg)          # history grows
    yield StatusEvent
      │
      ▼
    while True:
      model_client.complete(messages)  # model decides
      └─▶ ModelResponse(tool_calls=[get_time])
            │
            ▼
      yield ToolExecutionStarted
      tool.execute({})                 # tool runs
      messages.append(tool_result)     # history grows again
      yield ToolExecutionCompleted
            │
            ▼
      model_client.complete(messages)  # model sees result
      └─▶ ModelResponse(text="It is 10:44 UTC.")
            │
            ▼
      yield AssistantTextDelta
      return                           # turn done
      │
      ▼
  REPL loop renders events
  waits for next input
```

---

## 13. Summary — what changed from Chapter 0

| Chapter 0 | Chapter 1 |
|---|---|
| Raw `dict` messages | `Message` dataclass with constructors |
| `{"type": "tool_call"}` dict | `ToolCall` dataclass with a named `id` |
| Plain string model response | `ModelResponse` with `.wants_tool` property |
| Function + plain dict registry | `BaseTool` ABC + `ToolRegistry` class |
| `print()` everywhere | `yield` events, `render()` in REPL |
| Single function | `Agent` class owning history + loop |

The algorithm is identical. The structure makes it maintainable, testable, and extensible.

---

## Checklist before moving on

- [ ] `Message`, `ToolCall`, `ModelResponse`, `ToolResult` are each their own dataclass
- [ ] Every tool inherits from `BaseTool` and implements `async execute()`
- [ ] `ToolRegistry` maps tool names to tool instances
- [ ] `Agent.run()` is an async generator that `yield`s events, never `print`s
- [ ] Tool results are always appended to `self.messages` before the next model call
- [ ] The REPL renders events; the agent loop knows nothing about terminal formatting
- [ ] Unknown tool names are handled gracefully (error event, not a crash)

---

Next: [01-1-streaming.md](01-1-streaming.md) — upgrade the model client to stream tokens live, then continue to [02-tools.md](02-tools.md).

### Appendix: two best practices established here

**Type annotations everywhere.** All function parameters and return types should use Python 3.10+ syntax (`str | None` not `Optional[str]`). In an async multi-component system, missing types cause subtle bugs that are hard to find at runtime. Every function in this series follows this rule.

**`slots=True` on high-frequency dataclasses.** `@dataclass(slots=True)` prevents Python from creating a `__dict__` per instance, reducing memory by ~30% for objects created thousands of times per session (messages, tool calls, events). Use it on all dataclasses that live in message arrays or event streams.
