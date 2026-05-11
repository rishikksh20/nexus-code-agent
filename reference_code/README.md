# AI Agent From Scratch

A Python-first experimental coding agent runtime that grows step by step from a basic chat loop into a session-aware, tool-using, approval-gated, MCP-capable interactive agent with a Rich TUI.

This repo is both:

- a working agent CLI, and
- a knowledge base that documents the architecture incrementally in `docs/01-*.md` through `docs/16-*.md`.

---

## What this repo does

At a high level, the agent can:

- chat with an LLM through a streamed interactive CLI
- inspect and modify files in the workspace
- run shell commands
- search locally with `grep` / `glob`
- search and fetch the web
- use memory and todo tools
- load custom tools from `.ai-agent/tools`
- connect to MCP servers and expose their tools
- apply approval and safety checks before risky actions
- run lifecycle hooks
- compact long context and prune old tool outputs
- detect simple repetition loops and inject a loop-break prompt
- save sessions and create/restore checkpoints

---

## How to read this repo

This repository is easiest to understand in two passes:

1. **Start with `basic/`** if you want the smallest possible coding-agent loop.
   It shows the essential pattern: prompt + provider + streamed events + tool execution + terminal UI.
2. **Then move to the root `main.py` + `core/` runtime** to see how the same loop grows into a reusable framework with sessions, safety, persistence, MCP, hooks, and richer context management.

That split is intentional:

- `basic/` teaches the **minimum viable agent**
- `core/` teaches the **production-shaped runtime architecture**

---

## Agentic AI coding framework basics

At a conceptual level, a coding agent is not just “an LLM that can answer questions.”
It is a runtime that repeatedly does six things well:

1. **Build context**
   - collect system instructions, user input, previous turns, and available tool schemas
2. **Call a model**
   - stream text and structured tool calls from an LLM provider
3. **Execute tools**
   - read files, edit code, run shell commands, search the workspace, or call external services
4. **Feed results back into context**
   - add tool outputs to the conversation so the model can continue reasoning
5. **Apply runtime controls**
   - approval, hooks, loop detection, context pruning, and error handling
6. **Stop only when the task is complete**
   - return the final answer after the tool loop finishes

This repo implements that pattern in two layers:

- a **teaching implementation** in `basic/`
- a **framework-style implementation** in `core/`

If you are learning agentic systems, the important mental model is:

```text
user request
  -> prompt + conversation state + tools
  -> model stream
  -> optional tool calls
  -> tool execution
  -> updated conversation
  -> model stream again
  -> final answer
```

That loop is the backbone of most coding agents.

---

## Tech stack

- Python `>=3.12`
- `openai` for model access
- `click` for CLI handling
- `rich` for the TUI
- `pydantic` for config and tool schemas
- `tiktoken` for token estimation/counting
- `fastmcp` for MCP client integration
- `ddgs` for web search

Main project metadata lives in `pyproject.toml`.

---

## Quick start

The main CLI entrypoint is `python main.py`.

For a smaller learning-oriented version of the same idea, see `basic/README.md` and `basic/main.py`.

### 1) Install dependencies

Using `uv`:

```bash
cd /home/rishikesh/dev/exp/ai-agent-from-scratch
uv sync
```

Or with `pip`:

```bash
cd /home/rishikesh/dev/exp/ai-agent-from-scratch
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2) Set environment variables

Required:

```bash
export API_KEY="your-provider-api-key"
```

Optional:

```bash
export BASE_URL="https://your-compatible-openai-endpoint/v1"
```

Notes:

- `API_KEY` is required by `Config.validate()`.
- `BASE_URL` is optional and is useful for OpenAI-compatible providers.
- If `python-dotenv` is installed in your environment, `main.py` will also try to load a local `.env` file automatically.

### 3) Run the agent

Interactive mode:

```bash
python main.py
```

Single prompt mode:

```bash
python main.py "Read the repository and summarize the architecture"
```

Use a specific working directory:

```bash
python main.py --cwd /path/to/project
```

---

## Configuration

The runtime merges configuration from:

1. system config directory via `get_config_dir()`
2. project config at `.ai-agent/config.toml`
3. the CLI `--cwd` value for working directory context

It can also load developer instructions from `AGENT.MD` in the current working directory.

Important config areas already supported in the codebase include:

- model settings
- approval policy
- hooks
- shell environment filtering
- allowed tool lists
- MCP servers
- custom developer/user instructions

Example project config:

```toml
[model]
name = "gpt-4.1-mini"
temperature = 0.2

approval = "on-request"
max_turns = 50

[mcp_servers.docs]
enabled = true
command = "python"
args = ["-m", "my_docs_mcp_server"]
```

---

## Start with `basic/`: the minimum coding-agent loop

The `basic/` directory is the smallest complete example in the repo.
It is useful when you want to understand the essence of an agent before looking at the larger framework.

Its runtime shape is roughly:

```text
basic/main.py
  -> load provider settings
  -> build system prompt from live tool definitions
  -> stream provider events
  -> apply events into conversation/UI state
  -> execute requested tools
  -> feed tool results back to the provider
  -> repeat until no more tool calls remain
```

### Key pieces in `basic/`

- `basic/main.py`
  - builds the tool registry
  - selects a provider from config
  - runs the turn loop
  - executes tool calls and pushes results back to the provider

- `basic/src/prompt.py`
  - builds a dynamic system prompt from the live Python tool functions
  - teaches the model what tools exist and how to use them

- `basic/src/providers.py`
  - defines a shared `Provider` interface
  - adapts Gemini and OpenAI-compatible backends into one event-driven contract
  - normalizes streamed text and tool-call output

- `basic/src/events.py`
  - defines simple provider-independent message events such as `START_MESSAGE`, `UPDATE_CONTENT_BLOCK`, and `END_MESSAGE`
  - updates conversation state incrementally with `apply_event(...)`

- `basic/src/tools.py`
  - contains the first useful coding-agent tools: file read/write/edit, bash, glob, and grep-style search

### Why `basic/` matters

`basic/` shows that an agentic coding framework does not start with dozens of abstractions.
It starts with a tight loop:

- give the model tool affordances
- stream its response
- detect tool calls
- execute them safely enough for the experiment
- continue until the model stops asking for tools

Once you understand that loop, the rest of the repository becomes much easier to follow.

---

## How `basic/` grows into the full framework

The top-level runtime keeps the same core loop but turns each concern into a dedicated subsystem.

| `basic/` concept | Full runtime equivalent |
| --- | --- |
| Tool functions in `basic/src/tools.py` | typed tool classes in `core/tools/builtin/` + registry/discovery/MCP |
| Prompt built from tool functions | prompt + config + context assembly in `core/prompts/` and `core/context/` |
| Provider abstraction | `core/client/llm_client.py` and internal stream datatypes |
| One in-memory conversation loop | `ContextManager` with token accounting, pruning, and summary restore |
| Inline execution loop in `basic/main.py` | `Agent` + `Session` orchestration in `core/agent/` |
| Minimal UI updates | Rich TUI panels, streaming, approvals, and tool rendering in `core/ui/tui.py` |
| Simple local tools only | builtin tools + subagents + custom discovered tools + MCP servers |

So the full repo is best read as:

- **same idea**,
- **more structure**,
- **more safety**,
- **more state ownership**,
- **more extensibility**.

---

## Full runtime architecture

The main runtime follows this rough flow:

```text
CLI (main.py)
  -> Agent
    -> Session
      -> LLMClient
      -> ContextManager
      -> ToolRegistry
      -> HookSystem
      -> ApprovalManager
      -> MCPManager
      -> ChatCompactor
      -> LoopDetector
  -> TUI
```

Another useful way to see it is by responsibility layers:

```text
CLI / TUI layer        -> user interaction and rendering
Agent layer            -> multi-step run loop and event emission
Session layer          -> owns runtime-scoped stateful subsystems
Client / Context layer -> model I/O and conversation state
Tools / Safety layer   -> tool execution, approval, hooks, MCP, discovery
Persistence layer      -> saved sessions and checkpoints
```

### One full agent turn, step by step

For a normal coding request, the runtime behaves roughly like this:

1. `main.py` collects user input from CLI or one-shot prompt mode.
2. `CLI._process_message(...)` streams `AgentEvent` objects from `Agent.run(...)` into the TUI.
3. `Agent.run(...)` adds the user message to the `ContextManager` and enters `_agentic_loop()`.
4. `Session` provides the shared runtime objects for that loop: model client, tools, hooks, approval, context, MCP, compaction, and loop detection.
5. `LLMClient` calls the model with current messages plus tool schemas.
6. Streamed text becomes assistant output events; streamed tool calls are collected.
7. `ToolRegistry.invoke(...)` validates parameters, runs hooks, applies approval checks, and executes the tool.
8. Tool results are written back into conversation history.
9. If the model still wants more tools, the loop continues.
10. If there are no more tool calls, the final assistant response is emitted and the turn ends.

### Core components

- `main.py`
  - CLI entrypoint
  - interactive loop
  - slash-command handling
  - event routing into the TUI

- `core/agent/agent.py`
  - main agentic loop
  - model streaming
  - tool execution cycle
  - loop detection integration
  - context compaction/pruning triggers

- `core/agent/session.py`
  - owns runtime-scoped state
  - client, tools, context, hooks, MCP, approval, loop detector, compactor
  - session metadata and stats

- `core/context/manager.py`
  - stores conversation history
  - rebuilds model-facing messages
  - tracks token usage
  - supports compaction restoration and tool-output pruning

- `core/client/llm_client.py`
  - wraps the OpenAI-compatible chat completion API
  - emits stream events for text, tool calls, usage, and errors

- `core/tools/registry.py`
  - registers builtin tools, custom discovered tools, subagents, and MCP tools
  - validates and invokes tools
  - runs hook/approval integration around tool execution

- `core/ui/tui.py`
  - Rich-based interactive display
  - streaming assistant output
  - tool call panels
  - diff/code/output rendering
  - help and approval prompts

### Why `Session` is the architectural center

`Session` is where the repo stops being “just a script” and becomes a framework.

It owns the runtime-scoped objects that have to live across turns:

- the model client
- the tool registry
- MCP connectivity
- the context manager
- approval policy state
- loop detection history
- compaction support
- hooks
- session metadata such as timestamps and ids

That makes it possible for the CLI, persistence layer, and agent loop to share one coherent runtime boundary instead of passing many separate globals around.

---

## Builtin capabilities

Current builtin tools include:

- `read_file`
- `write_file`
- `edit`
- `list_dir`
- `shell`
- `grep`
- `glob`
- `web_search`
- `web_fetch`
- `todos`
- `memory`

The registry also adds:

- subagent tools from `core/tools/subagents.py`
- custom tools discovered from `.ai-agent/tools`
- MCP-backed tools from configured MCP servers

---

## TUI / interactive commands

In interactive mode, type a normal message to talk to the agent, or use a slash command.

### Core commands

- `/help` — show the built-in help panel
- `/exit` or `/quit` — leave interactive mode
- `/clear` — clear conversation history and loop-detector history
- `/config` — show the current runtime configuration
- `/model <name>` — switch the active model name
- `/approval <mode>` — change the approval policy at runtime
- `/stats` — show session statistics
- `/tools` — list currently available tools
- `/mcp` — show MCP server connection status

### Persistence commands

- `/save` — save the current session snapshot
- `/sessions` — list saved sessions
- `/resume <session_id>` — resume a previously saved session
- `/checkpoint` — create a timestamped checkpoint
- `/restore <checkpoint_id>` — restore a checkpoint

### Important command notes

- The TUI help text mentions `/checkpoints`, but `main.py` does not currently implement that command handler yet.
- Approval modes are defined in `core/config/config.py` under `ApprovalPolicy`.
- Some commands reflect current implementation quirks; for example, session/checkpoint restore is implemented by creating a fresh `Session` and replaying saved messages into a new `ContextManager`.

---

## Session and checkpoint storage

Persistence data is stored under the agent data directory from `get_data_dir()`.

Current layout:

```text
<data-dir>/
  sessions/
	<session_id>.json
  checkpoints/
	<session_id>_<timestamp>.json
```

Saved state currently includes:

- session id
- timestamps
- turn count
- model-facing messages
- cumulative token usage

Filesystem permissions are set restrictively in the persistence layer:

- directories: `700`
- files: `600`

---

## Advanced runtime features

This repo already includes several non-trivial runtime controls:

- **approval system** for mutating or risky operations
- **hook system** for before/after agent and tool events
- **MCP integration** for external tool servers
- **custom tool discovery** from `.ai-agent/tools`
- **context compaction** and **tool-output pruning** for long sessions
- **loop detection** and loop-break prompts for repetitive behavior
- **session persistence** and **checkpoint restore**

---

## Repo structure (high level)

```text
main.py                 # CLI entrypoint
core/
  agent/                # agent loop, session, events, persistence
  client/               # model client and stream event types
  config/               # config models and loaders
  context/              # context management, compaction, loop detection
  hooks/                # hook system
  prompts/              # system/compression/loop-break prompts
  safety/               # approval and policy flow
  tools/                # builtin tools, registry, discovery, MCP, subagents
  ui/                   # Rich TUI
  utils/                # helpers (paths, text, errors)
docs/                   # architecture deep dives, 01 through 16
```

---

## Architecture reading guide

If you want to learn the framework from easiest to deepest, this order works well:

1. `basic/main.py`
   - smallest complete tool-using loop
2. `basic/src/providers.py` + `basic/src/events.py`
   - provider abstraction and event normalization
3. `main.py`
   - real CLI entrypoint and command handling
4. `core/agent/agent.py`
   - the main agentic loop
5. `core/agent/session.py`
   - runtime ownership and subsystem composition
6. `core/context/manager.py`
   - message history, token accounting, and pruning
7. `core/tools/registry.py`
   - tool registration, validation, approvals, and invocation
8. `core/ui/tui.py`
   - how streamed output and tool events are presented interactively

That path mirrors how the repository itself evolves: from a small experiment to a more complete coding-agent framework.

---

## Documentation map

If you want the detailed design history, read the docs in order:

- `docs/01-agentic-ai-system-basics.md`
- `docs/02-agentic-ai-system-runtime-and-error-paths.md`
- `docs/03-agentic-ai-system-context-management-and-prompt-construction.md`
- `docs/04-agentic-ai-system-tool-calling-and-execution-runtime.md`
- `docs/05-agentic-ai-system-configuration-and-environmental-context-management.md`
- `docs/06-agentic-ai-system-session-runtime-ownership-and-state-boundaries.md`
- `docs/07-agentic-ai-system-builtin-tool-expansion-and-interactive-tool-observability.md`
- `docs/08-agentic-ai-system-search-and-discovery-tool-expansion.md`
- `docs/09-agentic-ai-system-web-discovery-and-tool-result-specialization.md`
- `docs/10-agentic-ai-system-subagent-delegation-and-prompt-surface-specialization.md`
- `docs/11-agentic-ai-system-mcp-tool-integration-and-runtime-management.md`
- `docs/12-agentic-ai-system-context-compaction-summary-restoration-and-tool-output-pruning.md`
- `docs/13-agentic-ai-system-safety-and-approval-mechanisms.md`
- `docs/14-agentic-ai-system-hook-system.md`
- `docs/15-agentic-ai-system-loop-detection-and-loop-breaking.md`
- `docs/16-agentic-ai-system-session-persistence-checkpointing-and-runtime-restoration.md`

---

## Current caveats

This repo is intentionally evolving in public, so some areas are still rough edges rather than polished product features. A few examples visible in the current codebase:

- some command/help surfaces are slightly ahead of implementation (for example `/checkpoints`).
- parts of the session persistence flow still have implementation nuances around `turn_count` handling.
- some runtime warnings or type mismatches still exist in the codebase during ongoing development.
- some older docs or comments may lag behind the current implementation while the repo is being refactored in public.

So the best way to understand the project is:

1. use this README for orientation,
2. use `main.py` + `core/` for actual behavior,
3. use the numbered docs for the architectural deep dive.


