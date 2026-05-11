# 04 — Hooks: Lifecycle Extension Points

## Prerequisites

Complete [03-session-manager.md](03-session-manager.md) first.

Your project should have:
```
agent/models.py, tools.py, events.py, adapters.py, client.py, agent.py, session.py
main.py
```

Right now, the agent loop runs cleanly but there is no way to attach extra behaviour — logging, blocking, notifications — without editing the loop itself. Every new cross-cutting concern would mean touching `agent.py`.

**Hooks fix this.** They give you stable lifecycle extension points where you can plug in logging, policy checks, notifications, or anything else — *without* touching the loop.

---

## What you will build

```
agent/
    hooks.py       ← NEW: HookEvent, HookResult, Hook protocol, HookExecutor
    agent.py       ← updated: emits hook events at key boundaries
main.py            ← updated: registers example hooks
```

After this chapter your agent will:
- Fire a hook when the user submits a prompt
- Fire a hook before every tool call (can block execution)
- Fire a hook after every tool call
- Fire a hook when each turn ends
- All hooks run through one central `HookExecutor`

---

## 1. The five lifecycle events you need first

Every hook is tied to one named event. Start with five:

| Event | When it fires | Can block? |
|---|---|---|
| `user_prompt_submit` | User sends a prompt | No |
| `pre_tool_use` | Just before a tool executes | **Yes** |
| `post_tool_use` | After a tool finishes | No |
| `stop` | Agent turn is complete | No |
| `notification` | Runtime wants to surface a message | No |

`pre_tool_use` is the most important one — it is where you can block a dangerous action before any side effects occur.

---

## 2. Create `agent/hooks.py`

```python
# agent/hooks.py

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


# ── Event enum ────────────────────────────────────────────────────────────────

class HookEvent(str, Enum):
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE       = "pre_tool_use"
    POST_TOOL_USE      = "post_tool_use"
    STOP               = "stop"
    NOTIFICATION       = "notification"


# ── Payload helpers ───────────────────────────────────────────────────────────

def prompt_payload(user_text: str) -> dict[str, Any]:
    return {"event": HookEvent.USER_PROMPT_SUBMIT, "text": user_text}

def pre_tool_payload(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"event": HookEvent.PRE_TOOL_USE, "tool_name": tool_name, "tool_input": tool_input}

def post_tool_payload(tool_name: str, output: str, is_error: bool) -> dict[str, Any]:
    return {"event": HookEvent.POST_TOOL_USE, "tool_name": tool_name, "output": output, "is_error": is_error}

def stop_payload(turn: int, tool_calls: int) -> dict[str, Any]:
    return {"event": HookEvent.STOP, "turn": turn, "tool_calls": tool_calls}

def notification_payload(message: str, level: str = "info") -> dict[str, Any]:
    return {"event": HookEvent.NOTIFICATION, "message": message, "level": level}


# ── Hook result ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class HookResult:
    """
    What a hook returns after running.

    blocked  — if True, the runtime stops the current action
    reason   — human-readable explanation (shown to user and model)
    outputs  — extra text the hook wants to surface
    """
    blocked: bool = False
    reason: str = ""
    outputs: list[str] = field(default_factory=list)

    @classmethod
    def allow(cls) -> "HookResult":
        return cls()

    @classmethod
    def block(cls, reason: str) -> "HookResult":
        return cls(blocked=True, reason=reason)

    @classmethod
    def emit(cls, *messages: str) -> "HookResult":
        return cls(outputs=list(messages))


# ── Aggregated result from all hooks for one event ────────────────────────────

@dataclass
class AggregatedHookResult:
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)

    @property
    def block_reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "blocked by hook"


# ── Hook protocol ─────────────────────────────────────────────────────────────

class Hook(Protocol):
    """Every hook must have an event and an async run() method."""
    event: HookEvent

    async def run(self, payload: dict[str, Any]) -> HookResult:
        ...


# ── Hook executor ─────────────────────────────────────────────────────────────

class HookExecutor:
    """
    Central dispatcher for all registered hooks.

    Usage:
        executor = HookExecutor()
        executor.register(MyLoggingHook())
        result = await executor.execute(HookEvent.PRE_TOOL_USE, payload)
        if result.blocked:
            # stop execution
    """

    def __init__(self) -> None:
        self._hooks: dict[HookEvent, list[Hook]] = defaultdict(list)

    def register(self, hook: Hook) -> None:
        self._hooks[hook.event].append(hook)

    def hooks_for(self, event: HookEvent) -> list[Hook]:
        return self._hooks[event]

    async def execute(
        self, event: HookEvent, payload: dict[str, Any]
    ) -> AggregatedHookResult:
        """
        Run all hooks registered for this event.

        Aggregates results:
        - if ANY hook blocks → the aggregated result is blocked
        - outputs from all hooks are collected
        """
        aggregated = AggregatedHookResult()

        for hook in self._hooks.get(event, []):
            try:
                result = await hook.run(payload)
            except Exception as exc:
                # A crashing hook should not crash the agent
                aggregated.outputs.append(f"[hook error] {hook.__class__.__name__}: {exc}")
                continue

            if result.blocked:
                aggregated.blocked = True
                if result.reason:
                    aggregated.reasons.append(result.reason)

            aggregated.outputs.extend(result.outputs)

        return aggregated
```

**Why crash-safe hooks?** A hook that throws should never bring down the agent loop. Wrapping hook execution in `try/except` keeps the core runtime stable regardless of hook quality.

---

## 3. Write some useful hooks

Put these in a new file `agent/builtin_hooks.py` — or keep them in `main.py` for now:

```python
# agent/builtin_hooks.py

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.hooks import HookEvent, HookResult


logger = logging.getLogger("agent.hooks")


class LoggingHook:
    """Logs every tool execution to Python's logging system."""
    event = HookEvent.POST_TOOL_USE

    async def run(self, payload: dict[str, Any]) -> HookResult:
        tool = payload.get("tool_name", "?")
        ok = not payload.get("is_error", False)
        icon = "✓" if ok else "✗"
        logger.info(f"{icon} tool={tool}")
        return HookResult.allow()


class DenyListHook:
    """
    Blocks a configurable list of tool names before they run.

    Example:
        hook = DenyListHook(denied={"bash", "write_file"})
    """
    event = HookEvent.PRE_TOOL_USE

    def __init__(self, denied: set[str]) -> None:
        self.denied = denied

    async def run(self, payload: dict[str, Any]) -> HookResult:
        tool_name = payload.get("tool_name", "")
        if tool_name in self.denied:
            return HookResult.block(
                reason=f"Tool '{tool_name}' is in the deny list."
            )
        return HookResult.allow()


class AuditLogHook:
    """
    Appends every tool call to a plain-text audit log file.
    Useful for reviewing what the agent actually did.
    """
    event = HookEvent.POST_TOOL_USE

    def __init__(self, log_path: str = "audit.log") -> None:
        self.log_path = log_path

    async def run(self, payload: dict[str, Any]) -> HookResult:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tool = payload.get("tool_name", "?")
        is_error = payload.get("is_error", False)
        output_preview = payload.get("output", "")[:80].replace("\n", "↵")
        line = f"{now} | {'ERR' if is_error else 'OK '} | {tool} | {output_preview}\n"
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line)
        return HookResult.allow()


class PromptLogHook:
    """Prints each user prompt to stdout with a timestamp."""
    event = HookEvent.USER_PROMPT_SUBMIT

    async def run(self, payload: dict[str, Any]) -> HookResult:
        now = datetime.now(timezone.utc).strftime("%H:%M:%S")
        text = payload.get("text", "")[:60]
        return HookResult.emit(f"[{now}] prompt: {text!r}")


class TurnSummaryHook:
    """Emits a summary at the end of each turn."""
    event = HookEvent.STOP

    async def run(self, payload: dict[str, Any]) -> HookResult:
        turn = payload.get("turn", 0)
        calls = payload.get("tool_calls", 0)
        return HookResult.emit(f"Turn {turn} complete. Tool calls this session: {calls}")
```

---

## 4. Update `Agent` to fire hooks

Open `agent/agent.py`. Add `HookExecutor` to `__init__` and fire hooks at the right places:

```python
# agent/agent.py  — updated

from __future__ import annotations
from typing import AsyncGenerator, Any

from agent.models import Message, ModelResponse, ToolExecutionContext, SessionSnapshot
from agent.tools import ToolRegistry
from agent.hooks import HookEvent, HookExecutor, AggregatedHookResult
from agent.hooks import (
    prompt_payload, pre_tool_payload, post_tool_payload,
    stop_payload, notification_payload,
)
from agent.events import (
    AgentEvent, AssistantTextDelta, ErrorEvent,
    StatusEvent, ToolExecutionCompleted, ToolExecutionStarted,
)


class Agent:
    def __init__(
        self,
        model_client: Any,
        tool_registry: ToolRegistry,
        system_prompt: str = "You are a helpful assistant.",
        cwd: str | None = None,
        model_name: str = "demo",
        hook_executor: HookExecutor | None = None,   # ← new
    ) -> None:
        self.model_client = model_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd or __import__("os").getcwd()
        self.model_name = model_name
        self.hooks = hook_executor or HookExecutor()  # ← new
        self.messages: list[Message] = []
        self._turn_count: int = 0
        self._tool_call_count: int = 0
        self._snapshot: SessionSnapshot | None = None

    async def run(self, user_text: str) -> AsyncGenerator[AgentEvent, None]:
        self.messages.append(Message.user(user_text))
        self._turn_count += 1

        # ── Hook: user_prompt_submit ──────────────────────────────────────────
        hook_result = await self.hooks.execute(
            HookEvent.USER_PROMPT_SUBMIT, prompt_payload(user_text)
        )
        for msg in hook_result.outputs:
            yield StatusEvent(message=msg)

        yield StatusEvent(message=f"Thinking... (turn {self._turn_count})")
        context = self._build_context()

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
                # ── Hook: stop ────────────────────────────────────────────────
                await self.hooks.execute(
                    HookEvent.STOP,
                    stop_payload(self._turn_count, self._tool_call_count),
                )
                return

            for tool_call in response.tool_calls:
                self._tool_call_count += 1

                # ── Hook: pre_tool_use — can block ────────────────────────────
                pre_result = await self.hooks.execute(
                    HookEvent.PRE_TOOL_USE,
                    pre_tool_payload(tool_call.name, tool_call.input),
                )

                if pre_result.blocked:
                    blocked_msg = f"Blocked by policy: {pre_result.block_reason}"
                    self.messages.append(Message.tool_result(tool_call.id, blocked_msg))
                    yield ToolExecutionCompleted(
                        tool_name=tool_call.name,
                        output=blocked_msg,
                        is_error=True,
                    )
                    # Fire notification hook so the CLI can surface this
                    await self.hooks.execute(
                        HookEvent.NOTIFICATION,
                        notification_payload(blocked_msg, level="warning"),
                    )
                    continue

                yield ToolExecutionStarted(
                    tool_name=tool_call.name,
                    tool_input=tool_call.input,
                )

                tool = self.tool_registry.get(tool_call.name)
                if tool is None:
                    result_text = f"Error: tool '{tool_call.name}' is not registered."
                    is_error = True
                    result_metadata: dict = {}
                else:
                    try:
                        result = await tool.execute(tool_call.input, context)
                        result_text = result.output
                        is_error = result.is_error
                        result_metadata = result.metadata
                    except Exception as exc:
                        result_text = f"Tool raised an exception: {exc}"
                        is_error = True
                        result_metadata = {}

                self.messages.append(Message.tool_result(tool_call.id, result_text))

                # ── Hook: post_tool_use ───────────────────────────────────────
                post_result = await self.hooks.execute(
                    HookEvent.POST_TOOL_USE,
                    post_tool_payload(tool_call.name, result_text, is_error),
                )
                for msg in post_result.outputs:
                    yield StatusEvent(message=msg)

                yield ToolExecutionCompleted(
                    tool_name=tool_call.name,
                    output=result_text,
                    is_error=is_error,
                    metadata=result_metadata,
                )

    def _build_context(self, ask_user_fn=None) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=self.cwd,
            ask_user=ask_user_fn,
            metadata={"turn": self._turn_count},
        )

    # snapshot() and restore() unchanged from Chapter 03
    def snapshot(self, carry_over: dict | None = None) -> SessionSnapshot:
        if self._snapshot is None:
            self._snapshot = SessionSnapshot.new(
                cwd=self.cwd, model=self.model_name,
                system_prompt=self.system_prompt,
            )
        self._snapshot.messages = [m.to_dict() for m in self.messages]
        self._snapshot.usage = {"turns": self._turn_count, "tool_calls": self._tool_call_count}
        self._snapshot.carry_over = carry_over or self._snapshot.carry_over
        if not self._snapshot.summary and self.messages:
            self._snapshot.summary = self.messages[0].text[:80]
        return self._snapshot

    def restore(self, snapshot: SessionSnapshot) -> None:
        self._snapshot = snapshot
        self.messages = [Message.from_dict(m) for m in snapshot.messages]
        self._turn_count = snapshot.usage.get("turns", 0)
        self._tool_call_count = snapshot.usage.get("tool_calls", 0)
        self.cwd = snapshot.cwd
```

---

## 5. Update `main.py` to register hooks

```python
# main.py  — updated section (build_agent function only)

from agent.hooks import HookExecutor
from agent.builtin_hooks import LoggingHook, DenyListHook, AuditLogHook, TurnSummaryHook
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

def build_agent() -> Agent:
    client = DemoModelClient()
    registry = default_registry()

    # ── Build hook executor with default hooks ────────────────────────────────
    executor = HookExecutor()
    executor.register(LoggingHook())
    executor.register(AuditLogHook(log_path="audit.log"))
    executor.register(TurnSummaryHook())
    # Uncomment to block write_file without confirmation:
    # executor.register(DenyListHook(denied={"write_file"}))

    return Agent(
        model_client=client,
        tool_registry=registry,
        system_prompt="You are a helpful assistant.",
        cwd=__import__("os").getcwd(),
        model_name="demo",
        hook_executor=executor,    # ← pass it in
    )
```

---

## 6. Run and see hooks in action

```bash
python main.py
```

```
you> what time is it?
  · Thinking... (turn 1)
  ⚙ get_time(no args)
  ✓ get_time → 2026-04-25 08:12:01 UTC
  · Turn 1 complete. Tool calls this session: 1
  💾 saved → session-abc123.json
```

The audit log:
```bash
cat audit.log
2026-04-25T08:12:01Z | OK  | get_time | 2026-04-25 08:12:01 UTC
```

Test blocking:
```python
# In build_agent(), add:
executor.register(DenyListHook(denied={"write_file"}))
```

```
you> write hello to test.txt
  · Thinking... (turn 1)
  [Blocked by policy: Tool 'write_file' is in the deny list.]
```

The model receives the block reason as a tool result and adapts.

---

## 7. The key design rules for hooks

```
                    ┌──────────────────────────────┐
                    │         Agent.run()           │
                    │                               │
user prompt ──────► │ fire: user_prompt_submit      │
                    │          ▼                    │
                    │ model.complete()              │
                    │          ▼                    │
tool call ────────► │ fire: pre_tool_use  ◄─── can BLOCK
                    │          ▼ (if not blocked)   │
                    │ tool.execute()                │
                    │          ▼                    │
                    │ fire: post_tool_use            │
                    │          ▼                    │
                    │ (loop back)                   │
                    │          ▼ (when done)        │
                    │ fire: stop                    │
                    └──────────────────────────────┘
```

**Rules:**
1. Hooks extend the runtime — they never replace core loop logic
2. Payloads are plain dicts of primitives — easy to log and test
3. `HookExecutor` is the only dispatcher — hooks never call each other
4. Hook crashes are caught — a bad hook never kills the agent
5. Only `pre_tool_use` can block — other events are observe-only

---

## 8. Common mistakes and fixes

### Mistake 1 — Putting core logic inside a hook

```python
# WRONG — the hook is doing the tool's job
class BadHook:
    event = HookEvent.PRE_TOOL_USE
    async def run(self, payload):
        if payload["tool_name"] == "get_time":
            import datetime
            print(datetime.datetime.now())   # side effect inside hook!
        return HookResult.allow()
```

**Fix:** hooks observe and optionally block. They never perform the action themselves.

### Mistake 2 — Raising from a hook

```python
# WRONG — crashes the entire agent turn
async def run(self, payload):
    raise RuntimeError("something went wrong")
```

**Fix:** return `HookResult.allow()` or `HookResult.block(reason)`. Never raise. The executor already wraps hooks in `try/except` but it is better practice not to rely on that.

### Mistake 3 — Registering hooks after the agent has started running

Hooks must all be registered before the first `agent.run()` call. Registration during a live turn causes race conditions if you ever go async.

---

## 9. Exercises

**Exercise A — Rate-limiting hook**

Create `RateLimitHook` that fires on `user_prompt_submit` and blocks if the user has submitted more than 10 prompts in the last minute. Use a `deque` with `maxlen=10` to track timestamps.

**Exercise B — Sensitive path hook**

Create `SensitivePathHook` for `PRE_TOOL_USE`. If `tool_name` is `read_file` or `write_file` and `tool_input["file_path"]` contains `.ssh` or `.aws`, return `HookResult.block(reason="Sensitive path denied.")`.

**Exercise C — Hook output in the renderer**

`HookResult.outputs` are currently surfaced as `StatusEvent`. Update the renderer in `main.py` to display hook outputs with a different prefix, like `[hook] message`.

**Exercise D — Persistent hook log**

Create a `JSONAuditHook` that writes one JSON object per line to `audit.jsonl`, including timestamp, event type, tool name, input summary, and output summary. This format is easier to parse and analyze than plain text.

---

## 10. Checklist before moving on

- [ ] `HookEvent` enum has at least 5 events
- [ ] `HookResult` has `blocked`, `reason`, and `outputs` fields
- [ ] `HookExecutor.execute()` aggregates results from all hooks for one event
- [ ] Hook crashes are caught — they produce an output message, not a crash
- [ ] `pre_tool_use` fires before `tool.execute()` and can prevent it
- [ ] `post_tool_use` fires after every tool whether it succeeded or failed
- [ ] `stop` fires when the agent turn ends (no more tool calls)
- [ ] `Agent.__init__` accepts a `hook_executor` parameter
- [ ] Hooks are registered in `build_agent()`, not inside `Agent`
- [ ] `HookExecutor.execute()` wraps each hook in `asyncio.wait_for(timeout=5.0)` to prevent slow hooks from stalling the agent

### Improvement: async hook timeout

A hook that makes a slow network call can stall the entire agent turn. Add a timeout to `HookExecutor.execute()`:

```python
# agent/hooks.py  — update HookExecutor.execute()
import asyncio

async def execute(self, event: HookEvent, payload: dict[str, Any]) -> AggregatedHookResult:
    aggregated = AggregatedHookResult()
    for hook in self._hooks.get(event, []):
        try:
            result = await asyncio.wait_for(hook.run(payload), timeout=5.0)
        except asyncio.TimeoutError:
            aggregated.outputs.append(f"[hook timeout] {hook.__class__.__name__} exceeded 5s")
            continue
        except Exception as exc:
            aggregated.outputs.append(f"[hook error] {hook.__class__.__name__}: {exc}")
            continue
        if result.blocked:
            aggregated.blocked = True
            if result.reason:
                aggregated.reasons.append(result.reason)
        aggregated.outputs.extend(result.outputs)
    return aggregated
```

---

Next: [05-context-engineering.md](05-context-engineering.md)

