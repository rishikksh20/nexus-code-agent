# Honest Audit & Review — Minimal Agent Harness Tutorial Series

> **Written after a full chapter-by-chapter read, April 2026.**  
> This is a genuine critical review, not a flattery document. It covers pedagogy, code quality,
> concept accuracy, and gaps that **still remain** even after the earlier IMPROVEMENTS.md work.

---

## Verdict at a Glance

| Dimension | Score | Notes |
|---|---|---|
| Concept coverage | 9/10 | Almost everything important is here |
| Progressive difficulty | 6/10 | Several abrupt jumps; async transition is brutal |
| Code correctness | 7/10 | Several subtle bugs and antipatterns |
| Copy-paste-and-run factor | 5/10 | Many chapters leave the reader with incomplete files |
| Real-world applicability | 8/10 | The patterns are genuinely production-minded |
| Beginner friendliness (Ch00-02) | 8/10 | Chapter 00 is excellent; drops off fast |
| Beginner friendliness (Ch03+) | 4/10 | Rapidly assumes too much |

**Overall rating: 7/10** — A genuinely useful tutorial that teaches real production patterns but has real code issues and pedagogical rough edges that could frustrate learners.

---

## What This Tutorial Does Really Well

Before critiquing, acknowledge what genuinely stands out:

1. **Chapter 00 is excellent.** The progression from `echo > input` → fake model → structured dict → tool call is exactly right. It earns patience.

2. **The "two loops" distinction** in Chapter 01 (REPL loop vs agent loop) is the single most important concept in the series and it is explained perfectly with the ASCII box diagram.

3. **`pre_tool_use` hooks block execution before side effects.** This design insight from Chapter 04 is exactly right and often missed in tutorial series.

4. **File-based memory before vector databases.** The Chapter 06 rationale for starting with plain Markdown files is honest and pragmatic.

5. **Permission outcomes: ALLOW / CONFIRM / DENY.** The three-tier system from Chapter 07 matches real production needs and is taught well.

6. **The `FakeModelClient` scripted response pattern** from Chapter 14 is the correct approach to testing agent loops and is rare to see documented this clearly.

7. **Chapter 05's layered prompt assembly** is a genuinely sophisticated context engineering pattern and the tutorial explains it without hype.

---

## Part 1 — Critical Code Bugs and Antipatterns

These are issues that would cause a reader following the tutorial to write broken or dangerous code.

---

### Bug 1 — `frozen=True` with mutable `dict` fields is silently unsafe

**Where:** Chapters 01, 02 — `ToolResult`, `ToolExecutionCompleted`

```python
# Code in the tutorial:
@dataclass(slots=True, frozen=True)
class ToolResult:
    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
```

The tutorial adds a footnote that says *"as long as you never mutate it after construction"*. This is not enforced by Python. `frozen=True` only prevents attribute **reassignment** — it does NOT prevent mutating the dict's contents:

```python
result = ToolResult(output="ok", metadata={"key": "val"})
result.metadata["injected"] = "evil"  # This works! frozen=True does nothing here.
result.output = "changed"             # This raises FrozenInstanceError — frozen DOES block this
```

Any hook, renderer, or downstream code that keeps a reference to `result.metadata` can mutate event data that the agent loop considers "past fact". This can cause debugging nightmares.

**Fix:** Either drop `frozen=True` on types with dict fields, or use `types.MappingProxyType`:

```python
from types import MappingProxyType

@dataclass(slots=True)
class ToolResult:
    output: str
    is_error: bool = False
    _metadata: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def metadata(self) -> MappingProxyType:
        return MappingProxyType(self._metadata)
```

---

### Bug 2 — Infinite agent loop with no guard

**Where:** Every chapter's `Agent.run()` inner `while True` loop

```python
# In agent.py — this loop has no exit condition except the model stopping itself:
while True:
    response = await self.model_client.complete(...)
    if not response.wants_tool:
        return
    for tool_call in response.tool_calls:
        # ... execute tool, append result ...
    # loop back — no counter, no stop condition
```

If a real LLM enters a "stuck" state where it repeatedly calls the same tool regardless of the result (a known failure mode called tool-call loops), this loop never terminates. The session will run until the API token limit is hit, which can cost real money.

**Fix:** Add a max-iterations guard, which the tutorial never discusses:

```python
MAX_LOOP_ITERATIONS = 50  # Make this configurable via agent.toml

async def run(self, user_text: str):
    self.messages.append(Message.user(user_text))
    loop_count = 0

    while True:
        loop_count += 1
        if loop_count > MAX_LOOP_ITERATIONS:
            yield ErrorEvent(
                message=f"Agent loop exceeded {MAX_LOOP_ITERATIONS} iterations. Stopping.",
                details="This likely indicates a model stuck in a tool-call loop."
            )
            return
        # ... rest of loop
```

This is not mentioned anywhere in the tutorial, not even as a warning.

---

### Bug 3 — `__import__("os").getcwd()` antipattern in tutorial code

**Where:** Chapter 02, `agent/agent.py`

```python
# Actual code in the tutorial:
self.cwd = cwd or __import__("os").getcwd()
```

This is obfuscated code that serves no purpose. `import os` at the top of the file is standard Python. Using `__import__` inline is a code smell that should never appear in tutorial code — beginners may think this is a recommended practice.

**Fix:** Add `import os` at the top of the file. Simple.

---

### Bug 4 — Streaming chapter forward-references Chapter 02

**Where:** Chapter 01-1, `OpenAIStreamingClient._to_openai_messages()`

```python
def _to_openai_messages(self, messages, system_prompt):
    result = [{"role": "system", "content": system_prompt}]
    for m in messages:
        # ...convert Message objects to OpenAI wire format (same as Chapter 02)...
    return result
```

The comment says "same as Chapter 02" but Chapter 01-1 comes before Chapter 02. A student following the tutorial in order is told to implement something whose implementation hasn't been taught yet. This creates a dead end.

The chapter says it "can be skipped" — but if it's skipped, streaming never works. If it's followed in order, the `_to_openai_messages` method is incomplete with placeholder code.

---

### Bug 5 — `ToolExecutionContext` import inconsistency

**Where:** Chapter 02-1 (`mcp.py`)

```python
from agent.tools import BaseTool, ToolExecutionContext, ToolRegistry
```

But `ToolExecutionContext` is defined in `agent/models.py`, not `agent/tools.py`. The `tools.py` file imports `ToolExecutionContext` from `models.py` and probably re-exports it, but this import chain is never made explicit. A student building `mcp.py` fresh will get an `ImportError` and be confused about where `ToolExecutionContext` actually lives.

---

### Bug 6 — `latest.json` as a symlink breaks on Windows

**Where:** Chapter 03, `SessionStore`

```python
# Creating a symlink — works on Linux/Mac, fails silently or requires special privileges on Windows
latest_link = self.root / "latest.json"
if latest_link.exists() or latest_link.is_symlink():
    latest_link.unlink()
latest_link.symlink_to(filename)
```

The tutorial never mentions this is platform-specific. On Windows, creating symlinks requires Developer Mode or elevated privileges. A student on Windows gets permission errors with no guidance.

**Fix:** Use a `latest.txt` file that contains just the session ID, or simply copy the file. Or explicitly document Linux/Mac only.

---

### Bug 7 — Mode system and permission system interact without explicit specification

**Where:** Chapters 07 and 09

Chapter 07 creates `PermissionChecker` that returns `ALLOW/CONFIRM/DENY`.  
Chapter 09 creates `ExecutionMode` (PLAN/DEFAULT/AUTO) that should override permissions.

The tutorial never explicitly defines what happens when:
- Mode is AUTO but the permission policy says DENY for a specific path → which wins?
- Mode is PLAN but a read-only tool triggers a hook that modifies state → is that blocked?
- A worker (Ch10) runs in AUTO mode but is spawned from a coordinator in PLAN mode → does the worker inherit the mode?

The interaction is left as "the reader figures it out," which is a conceptual gap in a tutorial that otherwise prides itself on explicit design.

---

## Part 2 — Pedagogical Issues

These are not bugs — the code (mostly) works. These are places where the tutorial makes learning harder than it needs to be.

---

### Issue 1 — The async cliff between Chapter 00 and Chapter 01

Chapter 00 is beautifully synchronous. Every line of code makes sense to a Python beginner:
```python
while True:
    user_input = input("you> ")
    response = fake_model(user_input)
    print(f"agent> {response}")
```

Chapter 01 immediately introduces:
- `async def` / `await`
- `async for event in agent.run(...):`
- `AsyncGenerator[AgentEvent, None]` as a return type
- Abstract base classes with `@abstractmethod`
- `asyncio.run(main())`

There is no bridge. No "here is why we need async — this is what blocking I/O looks like and why it's a problem." No "here is the minimum asyncio you need." No "here is `async def` vs `def` in one sentence."

Many readers who completed Chapter 00 successfully will hit Chapter 01 and stop. This is the largest single drop-off point in the series.

**Suggested fix:** Add a 1-page intro to Chapter 01 called "Why async?" that shows:
- A sync tool call that blocks for 2 seconds
- Why that blocks the entire REPL
- `asyncio.run()` in 5 lines
- `await` as "pause here until this is ready"

That's all. Don't teach all of asyncio. Teach the minimum needed to understand the next code block.

---

### Issue 2 — Tests are in Chapter 14, but the code they test runs from Chapter 01

The tutorial teaches a complete 15-chapter system first, then in Chapter 14 says "here's how to test it." By then, a reader has built 14 chapters of untested code. If they discover in Chapter 14 that their Chapter 02 implementation has a subtle bug, they have to debug backwards through many files.

Professional practice (TDD or at least "write tests alongside the code") is never modeled. The `FakeModelClient` pattern should be introduced in Chapter 01 alongside `DemoModelClient`. The first test file should appear in Chapter 02.

The tutorial implicitly teaches: "write all the code, then write tests." This is the opposite of what it should teach.

---

### Issue 3 — Chapter 09 (Modes) feels redundant with Chapter 07 (Permissions)

After reading Chapter 09, a student should understand why modes exist beyond permissions. The tutorial's explanation is:

> "Modes are not personality switches. They are concrete runtime contracts."

But the concrete contracts described (mutating tools blocked in PLAN, allowed in AUTO) are exactly what `PermissionPolicy` already provides. The `mode_permits()` function in `modes.py` duplicates logic that `PermissionChecker` could handle with a different policy.

The tutorial should be honest here: **modes are a higher-level UX concept** that sets the permission policy automatically, rather than requiring the user to manually configure `PermissionPolicy`. Frame it this way and the chapter becomes clear. Frame it as a parallel system and it appears redundant.

---

### Issue 4 — The `agent.py` file grows silently across 15 chapters

By Chapter 13, `Agent.run()` is responsible for:
- Message management
- Streaming model calls
- Hook execution (pre/post tool, stop, notification)
- Context building via `ContextBuilder`
- Permission checking via `PermissionChecker`
- Guardrail checking via `GuardrailChecker`
- Mode enforcement via mode helpers
- Confirmation flows via `confirm_action()`
- Tool execution with context
- Session auto-save

This is one of the most classic software anti-patterns: the God Class. The tutorial never acknowledges it or explains how to decompose it in a real project. For a tutorial about building a good harness, this is a missed opportunity to model good software design.

A real production codebase would have separate components orchestrated via a pipeline or chain-of-responsibility pattern.

---

### Issue 5 — No single "this is what the finished agent can do" demo

The series builds 15 chapters of infrastructure. Nowhere does it show:

> "Here is a single terminal session where the finished agent, connected to a real LLM, takes a real multi-step task from start to finish."

Every code example uses `DemoModelClient`, which returns scripted deterministic responses. The closest to a live demo is the "Exercise D — Connect a real LLM" exercises scattered across chapters.

A student finishing Chapter 15 should be able to point to a concrete example of the system doing real work. Without that, the tutorial feels like it builds a car one part at a time but never drives it.

---

### Issue 6 — Sub-chapters create an awkward reading path

The recommended reading order in `README.md` is:

```
00 → 01 → 01-1 → 02 → 02-1 → 02-2 → 03 → 03-1 → 04 → 05 → ...
```

Having both `01-1` and `02-1` and `02-2` and `03-1` creates a branching structure that the README linearizes post-hoc. A student reading the chapters by filename sort order gets a different sequence. The README's table is correct but easy to miss.

**Fix:** Rename chapters to eliminate the `X-Y` naming and use explicit linear names:

```
00, 01, 02, 03, 04 (streaming), 05, 06 (mcp), 07 (plugins), 08, 09 (compaction), ...
```

Or, embed the "streaming" content directly into Chapter 01 as a later section rather than a separate file.

---

### Issue 7 — The streaming REPL renderer uses a mutable global state flag

**Where:** Chapter 01-1

```python
# Actual code in the tutorial:
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

This is module-level mutable state for a function that tracks whether we've printed the "agent>" prefix. Problems:
- Not thread-safe (matters for async contexts)
- Requires manual `_reset_stream_state()` call — easy to forget, hard to notice when forgotten
- The function has a side effect hidden in its name (`_streaming_started()` reads like a query but writes state)

A tutorial teaching good design should not model global mutable state for something this trivial. Pass a `first_chunk: bool` variable through the render call instead.

---

## Part 3 — Gaps That Still Remain (Post-IMPROVEMENTS.md)

The previous `IMPROVEMENTS.md` was marked "all items implemented" — but some remain genuinely missing.

---

### Remaining Gap 1 — No Evaluation / Benchmarking Chapter

IMPROVEMENTS.md listed it as "Priority: Low-to-Medium" and described a full chapter (suggested Ch21). It was never implemented.

**Why it matters:** Without evaluation, you cannot know whether a change to the guardrails or prompt assembly improved or worsened the agent's performance. The `FakeModelClient` from Ch14 is essentially an eval harness waiting to be formalized. The chapter is 80% done — the tooling to evaluate already exists; it just needs a chapter framing it.

A minimal eval chapter would cover:
- What "agent quality" means (task completion rate, unnecessary tool calls, false guardrail blocks)
- How to write scripted eval scenarios using `FakeModelClient`
- How to track regressions in CI with `pytest`
- The difference between behavioral tests (did the task complete?) and unit tests (did the loop call the right function?)

---

### Remaining Gap 2 — No Rate Limiting or Retry on API Errors

The `OpenAIModelClient.complete()` method makes a raw API call:

```python
response = await self._client.chat.completions.create(...)
```

Real OpenAI API calls return HTTP 429 (Too Many Requests) regularly. The tutorial has zero guidance on:
- Exponential backoff
- `max_retries` configuration
- Distinguishing 429 (retry) from 400 (bad request - don't retry)
- Token budget exhaustion errors

A student building on this will hit rate limits in production and have no framework for handling them. At minimum, `OpenAIModelClient` should use `tenacity` or a simple `asyncio.sleep(2**attempt)` retry loop.

---

### Remaining Gap 3 — No BashTool for Local Execution

Chapter 07-1 covers `SandboxedBashTool` for Docker. But there is no unsandboxed `BashTool` for local/trusted execution. The most commonly needed agent capability ("run this shell command") is unimplemented outside the Docker path.

A reader who doesn't have Docker (or doesn't need the overhead) has no reference implementation for the most basic "execute arbitrary commands" tool. Writing it incorrectly (e.g., using `shell=True` without sanitization) is a significant security risk. The tutorial should provide a safe local `BashTool` with a clear security warning.

---

### Remaining Gap 4 — No Anthropic API Adapter

Only `OpenAIModelClient` is implemented. Anthropic Claude models are among the most widely used for agent tasks. The Anthropic API has different wire formats:

- Tool call format: `input` key (vs OpenAI's `arguments`)
- Response format: `content` is a list of blocks (text + tool_use), not a flat message
- Tool result format: uses separate `user` message with `tool_result` content type

Without an Anthropic adapter, users of Claude (arguably the most agent-capable model family) are left to implement everything themselves.

---

### Remaining Gap 5 — No Cost Tracking

The token estimator from Chapter 03-1 counts tokens for compaction. But nowhere is there:
- Per-session token cost tracking
- Cumulative cost display
- Budget enforcement ("stop if cost exceeds $5")
- Cost logging in `AuditTrail`

For a tutorial that covers safety and permissions in depth, letting an agent run unconstrained on billable API calls is a real oversight.

---

### Remaining Gap 6 — No "End-to-End With a Real LLM" Walkthrough

As noted in Issue 5 above — there is no chapter (or section) that walks the reader through:

```bash
export OPENAI_API_KEY=sk-...
python main.py
you> list the Python files in this directory and tell me what each one does
```

...and shows the actual expected agent behavior with a real model. Every example uses the `DemoModelClient` which returns synthetic canned responses. The gap between "tutorial complete" and "I have a running, useful agent" is not bridged.

---

### Remaining Gap 7 — `contextvars.ContextVar` SESSION_ID Correlation Not Wired Through

IMPROVEMENTS.md said "Added to Chapter 03" for session ID correlation via `contextvars`. Looking at Chapter 03 itself — the `SESSION_ID` contextvar is described but the tutorial never shows how the audit trail logger, hook executor, and memory store all *read* from it. The wiring is only half-shown.

---

### Remaining Gap 8 — Monitoring / Observability (Never Implemented)

IMPROVEMENTS.md labeled it "Priority: Low" and it was indeed never implemented. But for a 2026 tutorial series claiming to build a "production-capable" harness, having no observability guidance is a serious omission:

- No structured logging with correlation IDs
- No metrics (tool latency, error rates, session costs)
- No health check endpoint
- No guidance on how to run the agent as a long-running service

The `AuditTrail` in Chapter 13 is a start, but it's append-only JSONL with no reader/exporter.

---

## Part 4 — Things That Are Just Missing Conceptually

These are concepts that any production agent would need, but the tutorial doesn't address at all.

---

### Missing Concept 1 — What to do when the model produces malformed tool arguments

The loop assumes `ToolCall.input` is always valid JSON with the right field types. Real LLMs occasionally generate:
- Extra fields the schema doesn't declare
- Wrong types (string where int is expected)
- Missing required fields
- Truncated JSON if the model's context is near its limit

The validation in `BaseTool.execute()` catches missing fields but only at the tool level. The tutorial doesn't explain:
- Where to validate schema conformance before handing to the tool
- How to feed validation errors back to the model constructively
- Whether the `input_schema` JSON Schema should be enforced (it isn't, currently)

A `jsonschema.validate(tool_call.input, tool.input_schema)` call before dispatch would catch most issues and the tutorial should show this.

---

### Missing Concept 2 — Multi-turn context across the REPL loop

The `Agent` class accumulates `self.messages` across REPL turns. This means every message from every turn is in context forever (until compaction). But the user starting a "new topic" mid-session doesn't get a fresh context — the model still sees everything from 10 turns ago.

The tutorial never discusses:
- How to mark a "context boundary" (user switches topic)
- Whether to clear history on new topic
- How to handle the model being confused by stale context
- The `/clear` or `/new` REPL command

---

### Missing Concept 3 — Tool timeout enforcement

`BaseTool.execute()` is `await`ed with no timeout. A tool that makes a slow network call (a search tool, a web scraper) can block the agent loop indefinitely. Chapter 04 adds hook timeouts but not tool execution timeouts.

```python
# What the loop should do (never shown in the tutorial):
try:
    result = await asyncio.wait_for(
        tool.execute(tool_call.input, context),
        timeout=tool.timeout_seconds or 30.0
    )
except asyncio.TimeoutError:
    result = ToolResult(
        output=f"Tool '{tool_call.name}' timed out after {tool.timeout_seconds}s",
        is_error=True,
        is_recoverable=False,
    )
```

---

### Missing Concept 4 — Anthropic-style "thinking" / extended output

Several advanced LLM features (OpenAI o1/o3 reasoning tokens, Anthropic extended thinking) produce internal reasoning output separate from the visible response. The `ModelResponse` data model in Chapter 01 has only `text` and `tool_calls`. There is no field for:
- Reasoning/thinking tokens
- Refusal tokens
- Stop reason (why did the model stop?)

The `stop_reason` is particularly important — it tells the runtime whether the model stopped naturally, hit the token limit, or was stopped by content filtering. Without it, the runtime cannot distinguish "I'm done" from "I ran out of tokens mid-sentence."

---

### Missing Concept 5 — Composing agents hierarchically (agents calling agents)

Chapter 10 (Swarms) shows a coordinator spawning workers. But workers are spawned as tasks — the coordinator doesn't communicate with them as agents mid-task. This is "fan-out/fan-in" delegation, not true hierarchical composition.

True hierarchical composition (where a sub-agent is called like a tool and returns a structured result to the parent agent's conversation) is a different pattern that should be shown separately. It is arguably more useful than workers-as-tasks for most real workflows.

---

## Part 5 — What the Final Harness Looks Like vs What It Should Look Like

After following all 15 chapters, a student's `Agent.run()` method looks conceptually like:

```
run(user_text):
  append user message
  yield StatusEvent
  build context
  check hooks (user_prompt_submit)

  while True:
    build system prompt (ContextBuilder)
    stream model call
    emit AssistantTextDelta chunks
    assemble text, append to messages
    if no tool calls → check stop hook → return

    for each tool_call:
      check guardrails (GuardrailChecker)
      check permissions (PermissionChecker)
      maybe check confirmation (confirm_action)
      yield ToolExecutionStarted
      check mode (mode_permits)           ← redundant with permissions?
      execute tool with context
      yield ToolExecutionCompleted
      append tool_result to messages
      check hooks (post_tool_use)
    
    loop back
```

This is 25+ distinct operations in one method. For a tutorial claiming to model good architecture, this method alone would fail a code review. The tutorial teaches great patterns for each individual component but never shows how to compose them cleanly.

A production harness would use a **pipeline** or **middleware chain** — each concern is a distinct step with a clean interface:

```python
async def run(self, user_text: str):
    ctx = AgentContext(user_text, self.messages, self.config)
    
    async for event in Pipeline(
        PromptSubmitHooks(),
        ContextBuilder(),
        ModelCaller(),
        StreamingAssembler(),
        ForEachToolCall(
            GuardrailChecker(),
            PermissionChecker(),
            ConfirmationFlow(),
            ToolExecutor(),
            PostToolHooks(),
        ),
        StopHooks(),
    ).run(ctx):
        yield event
```

This is a design pattern the tutorial never reaches.

---

## Part 6 — Chapter-by-Chapter Quick Reference

| Chapter | Quality | Key Issue |
|---|---|---|
| 00 — Agent Basics | ✅ Excellent | None — this is the best chapter |
| 01 — Agent Loop | ✅ Good | Async cliff; type annotation in run() confusing |
| 01-1 — Streaming | ⚠️ Fair | Forward reference to Ch02; global state in renderer |
| 02 — Tools | ✅ Good | `frozen=True` bug; `__import__` antipattern; `is_recoverable` inconsistency in checklist |
| 02-1 — MCP | ✅ Good | Custom client won't work with official MCP SDK out of the box |
| 02-2 — Plugins | ✅ Good | No plugin failure handling |
| 03 — Sessions | ✅ Good | Symlink is Windows-incompatible; SESSION_ID wiring incomplete |
| 03-1 — Compaction | ✅ Good | Sliding window loses tool history; no tiktoken mention |
| 04 — Hooks | ✅ Good | Hook timeout added; but no way to inspect hook registry |
| 05 — Context Eng. | ✅ Good | User query injected into system prompt duplicates the messages array |
| 06 — Memory | ✅ Good | TTL added; but keyword retrieval is fragile |
| 07 — Permissions | ✅ Good | Mode/permission interaction never specified |
| 07-1 — Docker | ⚠️ Fair | No resource limits; no Windows Docker path guidance |
| 08 — Skills | ✅ Good | Skills are static; no dynamic discovery |
| 09 — Plan/Auto Mode | ⚠️ Fair | Conceptually redundant with permissions; interaction undefined |
| 10 — Swarms | ⚠️ Fair | No actual parallel execution shown; workers share coordinator's tool registry |
| 11 — Mailbox | ✅ Good | FileMailbox added; worker mailbox integration with loop not shown |
| 12 — Confirmation | ✅ Good | Solid; approval vs clarification distinction is correct |
| 13 — Guardrails | ✅ Good | Injection scanning is regex-based and easily bypassed |
| 13-1 — Config | ✅ Good | `tomllib` dependency (3.11+) not mentioned |
| 14 — Testing | ✅ Good | Tests come too late; should start in Ch02 |
| 15 — Adv. Context | ✅ Good | KnowledgeUpdater LLM call costs not discussed; profile grows forever |

---

## Part 7 — Specific Fixes Worth Implementing Now

These are the highest-value improvements that don't require new chapters:

### Fix 1 — Add max-iterations guard to the agent loop (5 lines)

In every version of `agent.py`:

```python
MAX_AGENT_ITERATIONS = 50  # configurable via agent.toml

async def run(self, user_text: str):
    self.messages.append(Message.user(user_text))
    _iterations = 0

    while True:
        _iterations += 1
        if _iterations > MAX_AGENT_ITERATIONS:
            yield ErrorEvent(
                message=f"Loop limit reached ({MAX_AGENT_ITERATIONS} iterations). Stopping.",
                details="The model may be stuck in a tool-call loop."
            )
            return
        # ... rest unchanged
```

This prevents runaway API spend and is trivial to add.

---

### Fix 2 — Add a "`asyncio` in 5 minutes" primer to Chapter 01

Before all the dataclass code, add a collapsible/sectioned "Why async?" section:

```python
# The problem with sync I/O in an agent:
import time

def slow_tool():
    time.sleep(2)      # blocks EVERYTHING for 2 seconds
    return "result"

# With async I/O:
import asyncio

async def slow_tool():
    await asyncio.sleep(2)   # "pause here but let other code run"
    return "result"

# The key patterns you'll see:
# async def    → defines a coroutine function (must be awaited to run)
# await        → "run this and wait for it to finish"
# async for    → iterate over an async generator
# asyncio.run  → run the top-level async entry point
```

This removes the cliff.

---

### Fix 3 — Show the actual file structure after every major chapter

Every chapter shows what the **new** files are but not the full updated state of existing files. After chapter 09, what does `agent.py` actually look like in its entirety? After chapter 13, what does `main.py` look like?

This is the most common source of confusion for readers: files accumulate modifications across chapters with no "this is the complete file right now" reference. Even one complete file snapshot per chapter (in a `# COMPLETE FILE` section at the end) would dramatically improve usability.

---

### Fix 4 — Add `jsonschema` validation before tool dispatch

In `agent.py`, before calling `tool.execute()`:

```python
# Optionally: pip install jsonschema
try:
    import jsonschema
    jsonschema.validate(tool_call.input, tool.input_schema)
except ImportError:
    pass  # Validation is optional; proceed without it
except jsonschema.ValidationError as exc:
    result_text = (
        f"Invalid arguments for tool '{tool_call.name}': {exc.message}\n"
        f"Schema expects: {tool.input_schema}"
    )
    is_error = True
    # ... yield error event, append to messages and continue
```

This teaches the reader that JSON Schema is active, not decorative.

---

### Fix 5 — Remove the `_saw_first_chunk` global state

Replace with a simple counter passed through the render function:

```python
async def repl(agent: Agent) -> None:
    while True:
        user_input = input("you> ").strip()
        # ...
        first_chunk_seen = False

        async for event in agent.run(user_input):
            first_chunk_seen = await render(event, first_chunk_seen)

async def render(event: object, first_chunk_seen: bool) -> bool:
    if isinstance(event, AssistantTextDelta):
        if not event.is_final:
            if not first_chunk_seen:
                sys.stdout.write("\nagent> ")
            sys.stdout.write(event.text)
            sys.stdout.flush()
            return True   # first_chunk_seen is now True
        else:
            print()
    # ... other events
    return first_chunk_seen
```

No globals. No manual reset. Stateless render function.

---

## Part 8 — Final Honest Summary

The Minimal Agent Harness tutorial series is one of the better "build it from scratch" agent tutorials available. Its strengths:

- It builds real patterns, not toy examples
- It does not paper over hard decisions (permission overlap, frozen vs mutable data, sync vs async I/O)
- The progression from literal basics to production concerns is honestly attempted
- The `FakeModelClient` / event-driven testing approach is genuinely good

Its honest weaknesses:

- The async transition is not handled — beginners will stop at Chapter 01
- Several code samples contain real bugs (`frozen=True` with `dict`, missing loop guard, global state in renderer)
- `agent.py` becomes a God Class by Chapter 13 and no one says so
- The system is never demonstrated end-to-end with a real LLM doing real work
- Testing comes last when it should be woven throughout
- The mode/permission interaction is never fully specified
- Several production concerns (rate limiting, cost tracking, tool timeouts, Anthropic support) are missing

A student who completes all 15 chapters will understand agent harness design better than most people who build with frameworks. That is the genuine value here. But they will also have a codebase with real bugs and structural problems that they may not know to look for.

---

*Audit complete. Approximately 23 chapters reviewed, ~10,000 lines of tutorial content.*

