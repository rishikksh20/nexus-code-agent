# ENGINEERING HANDBOOK

## 1. Repository Overview

### What this repo is
A Python-first coding-agent runtime that evolves from a minimal teaching loop in `basic/` into a session-aware, tool-using framework in `core/`.

- Minimal teaching loop: `basic/main.py:121`, `basic/src/providers.py:9`
- Full runtime entrypoint: `main.py:332`
- Main orchestrator: `core/agent/agent.py:14`
- Runtime ownership boundary: `core/agent/session.py:20`

### High-level architecture
```text
CLI/TUI
  -> Agent
    -> Session
      -> LLMClient
      -> ContextManager
      -> ToolRegistry
      -> MCPManager
      -> ApprovalManager
      -> HookSystem
      -> ChatCompactor
      -> LoopDetector
```

### Main engineering philosophy
1. **Keep the agent loop small; push complexity into subsystems.** The loop in `core/agent/agent.py:37` is still: build messages -> stream model -> collect tool calls -> execute tools -> feed results back -> repeat.
2. **Normalize all external variability behind internal contracts.** Examples:
   - model streaming -> `StreamEvent` in `core/client/datatype.py:55`
   - tools -> `Tool` in `core/tools/base.py:98`
   - remote MCP tools -> `MCPTool` adapter in `core/tools/mcp/mcp_tool.py:8`
3. **Own runtime state inside `Session`, not `Agent`.** `Session` is the framework boundary for per-conversation state and lifecycle (`core/agent/session.py:20`).
4. **Add capabilities by widening the tool surface, not by special-casing the loop.** Builtins, discovered tools, subagents, and MCP tools all flow through `ToolRegistry` (`core/tools/registry.py:16`).

---

## 2. Repository Structure

```text
.
├── main.py                 # CLI bootstrap + interactive command surface
├── basic/                  # teaching implementation of the minimal agent loop
├── core/
│   ├── agent/              # orchestration, session, events, persistence
│   ├── client/             # LLM adapter + internal stream datatypes
│   ├── config/             # typed config + loader/bootstrap
│   ├── context/            # conversation state, compaction, loop detection
│   ├── hooks/              # lifecycle hook runner
│   ├── prompts/            # system / compression / loop-break prompts
│   ├── safety/             # approval policy and safety classification
│   ├── tools/              # tool base class, registry, discovery, MCP, builtins
│   ├── ui/                 # Rich terminal rendering
│   └── utils/              # paths, token helpers, errors
├── docs/                   # stepwise architecture evolution docs
└── tests/                  # current regression tests (persistence-focused)
```

### Folder responsibilities
- `basic/`: the irreducible architecture. Read this first to understand the repo’s DNA.
- `core/`: production-shaped decomposition of the same loop.
- `docs/`: design-history narrative; code is still the source of truth.
- `tests/`: currently narrow, but useful to understand persistence guarantees (`tests/test_persistence.py:13`).

---

## 3. Core Runtime Flow

### Startup flow
```text
main()
  -> _load_env_file()
  -> load_config()
  -> Config.validate()
  -> CLI(config)
  -> Agent(config)
  -> Session.initialize()
```

Code path:
- `main.py:340` bootstraps config and mode selection
- `core/config/loader.py:76` merges system config, project config, and `AGENT.MD`
- `core/agent/session.py:44` initializes MCP, discovery, and context

### Why `Session.initialize()` order matters
`Session.initialize()` does:
1. MCP connect
2. register MCP tools
3. discover local custom tools
4. build `ContextManager` with the final tool list

Reference: `core/agent/session.py:44-49`

This is an important architectural choice: **the system prompt is built after the tool surface is complete**, so prompt construction sees builtin + discovered + MCP tools.

### Request lifecycle
```text
user message
  -> CLI._process_message()
  -> Agent.run()
  -> ContextManager.add_user_message()
  -> Agent._agentic_loop()
  -> LLMClient.chat_completion()
  -> streamed text / tool calls
  -> ToolRegistry.invoke()
  -> ContextManager.add_tool_result()
  -> repeat until no tool calls
  -> final text emitted to TUI
```

Key paths:
- `main.py:76` consumes `AgentEvent`s and routes them to the TUI
- `core/agent/agent.py:22` starts a run
- `core/agent/agent.py:62` calls the model
- `core/agent/agent.py:124` invokes tools

### Tool execution flow
```text
model tool call
  -> ToolRegistry.get(name)
  -> validate params
  -> before_tool hooks
  -> approval check
  -> tool.execute()
  -> normalize ToolResult
  -> after_tool hooks
  -> append tool result to context
```

Reference: `core/tools/registry.py:73`

This is the repo’s real execution middleware. Policy, validation, hooks, and failure normalization live here instead of in the agent loop or individual tools.

### Response generation flow
- `LLMClient` converts OpenAI-compatible chunks into internal `StreamEvent`s (`core/client/llm_client.py:45`, `core/client/datatype.py:17`)
- `Agent` converts those into UI-facing `AgentEvent`s (`core/agent/events.py:27`)
- `TUI` renders by event type and tool kind (`core/ui/tui.py:59`)

The result is a clean two-stage event pipeline:
```text
provider stream -> internal stream events -> agent events -> terminal UI
```

---

## 4. Core Abstractions

### `CLI` — interactive shell + command router
- Path: `main.py:31`
- Purpose: owns the interactive UX, slash commands, and event-to-TUI mapping.
- Why it exists: keeps terminal behavior out of the agent core.
- Pattern: thin controller.

### `Agent` — orchestration engine
- Path: `core/agent/agent.py:14`
- Purpose: drives the agentic loop, emits events, coordinates compaction, looping checks, and tool execution.
- Interacts with: `Session`, `LLMClient`, `ToolRegistry`, `ContextManager`.
- Why abstraction exists: behavior orchestration stays separate from state ownership.
- Pattern: orchestrator / application service.

### `Session` — runtime state boundary
- Path: `core/agent/session.py:20`
- Purpose: owns everything that should live across turns: client, registry, MCP manager, context, approval, hooks, compaction, loop detection, ids/timestamps.
- Why abstraction exists: this repo treats a conversation as a durable runtime object, not just a list of messages.
- Pattern: session object / composition root.

### `LLMClient` + stream datatypes — model adapter layer
- Paths: `core/client/llm_client.py:12`, `core/client/datatype.py:9`
- Purpose: translate OpenAI-compatible API behavior into the repo’s internal stream protocol.
- Why abstraction exists: the rest of the runtime should not depend on provider chunk shapes.
- Pattern: adapter.

### `ContextManager` — model-facing conversation state
- Path: `core/context/manager.py:33`
- Purpose: stores internal `MessageItem`s, rebuilds model messages, tracks token usage, prunes tool payloads, and restores from compaction summaries.
- Why abstraction exists: conversation state is more than a raw Python list.
- Pattern: state manager.

### `ChatCompactor` — overflow survival mechanism
- Path: `core/context/compaction.py:8`
- Purpose: summarize long history into a structured continuation prompt.
- Why abstraction exists: compaction is a separate prompt+LLM workflow, not just message trimming.
- Pattern: strategy-like helper around `LLMClient`.

### `LoopDetector` — behavioral guardrail
- Path: `core/context/loop_detector.py:5`
- Purpose: record action signatures and detect exact repeats / short cycles.
- Why abstraction exists: loop detection is orthogonal to tool logic and context storage.
- Pattern: lightweight monitor.

### `Tool` — universal tool contract
- Path: `core/tools/base.py:98`
- Purpose: define a common interface for builtin tools, discovered tools, subagents, and MCP tools.
- Key surface:
  - `schema`
  - `validate_params()`
  - `execute()`
  - `get_confirmation()`
  - `to_openai_schema()`
- Why abstraction exists: every tool source must look identical to the runtime.
- Pattern: interface / template boundary.

### `ToolRegistry` — control plane for tools
- Path: `core/tools/registry.py:16`
- Purpose: register, filter, schema-export, and invoke tools.
- Why abstraction exists: tool selection and execution policy need one central checkpoint.
- Pattern: registry + middleware pipeline.

### `ToolDiscoveryManager` — local plugin loader
- Path: `core/tools/discovery.py:12`
- Purpose: import Python modules from `.ai-agent/tools`, find `Tool` subclasses, instantiate, register.
- Why abstraction exists: plugins extend capability without touching core source.
- Pattern: convention-based plugin loader.

### `ApprovalManager` — safety policy engine
- Path: `core/safety/approval.py:92`
- Purpose: classify operations as approved / rejected / requires confirmation based on policy, command safety, and path scope.
- Why abstraction exists: approval logic belongs to policy, not to individual tools.
- Pattern: strategy by enum + policy object.

### `HookSystem` — lifecycle extension runner
- Path: `core/hooks/hook_system.py:12`
- Purpose: run configured commands/scripts before/after agent and tool events.
- Why abstraction exists: extensibility and observability without modifying core control flow.
- Pattern: event hook / integration layer.

### MCP layer — remote tool integration
- Paths:
  - `core/tools/mcp/client.py:27` — one remote connection
  - `core/tools/mcp/mcp_manager.py:9` — session-scoped MCP lifecycle
  - `core/tools/mcp/mcp_tool.py:8` — wraps remote tools as local `Tool`
- Why abstraction exists: external tools should reuse the same registry/runtime path as local tools.
- Pattern: adapter + manager.

### Persistence layer
- Path: `core/agent/persistence.py:13`
- Purpose: serialize a `SessionSnapshot`, write atomically, list/load sessions and checkpoints.
- Why abstraction exists: persistence should reconstruct a session by replay, not by pickling live managers.
- Pattern: snapshot + repository-style file manager.

### `TUI` — rendering, not orchestration
- Path: `core/ui/tui.py:59`
- Purpose: render assistant output, tool panels, diffs, shell output, and approval prompts.
- Why abstraction exists: presentation is isolated from runtime logic.
- Pattern: presenter/view.

### `basic/` abstractions worth copying
- `Provider` ABC in `basic/src/providers.py:9`
- event reducer in `basic/src/events.py:14`
- system prompt built from live tool functions in `basic/src/prompt.py:44`

`basic/` is the shortest proof that the framework is fundamentally an **evented model-tool loop**, not a large class hierarchy.

---

## 5. Design Patterns & OOP Usage

| Pattern | Where it exists | Why it is used |
| --- | --- | --- |
| Registry | `core/tools/registry.py:16` | One lookup/invocation surface for builtin, plugin, subagent, and MCP tools |
| Adapter | `core/client/llm_client.py:12`, `core/tools/mcp/mcp_tool.py:8`, `basic/src/providers.py:128` | Hide provider/protocol-specific payloads behind internal contracts |
| Factory / Composition root | `core/tools/registry.py:141`, `main.py:340`, `basic/main.py:99` | Build configured runtime graphs in one place |
| Strategy | `core/safety/approval.py:92`, `basic/src/providers.py:32` and `:128` | Swap behavior by policy/provider without changing callers |
| Plugin | `core/tools/discovery.py:45` | Load tools from convention-based directories |
| Middleware pipeline | `core/tools/registry.py:73` | Validation -> hooks -> approval -> execute -> hooks |
| Observer / event stream | `core/agent/events.py:27`, `basic/src/events.py:14`, `main.py:83` | Decouple model execution from UI rendering |
| Session boundary | `core/agent/session.py:20` | Treat a conversation as a runtime-owned state container |
| Snapshot persistence | `core/agent/persistence.py:13` | Save/load serializable session state without serializing live resources |

### Most important pattern: “adapt everything to one loop”
This repo’s strongest architectural move is not any single class; it is the rule that **new capability must conform to existing contracts**:
- new provider -> emit stream events
- new tool source -> implement `Tool`
- new safety/control feature -> wrap `ToolRegistry.invoke()` or `Agent._agentic_loop()`

That is why the codebase stays understandable as it grows.

---

## 6. Async & Concurrency Model

### What is actually async
- CLI entrypoints: `main.py:37`, `main.py:42`
- agent loop: `core/agent/agent.py:22`, `:37`
- model streaming: `core/client/llm_client.py:45`, `:106`
- hook subprocesses: `core/hooks/hook_system.py:38`
- shell tool subprocesses: `core/tools/builtin/shell.py:69`
- MCP connect/disconnect/call: `core/tools/mcp/client.py:56`, `:83`, `:91`
- subagent execution: `core/tools/subagents.py:43`

### Concurrency model in practice
1. **Single main async control flow** drives each turn.
2. **Streaming is pull-based** via async generators (`LLMClient.chat_completion()` -> `Agent.run()`).
3. **Tools are awaited sequentially** inside a model turn (`core/agent/agent.py:111-146`). This favors determinism over throughput.
4. **Parallel fan-out is used only for independent infrastructure work**, mainly MCP connect/shutdown via `asyncio.gather()` in `core/tools/mcp/mcp_manager.py:34-42` and `:65-68`.
5. **Subprocess work is async but isolated**; there is no background job scheduler or queue abstraction.

### Streaming contract
The runtime has a strong internal event split:
- provider layer: `StreamEventType` (`core/client/datatype.py:17`)
- agent/UI layer: `AgentEventType` (`core/agent/events.py:10`)

This keeps provider normalization separate from user-facing orchestration.

### What is not present
- no worker queue
- no actor system
- no shared mutable concurrency across sessions
- no optimistic parallel tool execution within a single assistant turn

This is a deliberately conservative async design.

---

## 7. Coding Style & Engineering Rules

### Structural conventions
1. **One concept, one module.** Small files often hold the boundary type that makes larger files simple (`core/agent/events.py:10`, `core/client/datatype.py:17`, `core/utils/errors.py:4`).
2. **Runtime nouns are managers/registries/sessions; behavior lives in methods with narrow scope.**
3. **Entry files stay thin.** `main.py` wires configuration, mode selection, and UI; it does not implement model/tool logic.

### Type system style
- **Pydantic models at boundaries**: config and tool params (`core/config/config.py:9`, `core/tools/builtin/read_file.py:8`)
- **Dataclasses for internal DTOs**: events, tool results, token usage, snapshots (`core/client/datatype.py:9`, `core/agent/persistence.py:13`, `core/tools/base.py:63`)
- Practical rule to copy: **validate external inputs early, keep internal transport lightweight**.

### Dependency direction
Preferred flow:
```text
main/CLI -> agent/session -> client/context/tools/safety/hooks/ui -> utils
```

Notably, tools depend on `ToolInvocation` / `ToolResult`, not on `Agent`.

### Error handling style
- Normalize failures into `ToolResult.error_result()` (`core/tools/base.py:72`) instead of throwing through the loop.
- Treat optional infrastructure leniently when appropriate (invalid optional config is skipped in `core/config/loader.py:83-95`).
- Use atomic write patterns for persistence (`core/agent/persistence.py:54`).

### Logging / observability style
- Minimal traditional logging (`core/config/loader.py:11`, `core/tools/registry.py:14`)
- Rich user-facing observability through structured TUI panels (`core/ui/tui.py:160`, `:243`)
- Hooks exist for external observability (`core/hooks/hook_system.py:84`)

### Async rules to copy
- Make I/O interfaces async even if some implementations are internally sync.
- Use async generators for streamed model output.
- Keep turn execution deterministic; parallelize infrastructure setup, not business logic.

### Tool authoring conventions
A tool should:
1. declare `name`, `description`, `kind`, `schema`
2. validate via Pydantic schema
3. return `ToolResult`, never raw text
4. optionally provide `get_confirmation()` for richer approval UX

Examples:
- read-only tool: `core/tools/builtin/read_file.py:28`
- mutating file tool with diff preview: `core/tools/builtin/write_file.py:25`
- mutating shell tool with safety envelope: `core/tools/builtin/shell.py:39`

### Configuration conventions
- secrets come from environment via `Config` properties (`core/config/config.py:108-114`)
- stable config lives in TOML (`core/config/loader.py:76`)
- repo-specific instructions come from `AGENT.MD` and become prompt material (`core/config/loader.py:100-103`, `core/prompts/system.py:25-35`)

### State conventions
- **session-local**: todos, context history, loop detector, turn count
- **cross-session**: user memory file via `MemoryTool` and `Session._load_memory()` (`core/tools/builtin/memory.py:24`, `core/agent/session.py:51`)
- **durable snapshots**: messages + usage + ids, then replay into a fresh session (`main.py:233-267`, `core/agent/persistence.py:44`)

### Practical style rule for another repo
If you want this codebase’s feel, prefer:
- composition over inheritance
- normalized result objects over ad-hoc returns
- manager/registry layers over direct cross-module calls
- extension by discovery/adapters, not conditionals inside the loop

---

## 8. Architectural DNA

### What makes this codebase distinctive
1. **It teaches and scales the same idea twice.** `basic/` shows the irreducible loop; `core/` shows how to professionalize it.
2. **The center is not the model; it is the session.** The real framework abstraction is the runtime boundary in `core/agent/session.py:20`.
3. **Capabilities are integrated, not bolted on.** MCP, discovered tools, memory, subagents, hooks, approval, persistence, and compaction all enter through existing contracts.
4. **Context is treated as an actively managed resource.** This is visible in `ContextManager`, `ChatCompactor`, and `LoopDetector` (`core/context/manager.py:103`, `core/context/compaction.py:57`, `core/context/loop_detector.py:27`).

### Core architectural mindset
> Keep the control loop conceptually stable; move growth into typed boundaries with explicit ownership.

That mindset explains nearly every major design decision in the repo.

### How to reproduce this style in another repository
1. Start with the minimal loop:
   ```text
   prompt -> stream -> tool calls -> tool execution -> updated context -> repeat
   ```
2. Introduce these boundaries early:
   - `Session`
   - `LLMClient`
   - `ContextManager`
   - `Tool` + `ToolRegistry`
3. Normalize everything crossing a boundary:
   - external API chunks -> stream events
   - tool outputs -> result objects
   - persistence -> snapshots
4. Add new features at control points, not everywhere:
   - approval/hooks at registry invoke
   - compaction/loop breaking in the agent loop
   - plugins/MCP through `Tool`
5. Keep presentation separate from orchestration.

### Final cheat-sheet
- **Smallest mental model**: `basic/`
- **Real runtime owner**: `Session`
- **Real extension seam**: `Tool`
- **Real control plane**: `ToolRegistry.invoke()`
- **Real long-session strategy**: compaction + pruning + loop detection
- **Real engineering style**: typed boundaries, normalized events, composition-first growth

