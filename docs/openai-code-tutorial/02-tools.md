# 02 — Tools: Schemas, Context, Validation, and Real I/O

## Prerequisites

Complete [01-agent-loop.md](01-agent-loop.md) first.

You should already have this file structure:

```
agent/
    __init__.py
    models.py      ← Message, ToolCall, ModelResponse, ToolResult
    tools.py       ← BaseTool, ToolRegistry, GetTimeTool, EchoTool
    events.py      ← StatusEvent, ToolExecutionStarted/Completed, etc.
    client.py      ← DemoModelClient
    agent.py       ← Agent class with async run() generator
main.py            ← REPL + renderer + entry point
```

This chapter expands the tool layer significantly. By the end, your agent will have real, useful tools that read files, search directories, and ask the user for clarification — and your `Agent` will be ready to plug into a real LLM API (OpenAI, Anthropic, or any OpenAI-compatible endpoint).

---

## What you will build

You will upgrade `agent/tools.py` with:

- `ToolExecutionContext` — runtime-owned data injected into every tool call
- `ToolResult` with a `metadata` field for structured runtime data
- `ReadFileTool` — reads a file from disk (read-only)
- `GlobTool` — lists files matching a pattern (read-only)
- `WriteFileTool` — writes content to a file (mutating)
- `AskUserQuestionTool` — asks the human a clarifying question mid-task

You will also add:

- input validation inside tools
- a bridge function to convert your internal types to OpenAI-compatible wire format
- a real `OpenAIModelClient` skeleton (swappable for `DemoModelClient`)

---

## 1. Why tools come right after the loop

Looking at what Chapter 01 built: the loop is complete but the agent can only do two things — `get_time` and `echo`. No agent is useful with only those.

Tools are what turn a "reasoning system" into an "acting system":

```
Chapter 01 agent:  user prompt → model thinks → text answer
Chapter 02 agent:  user prompt → model thinks → reads files
                                              → searches directories  
                                              → writes output
                                              → asks clarifying questions
                                              → text answer
```

The loop code from Chapter 01 does **not change** in this chapter. Everything you add here lives inside the tools themselves and the context they receive.

---

## 2. Upgrade `ToolResult` to carry metadata

Open `agent/models.py`. The current `ToolResult` is:

```python
@dataclass(slots=True, frozen=True)
class ToolResult:
    output: str
    is_error: bool = False
```

Add a `metadata` field. This is for runtime-only structured data that you may want for logging, UI, or policy — but that you do **not** necessarily feed back to the model:

```python
# agent/models.py  — updated ToolResult

from dataclasses import dataclass, field
from typing import Any

@dataclass(slots=True, frozen=True)
class ToolResult:
    output: str                                       # text fed back to the model
    is_error: bool = False                            # did the tool fail?
    metadata: dict[str, Any] = field(default_factory=dict)  # runtime-only extras
```

**What goes in `metadata`?**

Examples:
- `{"bytes_read": 4096, "resolved_path": "/home/user/app.py"}` from `ReadFileTool`
- `{"files_found": 12}` from `GlobTool`
- `{"bytes_written": 256}` from `WriteFileTool`

The model only sees `output`. Your REPL, logger, and future policy layer can inspect `metadata` without polluting the model's context.

> **Note:** `metadata` must also be hashable for `frozen=True` to work at runtime. Since `dict` is mutable, Python allows it in a frozen dataclass only if you never mutate it after construction — which is our convention here. Alternatively, swap `frozen=True` for just `slots=True` on `ToolResult` if you want to keep things simpler.

---

## 3. Add `ToolExecutionContext`

Add this new dataclass **to `agent/models.py`** (or create `agent/context.py` if you prefer cleaner separation):

```python
# agent/models.py  — add below ToolResult

import os
from typing import Callable, Awaitable


@dataclass
class ToolExecutionContext:
    """
    Runtime-owned information injected into every tool call.

    Tools should read from this context instead of relying on global state.
    This makes tools easier to test and reason about.
    """
    cwd: str = field(default_factory=os.getcwd)   # current working directory
    ask_user: Callable[[str], Awaitable[str]] | None = None  # async prompt callback
    metadata: dict[str, Any] = field(default_factory=dict)   # session/task extras
```

**Why explicit context instead of global state?**

Without context, tools reach into `os.getcwd()` or globals directly. That makes them:
- hard to test (you must mutate globals),
- session-unaware (every tool sees the same global),
- hard to sandbox (you cannot restrict what a tool sees).

With context, you can pass a fake `cwd` in tests, a sandboxed directory in production, and session-specific callbacks per task — without changing any tool code.

---

## 4. Upgrade `BaseTool` to accept context

Open `agent/tools.py`. Update `BaseTool.execute` to receive a `ToolExecutionContext`:

```python
# agent/tools.py  — full updated file

from __future__ import annotations

import datetime
import glob as glob_module
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from agent.models import ToolExecutionContext, ToolResult


class BaseTool(ABC):
    """
    Contract every tool must satisfy.

    name          — identifier the model uses to request this tool
    description   — human/model-readable explanation of what it does
    input_schema  — JSON Schema describing accepted arguments
    """
    name: str
    description: str
    input_schema: dict[str, Any]

    @abstractmethod
    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Run the tool. Always return a ToolResult, never raise."""
        ...

    def schema(self) -> dict[str, Any]:
        """Return the model-facing tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
```

The only change from Chapter 01 is the added `context: ToolExecutionContext` parameter. The rest of the contract is identical.

---

## 5. Build the tool suite

### 5a. Keep existing tools (updated signatures)

```python
# agent/tools.py  (continued)

class GetTimeTool(BaseTool):
    name = "get_time"
    description = "Returns the current date and time in UTC."
    input_schema = {"type": "object", "properties": {}, "required": []}

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        now = datetime.datetime.now(datetime.timezone.utc)
        return ToolResult(output=now.strftime("%Y-%m-%d %H:%M:%S UTC"))


class EchoTool(BaseTool):
    name = "echo"
    description = "Echoes the provided text back."
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to echo."}
        },
        "required": ["text"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        text = arguments.get("text", "")
        return ToolResult(output=f"Echo: {text}")
```

### 5b. `ReadFileTool` — a read-only tool

```python
# agent/tools.py  (continued)

class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "Read the contents of a text file from disk. "
        "Relative paths are resolved from the current working directory."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to read.",
            }
        },
        "required": ["file_path"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        # ── Input validation ─────────────────────────────────────────────────
        file_path = arguments.get("file_path", "").strip()
        if not file_path:
            return ToolResult(
                output="Error: 'file_path' argument is required.",
                is_error=True,
            )

        # ── Resolve path against context.cwd ─────────────────────────────────
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(context.cwd) / path
        path = path.resolve()

        # ── Read ──────────────────────────────────────────────────────────────
        if not path.exists():
            return ToolResult(
                output=f"Error: file not found: {path}",
                is_error=True,
            )
        if not path.is_file():
            return ToolResult(
                output=f"Error: path is not a file: {path}",
                is_error=True,
            )

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(output=f"Error reading file: {exc}", is_error=True)

        return ToolResult(
            output=content,
            metadata={"resolved_path": str(path), "bytes_read": len(content)},
        )
```

**Design notes:**
- **Never raise** — wrap every OS call in `try/except` and return an error `ToolResult`
- **Resolve paths** — always resolve relative paths against `context.cwd`, not `os.getcwd()`
- **Validate first** — check for missing/empty args before any I/O

### 5c. `GlobTool` — list files matching a pattern

```python
# agent/tools.py  (continued)

class GlobTool(BaseTool):
    name = "glob"
    description = (
        "List files matching a glob pattern. "
        "Examples: '**/*.py', 'src/*.ts', '*.md'. "
        "Returns a newline-separated list of matching paths."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match against.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 50).",
            },
        },
        "required": ["pattern"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        pattern = arguments.get("pattern", "").strip()
        max_results = int(arguments.get("max_results", 50))

        if not pattern:
            return ToolResult(
                output="Error: 'pattern' argument is required.",
                is_error=True,
            )

        search_root = Path(context.cwd)
        full_pattern = str(search_root / pattern)

        try:
            matches = glob_module.glob(full_pattern, recursive=True)
        except Exception as exc:
            return ToolResult(output=f"Glob error: {exc}", is_error=True)

        matches = sorted(matches)[:max_results]

        if not matches:
            return ToolResult(
                output="No files matched the pattern.",
                metadata={"pattern": pattern, "files_found": 0},
            )

        output = "\n".join(matches)
        return ToolResult(
            output=output,
            metadata={"pattern": pattern, "files_found": len(matches)},
        )
```

### 5d. `WriteFileTool` — a mutating tool

Notice the clear docstring: this tool **changes the world**. Label mutating tools explicitly — this distinction becomes important when you add permissions in Chapter 07.

```python
# agent/tools.py  (continued)

class WriteFileTool(BaseTool):
    name = "write_file"
    description = (
        "Write text content to a file. "
        "Creates the file if it does not exist. "
        "OVERWRITES the file if it already exists. "
        "This is a MUTATING tool — it changes the filesystem."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to write.",
            },
            "content": {
                "type": "string",
                "description": "Text content to write to the file.",
            },
        },
        "required": ["file_path", "content"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        file_path = arguments.get("file_path", "").strip()
        content = arguments.get("content", "")

        if not file_path:
            return ToolResult(
                output="Error: 'file_path' argument is required.",
                is_error=True,
            )

        path = Path(file_path)
        if not path.is_absolute():
            path = Path(context.cwd) / path
        path = path.resolve()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(output=f"Error writing file: {exc}", is_error=True)

        return ToolResult(
            output=f"Successfully wrote {len(content)} characters to {path}",
            metadata={"resolved_path": str(path), "bytes_written": len(content)},
        )
```

### 5e. `AskUserQuestionTool` — the most important tool

This tool is conceptually different from all the others: instead of doing work, it asks the human for information. This prevents the model from guessing when a guess would be unsafe or ambiguous.

```python
# agent/tools.py  (continued)

class AskUserQuestionTool(BaseTool):
    name = "ask_user_question"
    description = (
        "Ask the user a clarifying question when you need information "
        "that is not available in the conversation. "
        "Use this instead of guessing or assuming. "
        "Examples: choosing between multiple files, confirming a target environment, "
        "requesting missing parameters."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user.",
            }
        },
        "required": ["question"],
    }

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        question = arguments.get("question", "").strip()
        if not question:
            return ToolResult(
                output="Error: 'question' argument is required.",
                is_error=True,
            )

        # Use the callback from context if available
        if context.ask_user is not None:
            answer = await context.ask_user(question)
            return ToolResult(output=answer or "(no response provided)")

        # Fallback: synchronous stdin read (works in simple CLI mode)
        print(f"\n[agent question] {question}")
        try:
            answer = input("your answer> ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        return ToolResult(output=answer or "(no response provided)")
```

**Why this tool matters:**

```
WITHOUT ask_user_question:
  model sees 3 matching files → guesses → edits the wrong one → disaster

WITH ask_user_question:
  model sees 3 matching files → asks "Which file: A, B, or C?"
  → user answers → model proceeds with certainty
```

---

## 6. Update `ToolRegistry` and `default_registry`

```python
# agent/tools.py  (continued)

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools.keys())


def default_registry() -> ToolRegistry:
    """Return a ToolRegistry pre-loaded with all default tools."""
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    registry.register(EchoTool())
    registry.register(ReadFileTool())
    registry.register(GlobTool())
    registry.register(WriteFileTool())
    registry.register(AskUserQuestionTool())
    return registry
```

**Change from Chapter 01:** `register()` now raises `ValueError` on duplicate names. This catches mistakes early — a silent overwrite would be confusing to debug.

---

## 7. Update `Agent` to build and pass context

Open `agent/agent.py`. The loop from Chapter 01 does not change structurally — you only add context construction and pass it into `tool.execute()`.

```python
# agent/agent.py  — full updated file

from __future__ import annotations
from typing import AsyncGenerator, Any

from agent.models import Message, ModelResponse, ToolExecutionContext
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
    def __init__(
        self,
        model_client: Any,
        tool_registry: ToolRegistry,
        system_prompt: str = "You are a helpful assistant.",
        cwd: str | None = None,
    ) -> None:
        self.model_client = model_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd or __import__("os").getcwd()
        self.messages: list[Message] = []

    def _build_context(self, ask_user_fn=None) -> ToolExecutionContext:
        """Build the execution context for this turn."""
        return ToolExecutionContext(
            cwd=self.cwd,
            ask_user=ask_user_fn,
            metadata={"turn": len(self.messages)},
        )

    async def run(self, user_text: str) -> AsyncGenerator[AgentEvent, None]:
        self.messages.append(Message.user(user_text))
        yield StatusEvent(message="Thinking...")

        context = self._build_context()   # ← new: build context once per turn

        while True:
            try:
                response: ModelResponse = await self.model_client.complete(
                    messages=self.messages,
                    tools=self.tool_registry.schemas(),
                    system_prompt=self.system_prompt,
                )
            except Exception as exc:
                yield ErrorEvent(message="Model call failed.", details=str(exc))
                return

            if response.text:
                self.messages.append(Message.assistant(response.text))
                yield AssistantTextDelta(text=response.text)

            if not response.wants_tool:
                return

            for tool_call in response.tool_calls:
                yield ToolExecutionStarted(
                    tool_name=tool_call.name,
                    tool_input=tool_call.input,
                )

                tool = self.tool_registry.get(tool_call.name)
                if tool is None:
                    result_text = f"Error: tool '{tool_call.name}' is not registered."
                    is_error = True
                    result_metadata: dict[str, Any] = {}
                else:
                    try:
                        result = await tool.execute(tool_call.input, context)  # ← pass context
                        result_text = result.output
                        is_error = result.is_error
                        result_metadata = result.metadata
                    except Exception as exc:
                        result_text = f"Tool raised an exception: {exc}"
                        is_error = True
                        result_metadata = {}

                self.messages.append(
                    Message.tool_result(tool_call.id, result_text)
                )

                yield ToolExecutionCompleted(
                    tool_name=tool_call.name,
                    output=result_text,
                    is_error=is_error,
                    metadata=result_metadata,   # ← thread metadata through
                )
```

---

## 8. Update `ToolExecutionCompleted` to carry metadata

Open `agent/events.py` and add `metadata` to `ToolExecutionCompleted`:

```python
# agent/events.py  — updated ToolExecutionCompleted

from typing import Any
from dataclasses import dataclass, field

@dataclass(slots=True, frozen=True)
class ToolExecutionCompleted:
    tool_name: str
    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
```

> **Note:** `frozen=True` with a `dict` field works as long as you never mutate the dict post-construction. If you want strict immutability, use `tuple` or `types.MappingProxyType` instead.

---

## 9. Bridge to OpenAI-compatible wire format

This is the key connection point to real LLMs. Create `agent/adapters.py`:

```python
# agent/adapters.py
from __future__ import annotations

import json
from typing import Any

from agent.models import Message, ModelResponse, ToolCall
from agent.tools import BaseTool


# ── Request-side: internal → OpenAI wire format ───────────────────────────────

def tool_to_openai(tool: BaseTool) -> dict[str, Any]:
    """
    Convert a BaseTool to an OpenAI-compatible function definition.

    Internal format:
        {"name": ..., "description": ..., "input_schema": {...}}

    OpenAI format:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,  # JSON Schema — same format
        },
    }


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    """
    Convert internal Message list to OpenAI wire format.

    Handles three message types:
    - user text
    - assistant text
    - tool result (role="tool" in OpenAI format)
    """
    result = []
    for msg in messages:
        for block in msg.content:
            if block["type"] == "text":
                result.append({"role": msg.role, "content": block["text"]})
            elif block["type"] == "tool_result":
                result.append({
                    "role": "tool",
                    "tool_call_id": block["tool_call_id"],
                    "content": block["content"],
                })
    return result


# ── Response-side: OpenAI wire format → internal ─────────────────────────────

def openai_response_to_model_response(choice) -> ModelResponse:
    """
    Parse an OpenAI chat completion choice into a ModelResponse.

    Handles both plain text replies and tool_call requests.
    """
    message = choice.message
    tool_calls: list[ToolCall] = []

    for tc in message.tool_calls or []:
        try:
            parsed_args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            parsed_args = {}

        tool_calls.append(
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                input=parsed_args,
            )
        )

    return ModelResponse(
        text=message.content or "",
        tool_calls=tool_calls,
    )
```

### 9a. A real `OpenAIModelClient`

Now create a drop-in replacement for `DemoModelClient` in `agent/client.py`:

```python
# agent/client.py  — add below DemoModelClient

try:
    from openai import AsyncOpenAI
    _openai_available = True
except ImportError:
    _openai_available = False

from agent.adapters import messages_to_openai, openai_response_to_model_response
from agent.tools import ToolRegistry


class OpenAIModelClient:
    """
    Real model client for any OpenAI-compatible API.

    Works with:
    - OpenAI (api.openai.com)
    - Azure OpenAI
    - Local servers: Ollama, LM Studio, vLLM (set base_url)
    - Anthropic via OpenAI-compatible endpoint

    Usage:
        client = OpenAIModelClient(model="gpt-4o", api_key="sk-...")
        agent = Agent(client, registry)
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if not _openai_available:
            raise RuntimeError("Install openai: pip install openai")
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(
        self,
        *,
        messages: list,
        tools: list[dict],
        system_prompt: str,
    ) -> "ModelResponse":
        from agent.models import ModelResponse

        openai_messages = [
            {"role": "system", "content": system_prompt},
            *messages_to_openai(messages),
        ]

        # Convert internal tool schemas to OpenAI function format
        from agent.adapters import tool_to_openai
        from agent.tools import BaseTool

        # tools is already a list of schema dicts from registry.schemas()
        # We need to wrap them in OpenAI's "type":"function" envelope
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ] if tools else []

        response = await self._client.chat.completions.create(
            model=self.model,
            messages=openai_messages,
            tools=openai_tools or None,
            tool_choice="auto" if openai_tools else None,
        )

        return openai_response_to_model_response(response.choices[0])
```

**To use it:**

```bash
pip install openai
```

```python
# main.py  — swap one line
# client = DemoModelClient()
import os
client = OpenAIModelClient(model="gpt-4o", api_key=os.environ["OPENAI_API_KEY"])
```

Nothing else changes. The `Agent`, `ToolRegistry`, and all tools are completely unaware of which client is in use.

---

## 10. Update `main.py` to wire the full system

```python
# main.py  — updated for Chapter 02

import asyncio
import os

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


async def render(event: object) -> None:
    if isinstance(event, StatusEvent):
        print(f"  · {event.message}")

    elif isinstance(event, ToolExecutionStarted):
        args = event.tool_input or {}
        args_display = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "no args"
        print(f"  ⚙ {event.tool_name}({args_display})")

    elif isinstance(event, ToolExecutionCompleted):
        icon = "✗" if event.is_error else "✓"
        # Show first 120 chars of output to keep terminal readable
        preview = event.output[:120].replace("\n", "↵")
        print(f"  {icon} {event.tool_name} → {preview}")
        if event.metadata:
            meta_str = "  , ".join(f"{k}={v}" for k, v in event.metadata.items())
            print(f"    [{meta_str}]")

    elif isinstance(event, AssistantTextDelta):
        print(f"\nagent> {event.text}\n")

    elif isinstance(event, ErrorEvent):
        print(f"\n[ERROR] {event.message}")
        if event.details:
            print(f"        {event.details}")


async def repl(agent: Agent) -> None:
    print(f"Agent ready. Tools: {agent.tool_registry.names()}")
    print(f"Working directory: {agent.cwd}")
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

        async for event in agent.run(user_input):
            await render(event)


async def main() -> None:
    client = DemoModelClient()          # swap for OpenAIModelClient for real LLM
    registry = default_registry()
    agent = Agent(
        model_client=client,
        tool_registry=registry,
        system_prompt=(
            "You are a helpful assistant with access to filesystem tools. "
            "When you need information that you do not have, use ask_user_question "
            "instead of guessing."
        ),
        cwd=os.getcwd(),
    )
    await repl(agent)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 11. Run it and test the tools

```bash
python main.py
```

Test `ReadFileTool` (reads a real file):

```
you> read the file main.py
  · Thinking...
  ⚙ read_file(file_path='main.py')
  ✓ read_file → import asyncio↵import os↵↵from agent.agent import Agent...
    [resolved_path=/home/user/project/main.py, bytes_read=812]
```

Test `GlobTool`:

```
you> list all python files
  · Thinking...
  ⚙ glob(pattern='**/*.py')
  ✓ glob → agent/__init__.py↵agent/models.py↵agent/tools.py↵...
    [pattern=**/*.py, files_found=7]
```

Test `AskUserQuestionTool` (fake model routes "choose" to this tool):

```
you> choose a file for me
  · Thinking...
  ⚙ ask_user_question(question='Which file would you like me to work with?')

[agent question] Which file would you like me to work with?
your answer> main.py
  ✓ ask_user_question → main.py
```

---

## 12. Input validation patterns

Every tool should validate its inputs before doing any I/O. The pattern is consistent:

```python
async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:

    # 1. Extract and check required fields
    file_path = arguments.get("file_path", "").strip()
    if not file_path:
        return ToolResult(output="Error: 'file_path' is required.", is_error=True)

    # 2. Coerce and validate types
    max_lines = arguments.get("max_lines", 100)
    try:
        max_lines = int(max_lines)
    except (TypeError, ValueError):
        return ToolResult(output="Error: 'max_lines' must be an integer.", is_error=True)

    if max_lines < 1:
        return ToolResult(output="Error: 'max_lines' must be >= 1.", is_error=True)

    # 3. Proceed with clean, validated values
    ...
```

**Rule:** validate → then act. Never act on unvalidated model-provided inputs.

---

## 13. The read-only vs mutating distinction

Mark mutating tools clearly in their `description` and keep track of them:

```python
# A simple way to distinguish tool types

READ_ONLY_TOOLS = {"get_time", "read_file", "glob", "echo"}
MUTATING_TOOLS  = {"write_file", "bash"}
INTERACTIVE_TOOLS = {"ask_user_question"}
```

This distinction matters in Chapter 07 (permissions) where the runtime may:
- allow read-only tools without confirmation
- require user approval before mutating tools execute
- log mutating operations differently

You do not need the full permission system yet, but labeling tools now costs nothing and pays off later.

---

## 14. Common mistakes and fixes

### Mistake 1 — Raising exceptions inside `execute()`

```python
# WRONG — unhandled exceptions crash the loop
async def execute(self, arguments, context):
    content = Path(arguments["file_path"]).read_text()  # raises FileNotFoundError!
    return ToolResult(output=content)
```

**Fix:** wrap all I/O in `try/except` and return `ToolResult(is_error=True)`.

### Mistake 2 — Using `os.getcwd()` directly inside a tool

```python
# WRONG — ignores context, not testable, not sandboxable
async def execute(self, arguments, context):
    path = Path(os.getcwd()) / arguments["file_path"]  # uses global state
```

**Fix:** always use `Path(context.cwd) / arguments["file_path"]`.

### Mistake 3 — Sending `metadata` content to the model

```python
# WRONG — metadata pollutes the model's context
result = ToolResult(output=f"{content}\n\nMetadata: {metadata}")
```

**Fix:** put runtime data in `metadata`. Put model-readable output in `output`. They serve different audiences.

### Mistake 4 — Not adding `ask_user_question` to the registry

Without this tool, the model's only option when it is unsure is to guess. Guesses cause wrong mutations. Always include `AskUserQuestionTool`.

---

## 15. Exercises

**Exercise A — `ReadFileTool` with line range**

Add two optional fields to `ReadFileTool`:
- `start_line: int` (1-based, default 1)
- `end_line: int` (default: read all)

Return only the requested line range and include `{"start_line": x, "end_line": y}` in metadata.

**Exercise B — `GrepTool`**

Create a `GrepTool` that searches for a pattern inside files:

```python
input_schema = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string"},
        "file_path": {"type": "string"},
        "case_sensitive": {"type": "boolean"},
    },
    "required": ["pattern", "file_path"],
}
```

Use Python's `re` module. Return matching lines with line numbers.

**Exercise C — Tool timing**

Add timing to every tool execution. In `Agent._build_context`, pass a `start_time`. After `tool.execute()` returns, compute the duration and add it to `ToolExecutionCompleted.metadata`:

```python
metadata = {**result.metadata, "duration_ms": elapsed_ms}
```

**Exercise D — Connect a real LLM**

Replace `DemoModelClient` with `OpenAIModelClient`. Ask the agent:

```
you> list all python files in this directory and summarize what each one does
```

Watch it use `glob` and `read_file` automatically, without you programming that sequence.

---

## 16. Full updated file structure

```
agent/
    __init__.py
    models.py      ← Message, ToolCall, ModelResponse, ToolResult, ToolExecutionContext
    tools.py       ← BaseTool, ToolRegistry, GetTimeTool, EchoTool,
                      ReadFileTool, GlobTool, WriteFileTool, AskUserQuestionTool
    events.py      ← all events (ToolExecutionCompleted now has metadata)
    adapters.py    ← tool_to_openai(), messages_to_openai(), openai_response_to_model_response()
    client.py      ← DemoModelClient, OpenAIModelClient
    agent.py       ← Agent class (passes context to tools)
main.py
```

---

## 17. Checklist before moving on

- [ ] `ToolResult` has `output`, `is_error`, and `metadata` fields
- [ ] `ToolExecutionContext` has `cwd`, `ask_user`, and `metadata`
- [ ] `BaseTool.execute()` accepts `(arguments, context)` — both params  
- [ ] Every tool validates its required arguments before doing I/O
- [ ] All I/O is wrapped in `try/except` — tools never raise
- [ ] Paths are resolved against `context.cwd`, not `os.getcwd()`
- [ ] `AskUserQuestionTool` is registered and uses `context.ask_user` if set
- [ ] `ToolRegistry.register()` raises on duplicate tool names
- [ ] `agent/adapters.py` converts internal types to/from OpenAI wire format
- [ ] `ToolExecutionCompleted` events carry metadata through to the renderer
- [ ] Mutating tools are labeled clearly in their description
- [ ] `ToolResult` includes `is_recoverable` to distinguish retryable errors
- [ ] Error messages say what went wrong, why, and what to try next
- [ ] Mutating tools are idempotent: writing the same content twice is safe

### Three best practices for tool error handling

**1. Add `is_recoverable` to `ToolResult`**

The model treats all `is_error=True` results the same — it just retries. Add a flag to tell the loop whether retrying is useful:

```python
# agent/models.py or agent/tools.py  — update ToolResult
@dataclass(slots=True, frozen=True)
class ToolResult:
    output: str
    is_error: bool = False
    is_recoverable: bool = True    # ← new: False = don't retry (e.g. permission denied)
    metadata: dict = field(default_factory=dict)
```

Set `is_recoverable=False` for: permission denied, hard-denied paths, missing required arguments. Set `is_recoverable=True` for: file not found (model might try a different path), network timeout (might succeed on retry).

**2. Write error messages for the model, not for logs**

The error text is the model's only feedback. It must tell the model what to do next:

```python
# BAD — model has no idea what to try
return ToolResult(output="File not found.", is_error=True)

# GOOD — model can act on this
return ToolResult(
    output=(
        f"File not found: '{file_path}'\n"
        f"Tip: Use the glob tool to list available files in the parent directory first, "
        f"then read the correct filename."
    ),
    is_error=True,
    is_recoverable=True,
)
```

**3. Mutating tools must be idempotent**

If the model calls `write_file` twice with identical content (a common retry after a tool error), it should not corrupt state. `WriteFileTool.execute()` should check if content already matches:

```python
async def execute(self, arguments, context) -> ToolResult:
    path = Path(arguments["file_path"])
    content = arguments["content"]
    # Idempotency check — skip write if content already matches
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return ToolResult(
            output=f"File already has this content. No write needed: '{path}'",
            metadata={"resolved_path": str(path), "bytes_written": 0, "skipped": True},
        )
    path.write_text(content, encoding="utf-8")
    return ToolResult(
        output=f"Successfully wrote {len(content)} characters to '{path}'",
        metadata={"resolved_path": str(path), "bytes_written": len(content)},
    )
```

---

Next: [02-1-mcp-integration.md](02-1-mcp-integration.md) — connect external tool servers via MCP, then [02-2-plugins.md](02-2-plugins.md) for plugin packages, then continue to [03-session-manager.md](03-session-manager.md).

