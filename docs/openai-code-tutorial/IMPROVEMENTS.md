# Improvements and Missing Chapters — Review of the Tutorial Series

> **Status:** All items in this document have been implemented.
> New sub-chapters and inline updates have been applied.
> See [README.md](README.md) for the full reading order.

---

## Implementation summary

| Item | Status | Where |
|---|---|---|
| Testing the harness | ✅ Done | `14-testing-the-harness.md` |
| Streaming responses | ✅ Done | `01-1-streaming.md` |
| Context compaction | ✅ Done | `03-1-context-compaction.md` |
| Configuration system | ✅ Done | `13-1-configuration.md` |
| MCP integration | ✅ Done | `02-1-mcp-integration.md` |
| Plugin system | ✅ Done | `02-2-plugins.md` |
| Docker sandboxing | ✅ Done | `07-1-docker-sandboxing.md` |
| Session schema versioning | ✅ Done | Added to `03-session-manager.md` |
| Session ID correlation via contextvars | ✅ Done | Added to `03-session-manager.md` |
| Hook async timeout | ✅ Done | Added to `04-hooks.md` |
| Memory expiry/decay + `updated_at` | ✅ Done | Added to `06-memory-and-storage.md` |
| Per-session ephemeral permission grants | ✅ Done | Added to `07-permissions.md` |
| Tool error message quality | ✅ Done | Added to `02-tools.md` |
| `is_recoverable` on `ToolResult` | ✅ Done | Added to `02-tools.md` |
| Tool write idempotency | ✅ Done | Added to `02-tools.md` |
| Mode change events (`ModeChangedEvent`) | ✅ Done | Added to `09-plan-mode-and-auto-mode.md` |
| Worker result routing via `task_id` | ✅ Done | Added to `10-swarms-and-delegation.md` |
| Durable `FileMailbox` | ✅ Done | Added to `11-agent-communication.md` |
| `__slots__` rationale | ✅ Done | Added to `01-agent-loop.md` |
| Type annotations house rule | ✅ Done | Added to `01-agent-loop.md` |

---



## Part 1 — Critical Gaps (must-have additions)

### GAP 1: No testing chapter

**The biggest missing piece.** The entire series teaches how to build the harness but never explains how to test it. A harness without tests is fragile — every refactor is risky, every guardrail is unverified.

A dedicated chapter (suggested: `14-testing-the-harness.md`) should cover:

```
What to test:
  • Tools in isolation      → unit-test each tool's execute() with fake context
  • The agent loop          → fake model client that returns scripted responses
  • Session save/restore    → round-trip snapshot through JSON
  • Hook execution          → verify hooks fire, blocks work, outputs are collected
  • Permission policies     → table-driven tests for every allow/deny/confirm case
  • Guardrails              → property-based tests for path resolution edge cases
  • Memory retrieval        → keyword matching correctness

Key testing patterns:
  • FakeModelClient(script=[...]) — deterministic model for loop tests
  • RecordingHook            — captures events for assertion
  • tmp_path fixture         — isolate session/memory files per test
  • pytest-asyncio           — all agent code is async
```

**Why it matters:** Chapters 07 and 13 say "test every guardrail" but provide no framework for doing so.

---

### GAP 2: No streaming output chapter

Right now `ModelResponse.text` arrives as one complete string. Real LLM APIs stream tokens one at a time — users see the response build character by character instead of waiting for the full answer.

The current design's `AssistantTextDelta` event is named for streaming but the implementation returns a single string. A chapter (suggested: `15-streaming.md`) should cover:

```python
# What needs to change:

# Current: model returns one complete string
response = await model_client.complete(...)    # blocks until done
yield AssistantTextDelta(text=response.text)    # one event

# Streaming: model yields chunks as they arrive
async for chunk in model_client.stream(...):
    yield AssistantTextDelta(text=chunk)        # many events, each a few tokens
    # The REPL prints without newline, giving live typewriter effect
```

This affects `DemoModelClient`, `OpenAIModelClient`, and the REPL renderer. Critical for production UX.

---

### GAP 3: No context window management / compaction

Every chapter adds more to `self.messages`. In a long session with many tool calls, the message list grows without bound and will eventually exceed the model's context window — causing API errors or degraded quality.

A chapter (suggested: `16-context-compaction.md`) should cover:

```
Problem:    messages grow → hit token limit → API error or quality drop

Solutions:
  1. Token counting       — estimate token count before each API call
  2. Sliding window       — keep last N messages, drop oldest user/assistant pairs
  3. Summarization        — ask the model to summarize old turns into one message
  4. Tool result pruning  — replace old tool_result messages with digest summaries
  5. Carry-over state     — use snapshot.carry_over to preserve key facts, not raw messages

When to compact:
  • Estimated tokens > 80% of model's context window
  • Or: configurable turn count threshold
```

OpenHarness already has `services/token_estimation.py` and a `compact` service — the tutorial has no equivalent.

---

### GAP 4: No configuration system

Every `build_agent()` call in `main.py` has all settings hardcoded as function parameters. A real project needs:

- A YAML/TOML config file for model name, paths, permission policy, mode defaults
- Environment variable overrides (for CI/CD, Docker deployments)
- Per-project config files (`.agentrc` or `agent.toml` in the project root)

A chapter (suggested: `17-configuration.md`) should cover:

```python
# What a config file should look like:
# agent.toml

[model]
provider = "openai"
name = "gpt-4o"
max_tokens = 8192

[permissions]
write_allowed_root = "."
deny_tools = ["bash"]

[memory]
root = ".agent-memory"

[sessions]
root = "sessions"
auto_save = true

[mode]
default = "default"
```

OpenHarness has `config/schema.py` and `config/settings.py` — showing this is real-world necessary.

---

### GAP 5: No MCP (Model Context Protocol) chapter

MCP is the emerging standard for tool/resource discovery between agents and external services. It is already implemented in the reference repository (`src/openharness/mcp/`). Not covering it in a 2026 tutorial is a significant omission.

A chapter (suggested: `18-mcp-integration.md`) should cover:

```
What MCP does:
  • Standard protocol for exposing tools from external servers
  • Agent connects to an MCP server → discovers tools → calls them
  • Same tool interface your harness already uses, but the tools live in a subprocess

Why it matters:
  • Build a tool once → use from any MCP-compatible agent
  • Third-party tools without code changes: filesystem server, git server, browser server
  • The bridge between your BaseTool registry and the ecosystem

Minimal integration:
  • MCPClient connects to a server and lists its tools
  • MCPToolAdapter wraps each MCP tool as a BaseTool subclass
  • Register adapters into the ToolRegistry as normal
```

---

### GAP 6: No plugin system chapter

The tutorial's `ToolRegistry` requires all tools to be registered in `build_agent()`. There is no way to add tools from a third-party package. OpenHarness has a full plugin system (`plugins/loader.py`, `plugins/installer.py`). The tutorial should explain how to build one.

A chapter (suggested: `19-plugins.md`) should cover:

```
Plugin contract:
  • A plugin is a Python package that exposes a register(registry) function
  • plugins can add tools, hooks, skills, or memory sources
  • Discovery via entry points (pyproject.toml [project.scripts]) or a plugins/ directory

Security concerns:
  • Plugins run arbitrary code — verify or sandbox before installing
  • Plugin-provided tools go through the same guardrail/permission stack
  • Separate allow-list for pre-approved plugins vs unknown plugins

Installation:
  pip install my-agent-plugin
  # or: agent install ./my-plugin/

# plugin's __init__.py minimal contract:
def register(registry: ToolRegistry, hooks: HookExecutor) -> None:
    registry.register(MyCustomTool())
    hooks.register(MyCustomHook())
```

---

## Part 2 — Depth Improvements to Existing Chapters

### Chapter 02 (Tools) — Missing: error schema and retry

The `ToolResult.is_error` field exists but there is no guidance on:
- What the model should do after a tool error (the current loop just feeds the error text back, but the model may retry indefinitely)
- Adding a `retry_count` to `ToolExecutionContext` and a max-retry guard in the loop
- Distinguishing recoverable errors (file not found) from unrecoverable ones (permission denied)

**Suggested addition:**
```python
# In agent/tools.py
@dataclass(slots=True, frozen=True)
class ToolResult:
    output: str
    is_error: bool = False
    is_recoverable: bool = True   # ← new: hint to the loop whether to retry
    metadata: dict = field(default_factory=dict)
```

---

### Chapter 03 (Session) — Missing: schema versioning

The `SessionSnapshot.to_dict()` writes JSON but there is no `schema_version` field. When you add `mode` in Chapter 09, old session files break silently (`mode` defaults to `"default"` which may be wrong for restored AUTO sessions).

**Suggested addition:**
```python
# In to_dict():
"schema_version": 3,     # increment when snapshot shape changes

# In from_dict():
version = data.get("schema_version", 1)
if version < 3:
    data = migrate_snapshot(data, from_version=version)
```

A small migration function ensures old sessions load correctly after code changes.

---

### Chapter 04 (Hooks) — Missing: async hook timeout

A hook that makes a slow network call can stall the entire agent turn. The `HookExecutor` should wrap each hook in `asyncio.wait_for()`.

**Suggested addition:**
```python
# In HookExecutor.execute():
try:
    result = await asyncio.wait_for(
        hook.run(payload),
        timeout=5.0,   # configurable per-event
    )
except asyncio.TimeoutError:
    aggregated.outputs.append(f"[hook timeout] {hook.__class__.__name__} exceeded 5s")
    continue
```

---

### Chapter 06 (Memory) — Missing: memory expiry and decay

The `MemoryStore` grows forever. Facts become stale. The tutorial should explain:

- `updated_at` field on `MemoryEntry`
- TTL-based pruning: entries older than N days that haven't been retrieved are candidates for deletion
- LRU-based pruning: if memory exceeds a size budget, least-recently-retrieved entries are removed
- The agent can call `DeleteMemoryTool` to clean up explicitly

---

### Chapter 07 (Permissions) — Missing: per-session permission grants

Right now permissions are global (policy applies to every session). But users often want to grant write permission *for this session only*:

```
you> for this session, auto-approve write_file — I trust you
```

**Missing concept:** ephemeral session-scoped grants that live in `SessionSnapshot.carry_over` and expire when the session ends. This is distinct from permanently whitelisting a tool in the policy.

---

### Chapter 09 (Modes) — Missing: mode transitions as events

When the mode changes (via `/mode auto`), no hook fires and nothing is logged. A `ModeChangedEvent` should be added:

```python
@dataclass(frozen=True)
class ModeChangedEvent:
    old_mode: str
    new_mode: str
    changed_by: str   # "user" | "config" | "restore"
```

This makes mode transitions observable through the existing hook/event system instead of being side effects noticed only in the prompt.

---

### Chapter 10 (Swarms) — Missing: worker result routing

When multiple workers run in parallel, all their results land in the same coordinator. The tutorial has no pattern for routing results to the right part of the coordinator's workflow. 

**Missing concept:** result correlation — the coordinator should include a `task_id` in the spawn request and match returned results to the right pending step.

---

### Chapter 11 (Mailbox) — Missing: durable mailbox

`InMemoryMailbox` loses all messages on restart. If the coordinator crashes while a worker is mid-task, the worker's result is lost. The tutorial notes this but provides no solution.

**Missing concept:** `FileMailbox` — each message is a JSON file in `mailbox/{recipient}/`. On startup, the agent reads its pending mailbox files. This is the simplest durable alternative and requires only 20 lines.

---

## Part 3 — Suggested New Chapters

### Suggested Chapter 14: Testing the Harness

**Priority: Critical**

Cover: `FakeModelClient`, `RecordingHook`, tool unit tests, loop integration tests, session round-trip tests, permission table tests, guardrail property tests. Provide a `tests/` scaffold readers can copy directly.

---

### Suggested Chapter 15: Streaming Responses

**Priority: High**

Cover: streaming vs non-streaming model clients, how `AssistantTextDelta` becomes a true stream of small chunks, updating the REPL renderer to print without newlines, back-pressure, and how to handle a stream that errors mid-way.

---

### Suggested Chapter 16: Context Compaction

**Priority: High**

Cover: token estimation, the sliding window strategy, the summarization strategy, when to compact (turn count threshold vs token budget), how compaction interacts with session save/restore, and the carry-over state as a compaction aid.

---

### Suggested Chapter 17: Configuration System

**Priority: Medium**

Cover: `agent.toml` / `.agentrc`, `pydantic-settings` or `dynaconf` for schema validation, environment variable overrides, per-project config discovery (walk up directory tree), sensible defaults for everything, secret handling (model API key from env, not config file).

---

### Suggested Chapter 18: MCP Integration

**Priority: Medium**

Cover: what the Model Context Protocol is, connecting to an MCP server as a subprocess, listing tools via the MCP wire format, wrapping them as `BaseTool` instances, the `MCPToolAdapter`, connecting to `mcp-server-filesystem` or `mcp-server-git` as real examples.

---

### Suggested Chapter 19: Plugins and Extensions

**Priority: Medium**

Cover: the plugin entry-point contract, discovery by directory scan vs pip entry points, a `PluginLoader` class, the security surface (plugins can register tools that run arbitrary code), a minimal example plugin package that adds a custom tool and hook.

---

### Suggested Chapter 20: Docker Sandboxing

**Priority: Medium** (addresses a real production need)

Cover: why path restrictions alone are not enough (a tool could `import subprocess` and exec anything), running tools inside a Docker container, the `DockerSandbox` class (build image, exec command, capture stdout/stderr, enforce timeout, clean up), bind-mounting the workspace read-only, mapping results back out. OpenHarness has `sandbox/docker_backend.py` as a reference.

---

### Suggested Chapter 21: Evaluation and Benchmarking

**Priority: Low-to-Medium**

Cover: what "quality" means for an agent (task completion rate, tool call efficiency, unnecessary confirmation rate, false positive guardrail blocks), creating scripted eval scenarios with `FakeModelClient`, running evals in CI, tracking regressions across refactors. This is the "unit testing for agent behaviour" chapter.

---

### Suggested Chapter 22: Monitoring and Observability in Production

**Priority: Low** (for later)

Cover: structured logging (JSON log lines with correlation IDs), Prometheus-style metrics (tool latency histograms, error rates, session durations), distributed tracing across coordinator + workers, cost tracking (tokens in/out per session), health-check endpoint if the agent runs as a service.

---

## Part 4 — Cross-Cutting Concerns (weave into existing chapters)

These are not new chapters — they should be added as sections or callout boxes within existing chapters.

### 1. Error message quality to the model

Every chapter that returns a `ToolResult(is_error=True)` shows a terse error string. The model reasons from these. Better error messages = better model recovery.

**Rule to add in Chapter 02:** Error messages should say:
1. What went wrong
2. Why it went wrong
3. What the model should try instead

```python
# BAD
ToolResult(output="File not found.", is_error=True)

# GOOD
ToolResult(
    output=(
        "File not found: 'src/auth.py'\n"
        "Tip: Use the glob tool to list available files in src/ first, "
        "then read the correct filename."
    ),
    is_error=True,
)
```

---

### 2. Idempotency of tool writes

Chapter 02 introduces `WriteFileTool` but never mentions idempotency. If the model calls `write_file` twice with the same content (a common retry pattern), it should not create duplicates or corrupt state.

**Pattern to add in Chapter 02:** tools that mutate should be idempotent where possible — writing the same content twice should produce the same end state as writing once.

---

### 3. Structured result metadata usage

`ToolResult.metadata` is defined in Chapter 02 but never used for anything meaningful until Chapter 05 where `last_read_file` is extracted from it. Chapters 02–04 should explicitly show *why* metadata exists and give a concrete example of using it.

---

### 4. Type annotations everywhere

Several chapters show code without full type annotations on function signatures. In an async, multi-component system, missing types cause subtle bugs. Add a note in Chapter 01:

> **House rule:** annotate all function parameters and return types using Python 3.10+ syntax (`int | None` instead of `Optional[int]`). This applies to all code in this series.

---

### 5. `__slots__` rationale

Chapters 01 and later use `@dataclass(slots=True)` on performance-critical types (`Message`, `ToolCall`, etc.) but never explain why. Add a one-paragraph explanation in Chapter 01:

> `slots=True` prevents Python from creating a `__dict__` per instance, reducing memory by ~30% for types that are created thousands of times per session. Use it on all dataclasses that live in message arrays or event streams.

---

### 6. Session ID as a correlation handle

From Chapter 03 onward, every log line, audit entry, and hook output should include the session ID. Right now each system logs in isolation — you cannot correlate a hook output with the session that produced it.

**Add to Chapter 03:** The first thing `repl()` does after building the agent should be to set a `contextvars.ContextVar` with the session ID. All logging downstream reads this automatically.

```python
import contextvars
SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="none")

# In repl(), after building/restoring:
SESSION_ID.set(agent._snapshot.session_id)
```

---

## Part 5 — Summary Priority Table

| # | Topic | Priority | Suggested Chapter |
|---|---|---|---|
| 1 | Testing the harness | 🔴 Critical | 14 |
| 2 | Streaming responses | 🔴 Critical | 15 |
| 3 | Context compaction / token budgets | 🔴 Critical | 16 |
| 4 | Configuration system (TOML/env) | 🟡 High | 17 |
| 5 | MCP integration | 🟡 High | 18 |
| 6 | Plugin system | 🟡 High | 19 |
| 7 | Docker sandboxing | 🟡 High | 20 |
| 8 | Session schema versioning | 🟡 High | Improve Ch03 |
| 9 | Hook async timeout | 🟡 High | Improve Ch04 |
| 10 | Memory expiry/decay | 🟠 Medium | Improve Ch06 |
| 11 | Per-session permission grants | 🟠 Medium | Improve Ch07 |
| 12 | Tool error message quality | 🟠 Medium | Improve Ch02 |
| 13 | Mode change events | 🟠 Medium | Improve Ch09 |
| 14 | Durable file mailbox | 🟠 Medium | Improve Ch11 |
| 15 | Evaluation / benchmarking | 🟠 Medium | 21 |
| 16 | Worker result routing | 🟠 Medium | Improve Ch10 |
| 17 | Monitoring / metrics in prod | 🔵 Low | 22 |
| 18 | Idempotency of write tools | 🔵 Low | Improve Ch02 |
| 19 | Correlation ID in logs | 🔵 Low | Improve Ch03 |
| 20 | `__slots__` rationale | 🔵 Low | Improve Ch01 |

---

## Part 6 — Recommended Next Steps

If you want to continue the series, the highest-value additions in order are:

**Round 1 — Foundation completeness:**
1. `14-testing-the-harness.md` — without this, nothing else can be verified
2. `16-context-compaction.md` — without this, long sessions break
3. Improve `03-session-manager.md` with schema versioning

**Round 2 — Production readiness:**
4. `15-streaming.md` — UX critical for real deployment
5. `17-configuration.md` — required before any real project uses this
6. Improve `04-hooks.md` with async timeout

**Round 3 — Ecosystem integration:**
7. `18-mcp-integration.md` — connects to the wider tool ecosystem
8. `19-plugins.md` — enables community extension
9. `20-docker-sandboxing.md` — real security for tool execution

**Round 4 — Observable and measurable:**
10. `21-evaluation.md`
11. `22-monitoring.md`

