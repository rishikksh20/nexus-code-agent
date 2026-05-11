# Nexus Agent Framework

Nexus Agent Framework is a CLI-first Python agent harness scaffold built from the repository's action-plan and tutorial docs.

This repository is not just a single CLI entrypoint. It is a staged implementation of an agent harness, where the docs describe the target architecture and the `nexus/` package contains the progressively implemented runtime.

The current implementation focuses on the early runtime and safety layers:

- typed runtime models for messages, tool calls, tool results, requests, and responses
- interactive REPL and headless execution paths
- builtin tools with explicit mutating vs read-only behavior
- permission gating with plan, default, and auto execution modes
- workspace and global config hierarchy
- workspace knowledge bootstrap and file-based memory
- session persistence with retention pruning
- lifecycle hooks and JSONL runtime logging
- lightweight pytest coverage for the implemented paths

## Status

This repo is currently a stable scaffold, not a finished production harness.

Implemented now:

- builtin file-system tools: `read_file`, `write_file` (high-risk, always confirmed), `modify_file`, `replace_text`, `glob`, `grep`, `ls`, and `bash` with a three-level risk classifier (`low` / `medium` / `high`); high-risk bash commands require user confirmation even in auto mode
- fake model client for deterministic local development
- OpenAI-compatible adapter boundary types plus a live `/chat/completions` client path with bounded retries
- Mistral provider support through the same live compatible client boundary
- Mistral is now the default provider; `mistral-medium-latest` is the default model
- `api_base_url` defaults to `https://api.mistral.ai/v1` for the `mistral` provider
- `.env` file in the workspace root is loaded at startup and injects env vars (including `MISTRAL_API_KEY`) before config resolution; `.env` values take priority over system env
- `api_key` config field allows setting the provider API key directly in `.nexus/config.toml` or via `AGENT_API_KEY`
- context-window limits for 40+ known models (Mistral, OpenAI, fake) stored in `nexus/config/model_limits.py`; compaction soft and hard thresholds are auto-tuned at startup to 65% and 85% of the active model's context window unless the user has overridden them explicitly
- REPL startup banner shows active provider, model, and mode; warns if no API key is found for live providers
- local `.nexus/` workspace state and global `~/.nexus/` defaults
- merged global and local skill discovery with explicit per-session skill activation
- built-in `nexus-agent` skill that ships with the package, auto-activates in every session, and answers natural-language questions about Nexus commands, config, and providers
- custom skills in `~/.nexus/skills/` and `.nexus/skills/` override built-in skills on name collision
- post-session workspace learning to `.nexus/knowledge.md` and `.nexus/facts.json`
- post-session user profile learning to `~/.nexus/profile.md` and `~/.nexus/workspaces.json`
- deeper context compaction with carry-over state for longer sessions
- slash commands for config, mode, session, tools, MCP status inspection, memory, context, history, and exit
- every slash command has a `help` subcommand (`/context help`, `/mode help`, etc.) showing all subcommands and examples
- unknown slash-command input is forwarded to the agent as a natural-language query instead of being silently dropped
- delegation runtime with coordinator-owned task state, typed mailboxes, worker loops, approvals, and optimistic resource coordination
- basic usage and estimated cost accounting in session metadata
- turn-level telemetry with `turn_id` and `trace_id` correlation in runtime events
- audit-trail logging for mutating actions in `.nexus/audit-trail.jsonl`
- redacted JSONL observability plus aggregated `metrics.json`
- bounded retry helper with retryable vs non-retryable provider failure handling
- clarification and approval request handling for tool execution
- plugin-based local tool loading from `~/.nexus/plugins/`
- MCP tool discovery over subprocess stdio
- Docker-backed sandboxed command execution when enabled and configured

Not implemented yet:

- model-driven automatic skill selection
- true streamed provider output from the live provider path
- trace export / OpenTelemetry integration
- multi-user authn/authz and hosted deployment controls

## Repository Guide

This repo is organized into three main surfaces:

1. implementation code under `nexus/`
2. planning and chapter docs under `docs/`
3. validation and regression coverage under `tests/`

If you are new to the repo, the most useful reading order is:

1. `README.md` for the current implementation state
2. `phase1-roadmap.md` for the original implementation baseline
3. `next_roadmap.md` for the next recommended implementation passes
4. `docs/action-plan/` for the target architecture by chapter
5. `nexus/app.py` and the `nexus/runtime/` package for the actual execution path

## Repository Layout

### Root Files

- `README.md`: current implementation overview, setup, runtime behavior, and limitations
- `phase1-roadmap.md`: original foundation roadmap used to structure the initial build
- `next_roadmap.md`: detailed follow-up roadmap organized by current implementation areas
- `REVIEW_REPORT.md`: review summary and hardening observations from prior passes
- `Mistral.md`: provider-specific setup guide for running Nexus against Mistral
- `pyproject.toml`: package metadata, CLI entrypoint, and test dependencies
- `instruction.md`: currently unused placeholder at repo root

### Implementation Package

The `nexus/` package is the actual runtime.

- `app.py`: top-level CLI startup, config loading, registry construction, model client selection, REPL and headless routing
- `models.py`: normalized runtime contracts shared across the system
- `prompts.py`: prompt and context assembly
- `skills.py`: skill discovery and registry loading

Subpackages inside `nexus/`:

- `cli/`: argument parsing, init flow, input resolution, and headless execution support
- `config/`: defaults and config loading and validation
- `extensions/`: plugin loading support
- `integrations/`: fake model, live OpenAI-compatible path, MCP integration, and retry helper
- `memory/`: file-backed memory, workspace state helpers, and profile structures
- `observability/`: runtime JSONL logs, metrics aggregation, and audit trail handling
- `runtime/`: the core agent loop, REPL flow, compaction, permissions, sessions, delegation, sandbox, and slash commands
- `tools/`: tool abstractions and built-in tool implementations

### Docs

The `docs/` directory is the design and implementation reference for the repo.

- `docs/action-plan/`: chapter-by-chapter target architecture and rollout guidance
- `docs/openai-code-tutorial/`: tutorial-style supporting material and audit notes used as a reference during implementation

Within `docs/action-plan/`, the most relevant areas for the current repo state are:

- `01-foundations/`: early runtime loop, tools, and typed model groundwork
- `02-runtime-and-safety/`: state, permissions, testing, and observability foundations
- `03-extensions-and-scale/`: MCP, sandboxing, and delegation
- `04-production-blueprint/`: rollout and operational posture
- `05-advanced-continuity/`: skills, learning, advanced observability, provider boundaries, config, slash commands, and headless control

### Tests

The `tests/` directory is focused on behavior-level coverage rather than framework-heavy fixtures.

It currently covers:

- agent loop behavior
- config validation and merge behavior
- sessions and persistence
- prompt construction and continuity features
- hook logging and audit behavior
- slash-command handling
- retry behavior and provider integration slices
- headless CLI execution
- delegation and related coordination flows

## Planning And Documentation Files

The repo now has distinct planning documents for different purposes:

- `phase1-roadmap.md`: what was needed to build the first working scaffold
- `next_roadmap.md`: what should be implemented next, section by section, based on the current codebase
- `REVIEW_REPORT.md`: what was reviewed, what was hardened, and what risks remained at review time

This split matters because the README should describe the repo as it exists now, while the roadmap files should describe future work.

## Requirements

- Python 3.11+
- `uv`

## Install

```bash
uv sync --group dev
```

## Quick Start

Initialize local and global Nexus config/state:

```bash
uv run nexus init
```

Run the interactive REPL:

```bash
uv run nexus
```

On startup the REPL prints the active provider, model, and mode:

```
Provider: mistral  |  Model: mistral-medium-latest  |  Mode: default
```

If no API key is detected for a live provider, a warning with setup instructions is shown before the prompt.

Type a natural-language question at the `>` prompt to send it to the agent. Unrecognised slash commands are also forwarded to the agent as queries.

Inspect MCP state inside the REPL:

```text
/mcp status
/mcp tools
/mcp refresh
/mcp refresh filesystem
```

Enable delegation and inspect worker coordination in the REPL:

```text
/delegate status
/delegate workers
/delegate tasks
/delegate spawn "Review docs" "Summarize the foundations chapter." --worker worker-1
/delegate approvals
```

Run one headless prompt:

```bash
uv run nexus --prompt "what time is it"
```

When a one-shot run is started from an interactive terminal, confirmation and clarification prompts are shown inline. In non-interactive runs (for example CI or piped execution without a TTY), Nexus exits with code `3` when confirmation is required unless you pass `--auto-confirm` or use `--mode plan` / `--deny-mutating`.

Run against an OpenAI-compatible endpoint:

```bash
export OPENAI_API_KEY="your-token"
uv run nexus --provider openai-compatible --model your-model --prompt "summarize this repo"
```

Set `api_base_url` in `.nexus/config.toml` or `AGENT_API_BASE_URL` when using a local or hosted compatible endpoint.

Run against Mistral:

```bash
export MISTRAL_API_KEY="your-token"
uv run nexus --provider mistral --model mistral-small-latest --prompt "summarize this repo"
```

Mistral defaults to `https://api.mistral.ai/v1`. Override it with `api_base_url`, `AGENT_API_BASE_URL`, or `MISTRAL_BASE_URL` when needed. For a full setup guide, see [`Mistral.md`](./Mistral.md).

Run tests:

```bash
uv run --group dev python -m pytest -q
```

Run against Mistral (default provider):

```bash
# Add your key to a .env file at the workspace root:
echo 'MISTRAL_API_KEY=sk-...' > .env
uv run nexus
```

Or export it as an environment variable:

```bash
export MISTRAL_API_KEY="your-token"
uv run nexus --prompt "summarize this repo"
```

## Development Workflow

The repo is meant to be worked on incrementally.

Typical workflow:

1. update or read the relevant chapter under `docs/action-plan/`
2. implement the narrowest runtime slice under `nexus/`
3. add or extend focused tests under `tests/`
4. run `uv run --group dev python -m pytest -q`
5. update `README.md` and any impacted chapter docs in the same pass

That pattern matches how the current implementation has been built so far: small vertical slices, immediate validation, then documentation alignment.

## CLI Surface

Core subcommands:

- `nexus` — start the interactive REPL
- `nexus init` — initialize `.nexus/` and `~/.nexus/` state
- `nexus version` — print version
- `nexus doctor` — run production-readiness gate check
- `nexus config show [global|local|merged]` — print config

Slash commands (inside the REPL):

| Command | Description |
|---|---|
| `/help` | Summary table of all commands |
| `/context [show\|usage\|help]` | Print system prompt or show token/context window usage stats |
| `/mode [plan\|default\|auto\|help]` | Show or switch execution mode |
| `/provider [list\|set <p> <v>\|help]` | Show or update provider, model, temperature, etc. |
| `/config [show\|set\|reset\|reload\|reinit [local\|global]\|help]` | Inspect or edit config; `reinit` rewrites the target config file to clean defaults |
| `/skills [list\|show\|add\|remove\|reload\|help]` | Manage session skills |
| `/session [new\|list\|resume\|save\|export\|help]` | Manage sessions |
| `/memory [list\|search\|save\|show\|help]` | Workspace memory |
| `/tools [list\|enable\|disable\|info\|help]` | List registered tools and their risk / mutating flag |
| `/history [n\|help]` | Show conversation history |
| `/mcp [status\|tools\|refresh\|help]` | Inspect MCP server status |
| `/delegate [status\|workers\|tasks\|spawn\|…\|help]` | Multi-agent delegation |
| `/quit` or `/exit` | Save and exit |

Every slash command accepts a `help` subcommand (e.g. `/context help`) that prints a concise table of its subcommands with examples.

Headless inputs:

- `--prompt`
- `--prompt-file`
- `--stdin`

Common overrides:

- `--mode plan|default|auto`
- `--model`
- `--provider`
- `--skill <name>`
- `--max-turns`
- `--output`
- `--output-format text|json|jsonl`
- `--auto-confirm`
- `--no-session`
- `--no-plugins`
- `--no-skills`
- `--deny-mutating`

## Execution Model

### Startup

`nexus.app:main` loads config, injects any `.env` file from the workspace root, auto-tunes compaction limits to match the active model's context window, ensures directories exist, initializes workspace knowledge, builds the tool registry, loads skills (including the built-in `nexus-agent` skill), and routes into either REPL or headless mode. If a previous session exists in `.nexus/sessions/`, the REPL resumes it automatically and prints the session ID and message count in the startup banner. Pass `--no-session` or run `/session new` inside the REPL to start fresh. The REPL also warns if no API key is found. At session end it updates workspace knowledge and the user profile in a post-session pass.

### Config

Config is resolved in this order:

1. built-in defaults
2. global config in `~/.nexus/config.toml`
3. local config in `.nexus/config.toml`
4. `.env` file in the workspace root (parsed at startup; keys are injected into the process environment; `.env` values take priority over system env for the same keys)
5. `AGENT_*` environment variables
6. CLI overrides

Invalid config is rejected early.

Provider auth resolution order for Mistral: `MISTRAL_API_KEY` → `NEXUS_API_KEY` → `OPENAI_API_KEY`. Add your key to a `.env` file at the workspace root:

```
MISTRAL_API_KEY=sk-...
```

Provider selection is validated at config-load time. Accepted values: `fake`, `mistral`, `openai`, `openai-compatible`. `mistral` defaults `api_base_url` to `https://api.mistral.ai/v1`. Other live providers require `api_base_url` to be set explicitly before startup.

### Agent Loop

The runtime loop:

1. builds context from config, tools, workspace knowledge, and task input
2. compacts message history to a token budget (thresholds auto-set to 65%/85% of the model's known context window) with carry-over summaries for older context
3. calls the model client with normalized request types
4. emits usage notifications if present
5. requests clarification for missing tool arguments before execution
6. requests approval for mutating actions when required by mode
7. executes allowed tools and appends tool results back into history
8. emits lifecycle hooks for prompt submission, tool execution, notifications, audit, and stop

### Sessions

Sessions are persisted as JSON snapshots under `.nexus/sessions/`. The configured `max_sessions_retained` value is enforced on save by pruning the oldest saved sessions, and `latest_session.txt` records the most recently saved session ID.

On startup, Nexus automatically resumes the last saved session. The banner shows:

```
Resumed session abc123def (14 messages). Use /session new to start fresh or /session list to pick another.
```

To opt out of session persistence entirely, pass `--no-session`. To resume a specific session, pass `--session <id>`. Inside the REPL:

| Command | What it does |
|---|---|
| `/session new` | Save the current session and start a fresh one |
| `/session list` | Show all saved sessions with ID, timestamp, and summary |
| `/session resume <id>` | Load a previous session's messages into context |
| `/session save` | Persist the current session to disk immediately |
| `/session export <path>` | Export session messages as JSON |

### Memory And Knowledge

- workspace memory entries live under `.nexus/memory/`
- workspace knowledge lives in `.nexus/knowledge.md`
- structured workspace facts live in `.nexus/facts.json`
- the mutating-action audit trail lives in `.nexus/audit-trail.jsonl`
- user profile summaries live in `~/.nexus/profile.md`
- workspace-level learning summaries live in `~/.nexus/workspaces.json`
- global defaults live under `~/.nexus/`

### Skills

- skills are loaded from three sources in order: the built-in package skills directory, the global skills directory (`~/.nexus/skills/`), and the local directory (`.nexus/skills/`)
- local skill names override global ones; global skill names override built-in ones when names collide
- the built-in `nexus-agent` skill is auto-activated at REPL startup and answers natural-language questions about Nexus itself (suppressed with `--no-skills`)
- active skills are injected deliberately into prompt context for the current session only
- the REPL supports `/skills list|show|add|remove|reload`

## Builtin Tools

All tools go through the permission system and lifecycle hooks before and after execution. Risk levels determine the confirmation behaviour.

| Tool | Risk | `is_mutating` | Description |
|---|---|---|---|
| `get_time` | — | No | Returns the current UTC timestamp. |
| `write_note` | medium | Yes | Writes a small note file inside the workspace. Rejects oversized payloads and paths outside the workspace or under `.nexus/`. |
| `read_file` | low | No | Reads a file (or a line range) within the workspace. |
| `write_file` | **high** | Yes | Creates or fully overwrites a file. Always requires confirmation — even in auto mode. Cannot write outside the workspace or into `.nexus/`. |
| `modify_file` | medium | Yes | Replaces a specific line range in an existing file. Confirmed in default mode, auto-approved in auto mode. |
| `replace_text` | medium | Yes | Finds and replaces a literal string in a file (first occurrence or all). Confirmed in default mode. |
| `glob` | low | No | Searches for files and directories within the workspace by glob pattern (e.g. `**/*.py`). |
| `grep` | low | No | Searches file contents for a regex or literal string; returns file path, line number, and matching line. |
| `ls` | low | No | Lists directory contents within the workspace with file sizes. |
| `bash` | **dynamic** | Yes | Runs a bash command in the workspace root. Risk is classified per command — see bash risk rules below. |

External tools (MCP, plugins) are marked by source in the prompt context, `/tools` output, and runtime hook payloads.

### Bash Risk Classification

Before every `bash` tool call the command string is classified as `low`, `medium`, or `high` risk. The permission decision is based on that classification:

| Risk | Examples | Default mode | Auto mode | Plan mode |
|---|---|---|---|---|
| **low** | `cat`, `grep`, `ls`, `echo`, `git status`, `git log` | Auto-approved | Auto-approved | Auto-approved |
| **medium** | `rm file`, `mv`, `mkdir`, `cp`, `git commit`, `sed -i`, `>` redirect, package installs | User confirmation | Auto-approved | Denied |
| **high** | `rm -rf`, `sudo`, `kill -9`, `killall`, pipe to shell (`\| bash`), `dd if=`, `mkfs` | User confirmation | **User confirmation** | Denied |

High-risk bash commands **always** require user confirmation regardless of execution mode. Auto mode only bypasses confirmation for medium-risk commands.

External tools (MCP, plugins) are marked by source in the prompt context, `/tools` output, and runtime hook payloads.

## MCP Setup

Add one or more MCP servers to `.nexus/config.toml` using inline tables:

```toml
mcp_servers = [
	{ name = "filesystem", command = ["uvx", "mcp-server-filesystem", "."], prefix = "fs_" }
]
allowed_tools = ["get_time", "write_note", "fs_read_file", "fs_write_file"]
```

Notes:

- each MCP server entry requires `name` and `command`
- `prefix` is optional and helps avoid tool-name collisions
- MCP tools remain behind the internal tool registry and still go through permissions and hooks
- `/mcp refresh` rechecks live MCP connectivity and available remote tools, but does not hot-register new tools into the current session

## Delegation Setup

Enable delegation in `.nexus/config.toml`:

```toml
delegation_enabled = true
delegation_workers = ["worker-1", "worker-2"]
delegation_poll_interval_seconds = 0.05
delegation_message_history_limit = 200
```

The delegation runtime provides:

- explicit coordinator-owned `TaskRecord` state
- typed mailbox messages between coordinator and workers
- a real inner `Agent` loop inside each worker, using a restricted per-task tool registry
- centralized permission decisions via `/delegate approve <decision_id>` and `/delegate reject <decision_id>`
- optimistic resource version checks when multiple delegated tasks claim the same resource name

Worker behavior:

- each delegated task runs through the same typed agent loop used by the main runtime
- workers can be restricted to specific tools with repeated `--tool` flags on `/delegate spawn`
- approval requests are still routed back through the coordinator instead of being auto-approved by workers
- if a delegated task hits a clarification requirement, the worker stops and reports that condition back to the coordinator

Delegation commands in the REPL:

```text
/delegate status
/delegate workers
/delegate tasks [active]
/delegate spawn "Task title" "Focused instructions" --worker worker-1 --tool get_time --resource notes/report.md --permission-action write_note --permission-reason "Need to persist findings"
/delegate messages [participant] [limit]
/delegate approvals
/delegate approve <decision_id>
/delegate reject <decision_id>
```

## Sandbox Setup

Build the sandbox image once:

```bash
docker build -f nexus/Dockerfile.sandbox -t nexus-sandbox:latest .
```

Then enable the sandboxed command tool in `.nexus/config.toml`:

```toml
sandbox_commands = true
allowed_tools = ["get_time", "write_note", "run_command"]
```

The sandboxed tool:

- runs via Docker, not directly on the host
- mounts the workspace at `/workspace`
- disables network by default
- enforces memory, tmpfs, and timeout limits
- still goes through the existing permission system because it is registered as a mutating tool

## Observability

When `log_format = "json"`, lifecycle hooks are recorded to JSONL runtime logs under the configured log directory.

In the same mode, Nexus also writes a durable `metrics.json` snapshot beside `runtime.jsonl` so production runs have lightweight counters without adding a separate metrics backend.

Separately from operational logs, mutating actions are written to `.nexus/audit-trail.jsonl` with explicit action state and rollback notes.

Tracked runtime data currently includes:

- prompt submission
- tool pre/post execution
- notifications
- stop events
- prompt, completion, total token usage, and estimated cost
- aggregated counters by session and tool in `metrics.json`
- `turn_id`, `trace_id`, and `tool_call_id` correlation fields on runtime hook payloads
- tool execution duration in `POST_TOOL_USE` payloads
- recent turn telemetry snapshots in session metadata

## Production Hardening

Minimal production-facing additions now included:

- `nexus doctor` checks the current workspace against runtime, safety, operational, and extension gates
- `write_note` enforces a configurable payload limit through `write_note_max_bytes`
- JSON observability now includes both event logs and an aggregated metrics snapshot
- runtime permission policy now uses `write_note` arguments to hard-deny writes outside the workspace or into Nexus-managed `.nexus/` state
- post-session workspace and user learning runs out of band rather than inline with tool execution
- mutating actions now have a separate audit trail from operational logs

Example:

```bash
uv run nexus doctor --output-format json
```

The doctor report is intentionally local and minimal. It verifies the current config, writable state directories, sandbox readiness when enabled, MCP connectivity when configured, and whether structured production logs are enabled.

## Tests

The repo currently covers:

- agent loop behavior
- tool execution and workspace boundary checks
- config hierarchy and validation
- prompt building stability
- session persistence and retention
- hook logging
- CLI headless behavior
- clarification and approval control flow

## Known Limitations

- the fake model client is included for offline/CI use; `mistral` is the default provider in all generated config files and the built-in dataclass defaults
- `mistral-medium-latest` is the default model; context-window limits for 40+ models are built in to `nexus/config/model_limits.py` and used to auto-tune compaction thresholds
- unsupported provider names and missing `api_base_url` for the live compatible providers now fail early during config loading instead of later at runtime
- skill activation is explicit; the model does not yet request or auto-select skills
- memory search is a simple full-directory scan
- the live provider path currently supports completion requests only, not token streaming
- traces are not exported to an external backend yet

Recently added:

- REPL auto-resumes the last saved session on startup — the banner shows the session ID and message count; use `/session new` to start fresh or `/session list` to pick another
- arrow keys (← → ↑ ↓), Ctrl-A/E, and command history now work at the `>` prompt on macOS (readline activated at import)
- tool call arguments, tool results, and confirmation panel values are all truncated to 150 characters with `…` — no more multi-kilobyte log dumps in the REPL
- confirmation approval prompt now correctly shows `Allow? [y/N]:` (escaped Rich markup bracket)
- `/config reinit` (local) and `/config reinit global` rewrite the target config file to clean Nexus defaults (provider `mistral`, standard settings) and reload immediately — sessions, memory, and knowledge are untouched
- `nexus init` now prints a numbered API key setup guide when no key is detected for the configured provider: `.env` file method, environment variable method, and config TOML method with a link to obtain a Mistral key
- provider errors during REPL turns and headless runs now show a friendly `✗ Request failed.` message instead of a raw Python traceback; covered cases: missing API key, 401/403 auth failure, 429 rate limit, missing `api_base_url`, and connection errors; the REPL removes the failed turn from history and stays open
- `mistral` is the default provider; `api_base_url` defaults to `https://api.mistral.ai/v1`
- `.env` file loading at startup — add `MISTRAL_API_KEY=sk-...` to a `.env` file in your workspace root
- built-in `nexus-agent` skill auto-activates in every REPL session and answers questions about Nexus itself
- `/context usage` shows provider, model, context window size, token estimates, and compaction thresholds
- compaction limits auto-tune to 65%/85% of the active model's context window at startup
- REPL startup banner shows provider, model, and mode; warns if no API key is found
- unknown slash commands are forwarded to the agent as natural-language queries
- every slash command now has a `help` subcommand (e.g. `/context help`, `/skills help`)

Recently fixed hardening gaps:

- arrow key escape sequences (`^[[A` etc.) no longer appear as literal characters at the REPL prompt on macOS
- `--no-session` now runs without persisting session snapshots
- malformed slash command quoting is handled without crashing the REPL
- structured headless `json` and `jsonl` output now emits to stdout when no output file is provided
- hook and logger failures are isolated so observability errors do not abort the runtime
- `write_note` now rejects oversized payloads before writing to disk
- `nexus doctor` now provides a rollout gate check for production readiness
- JSON observability now writes an aggregated `metrics.json` snapshot in addition to `runtime.jsonl`

## Review Report

The current engineering review is documented in [REVIEW_REPORT.md](REVIEW_REPORT.md).