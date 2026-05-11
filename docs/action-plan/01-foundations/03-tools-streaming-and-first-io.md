# Chapter 3: Tools, Streaming, And First IO

## Objective

Give the harness useful capabilities without losing control. In this chapter you add a small tool system, a tool registry, runtime execution context, and token streaming so the harness starts to feel interactive rather than batch-oriented.

This chapter combines several tutorial improvements:

- `openai-code-tutorial` adds practical tool abstractions and streaming-specific interfaces
- `agentic-framework-tutorial` reinforces why responsive streaming and explicit tool orchestration matter in production systems

## Build The Tool Interface

Your tools should be small, boring, and explicit.

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# ToolResult and Message live in models.py; import them from there:
# from models import Message, ModelResponse, ToolCall, ToolResult


@dataclass(slots=True)
class ToolExecutionContext:
    session_id: str
    working_directory: Path
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool(Protocol):
    name: str
    description: str
    is_mutating: bool

    async def execute(
        self,
        call_id: str,              # taken from ToolCall.call_id, not from arguments
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        ...
```

Two design choices matter here:

- `ToolExecutionContext` carries runtime details that the model should not have to invent
- `is_mutating` lets the harness enforce scheduling and permissions later
- `call_id` is passed separately so tools never need to read it out of the arguments dict

## Build The Registry

```python
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise LookupError(f"Unknown tool: {name}") from exc

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())
```

The registry should stay dumb. It stores tools and returns them. It should not perform permissions, logging, or policy checks.

## Add Two First Tools

Start with one safe read tool and one tiny mutation tool.

```python
from datetime import datetime, UTC


class GetTimeTool:
    name = "get_time"
    description = "Return the current UTC timestamp."
    is_mutating = False

    async def execute(self, call_id: str, arguments: dict, context: ToolExecutionContext) -> ToolResult:
        now = datetime.now(UTC).isoformat()
        return ToolResult(call_id=call_id, tool_name=self.name, output=now)


class WriteNoteTool:
    name = "write_note"
    description = "Write a short note into the current working directory."
    is_mutating = True

    async def execute(self, call_id: str, arguments: dict, context: ToolExecutionContext) -> ToolResult:
        target = context.working_directory / arguments["path"]
        target.write_text(arguments["content"], encoding="utf-8")
        return ToolResult(call_id=call_id, tool_name=self.name, output=f"Wrote {target}")
```

`call_id` always comes from the `ToolCall` object emitted by the model. Passing it explicitly into `execute()` keeps tools decoupled from the message envelope format.

## Execute Tool Calls In The Agent Loop

Now the agent loop can actually do work.

```python
class Agent:
    def __init__(self, model_client, tool_registry: ToolRegistry) -> None:
        self.model_client = model_client
        self.tool_registry = tool_registry

    async def run(self, messages: list[Message], context: ToolExecutionContext):
        response = await self.model_client.complete(messages)
        yield {"event": "model_response", "value": response}

        for tool_call in response.tool_calls:
            tool = self.tool_registry.get(tool_call.tool_name)
            # Pass call_id from ToolCall, not from arguments
            result = await tool.execute(tool_call.call_id, tool_call.arguments, context)
            yield {"event": "tool_result", "value": result}
```

This is still intentionally minimal. Permissions, hooks, and parallel scheduling will come later.

## Add Streaming To The Model Interface

One of the most important `openai-code-tutorial` improvements is making streaming a first-class interface, not an afterthought.

```python
from collections.abc import AsyncIterator


class ModelClient(Protocol):
    async def complete(self, messages: list[Message]) -> ModelResponse:
        ...

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        ...
```

The streaming interface should emit chunks for rendering. It should not secretly execute tools or mutate history.

## Render Streaming In The REPL

```python
async def render_stream(agent: Agent, history: list[Message]) -> None:
    async for chunk in agent.model_client.stream(history):
        print(chunk, end="", flush=True)
    print()
```

Later you will combine this with structured runtime events. For now the main lesson is architectural: streaming belongs to output rendering, not to the business rules of tool execution.

## Keep One Deliberate Limitation

Do not implement parallel tool execution in this chapter yet. The production tutorials correctly show that parallel read-only tools can improve latency, but you first need:

- mutating vs read-only classification
- permission decisions
- event logging
- predictable error handling

Without those, concurrency creates confusion instead of speed.

## Action Plan

1. Add a `BaseTool` protocol and `ToolExecutionContext`.
2. Implement a dumb `ToolRegistry`.
3. Add one read-only tool and one mutating tool.
4. Execute tools from the agent loop.
5. Extend the model interface with a streaming method.
6. Render streamed chunks in the REPL without mixing them into policy logic.

## Validation Checklist

- The agent can execute at least one registered tool.
- Tool execution receives runtime context explicitly.
- Streaming works even if no tool is called.
- A tool result can be shown to the user or fed back into history.
- The registry does not contain permission logic.

## Definition Of Done

You are ready for the next stage when your harness can do all of the following in one session:

- accept a user request
- ask the model for a next step
- execute a known tool
- render a visible result
- stream text output without freezing the terminal

## Current Nexus Notes

The current Nexus tool surface has grown well beyond the two-tool sketch in this chapter. All tools live in `nexus/tools/` and are registered in `nexus/app.py` via `_build_registry`. Every tool goes through `PermissionChecker.evaluate` before execution and fires `PRE_TOOL_USE` / `POST_TOOL_USE` hooks for observability.

**Full builtin tool set (`nexus/tools/builtin.py` and `nexus/tools/filesystem.py`):**

| Tool | `is_mutating` | Risk | Notes |
|---|---|---|---|
| `get_time` | No | low | UTC timestamp. |
| `write_note` | Yes | medium | Short note file; payload-size cap; workspace restricted. |
| `read_file` | No | low | File contents or a line range; workspace restricted. |
| `write_file` | Yes | **high** | Full file create / overwrite; always confirmed (even in auto mode). |
| `modify_file` | Yes | medium | Replace a line range in an existing file. |
| `replace_text` | Yes | medium | Literal string find-and-replace; first or all occurrences. |
| `glob` | No | low | Glob pattern search within the workspace (`**/*.py`). |
| `grep` | No | low | Regex or fixed-string search across files; returns `file:line: content`. |
| `ls` | No | low | Directory listing with sizes; hides dotfiles by default. |
| `bash` | Yes | **dynamic** | Runs a shell command; risk is classified per command string. |

**Bash risk classifier (`nexus/tools/filesystem.py: classify_bash_risk`):**

The classifier evaluates the raw command string against ordered regex sets:

1. HIGH: `rm -rf`, `sudo`, `su`, `dd if=`, `mkfs`, `fdisk`, pipe-to-shell (`| bash`), `kill -9`, `killall`, recursive `chmod`/`chown`, writes to system paths.
2. MEDIUM: `rm` (single file), `mv`, `cp`, `touch`, `mkdir`, `chmod`, `sed -i`, `tee`, any `>` output redirect, `git add/commit/push/reset/rebase`, package installs.
3. LOW: command parses cleanly and the leading token is in a known read-only set (`cat`, `grep`, `echo`, `ls`, `git status`, `git log`, …).
4. MEDIUM (default): unknown commands.

The permission decision driven by risk level:
- LOW → `ALLOW` in all modes
- MEDIUM → `DENY` in plan, `CONFIRM` in default, `ALLOW` in auto
- HIGH → `DENY` in plan, `CONFIRM` in default **and** auto (auto mode cannot bypass high-risk)

The risk level is also recorded in `ToolResult.metadata["risk"]` for every bash execution.

That is the first real version of a minimal agent harness.