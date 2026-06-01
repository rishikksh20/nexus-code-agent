# Nexus AI Coding Agent

**Nexus** is a CLI-first AI coding agent and terminal-based pair programmer. It runs in an interactive REPL or headless one-shot mode, executes tools, manages sessions, and keeps context across long conversations through compaction and carry-over summaries.

> **Status**: active scaffold — core runtime, streaming model clients, safety, skills, MCP, delegation, sandboxing, JSON observability, and optional OpenTelemetry tracing are implemented.

---

## Table of Contents

1. [Features](#features)
2. [Directory Structure](#directory-structure)
3. [Setup](#setup)
4. [Running Nexus](#running-nexus)
   - [Interactive REPL](#interactive-repl)
   - [Headless One-Shot](#headless-one-shot)
   - [Running in a Custom Workspace](#running-in-a-custom-workspace)
5. [CLI Arguments](#cli-arguments)
6. [Slash Commands](#slash-commands)
7. [Examples](#examples)
8. [Built-in Tools](#built-in-tools)
9. [Skills](#skills)
10. [Configuration](#configuration)
11. [Prompt Architecture](#prompt-architecture)
12. [Providers](#providers)
13. [Observability](#observability)

---

## Features

- **Interactive REPL** with readline history, arrow-key navigation, and inline confirmation prompts
- **Headless / one-shot** execution via `--prompt`, `--prompt-file`, or `--stdin`
- **Typed agent loop** — normalized message, tool-call, and tool-result contracts keep provider adapter logic outside the runtime
- **Deterministic approval resume** — approved tool calls resume from the exact pending call shown to the user, avoiding provider-regenerated permission loops
- **Built-in tools** — file read/write/edit, glob, grep, Python LSP-style code intelligence, ls, bash (with risk classification), web fetch/search, memory, todos, and more
- **Permission system** — `plan` / `default` / `auto` execution modes with per-tool risk gating
- **Session persistence** — JSON snapshots under `.nexus/sessions/`; resume by session name or opt into latest-session resume
- **Context compaction** — token-budget management with carry-over summaries; soft/hard limits auto-tuned to the active model's context window
- **Skills** — Markdown skill files loaded from builtin, global, and workspace directories; activated per-session
- **MCP integration** — tool discovery over subprocess stdio
- **Plugin system** — load custom-tool `.py` files from global and project roots
- **Cognitive sub-agents** — specialized agent tools with isolated context and normal nested tool permissions
- **Sandbox** — Docker-backed command execution with resource and network limits
- **Lifecycle hooks** — JSONL runtime logs, aggregated metrics, and audit trail for mutating actions
- **Post-session learning** — workspace knowledge and user profile updated out of band after each session
- **`nexus doctor`** — production-readiness gate check

---

## Directory Structure

```
nexus/                         # Main package
├── app.py                     # CLI entrypoint, startup, runtime routing
├── models.py                  # Shared runtime contracts (Message, ToolCall, etc.)
├── prompts.py                 # Top-level prompt shim (delegates to nexus/prompts/)
│
├── cli/                       # Argument parsing, init, headless execution
│   ├── args.py                # Click CLI definition and subcommands
│   ├── doctor.py              # Production-readiness checks
│   ├── headless.py            # Headless run logic
│   ├── init.py                # `nexus init` workspace bootstrap
│   └── input.py               # Prompt input resolution (--stdin, --prompt-file)
│
├── config/                    # Config loading and defaults
│   ├── defaults.py            # AgentConfig dataclass with all fields and defaults
│   ├── loader.py              # Global + local TOML merge and env-var injection
│   └── model_limits.py        # Context-window limits for 40+ models
│
├── context/                   # Context management
│   ├── builder.py             # ContextSections dataclass + ContextBuilder renderer
│   ├── compactor.py           # Token estimation, message compaction, tool pruning
│   └── loop_detector.py       # Repetition and cycle detection
│
├── extensions/
│   └── plugins.py             # Plugin loader — registers tools from global and project roots
│
├── hooks/                     # Event-driven hook system
│   ├── events.py              # HookEvent enum
│   ├── executor.py            # HookExecutor — register and dispatch handlers
│   ├── integration.py         # setup_hooks() factory
│   └── payloads.py            # Typed payload dataclasses
│
├── integrations/              # Model and protocol adapters
│   ├── anthropic.py           # Native Anthropic client
│   ├── fake_model.py          # Deterministic local fake client for CI
│   ├── gemini.py              # Native Gemini client
│   ├── ollama.py              # Native Ollama client
│   ├── openai_compatible.py   # Live OpenAI-compatible HTTP client
│   └── retry.py               # Bounded retry helper
│
├── memory/                    # Persistence layer
│   ├── store.py               # Key-value memory store under .nexus/memory/
│   ├── workspace.py           # Workspace knowledge (.nexus/knowledge.md, facts.json)
│   └── profiles.py            # User profile (~/.nexus/profile.md, workspaces.json)
│
├── observability/             # Logging, metrics, and audit
│   ├── logging.py             # JsonlRuntimeLogger
│   ├── metrics.py             # RuntimeMetricsCollector → metrics.json
│   └── audit.py               # JsonlAuditTrail for mutating actions
│
├── prompts/                   # Prompt construction
│   ├── system.py              # Section builders: identity, env, security, operational, etc.
│   └── compression.py         # Context-compaction continuation prompt
│
├── runtime/                   # Core agent execution
│   ├── agent.py               # Agent class — agentic loop, tool dispatch, hooks
│   ├── agent_scope.py         # Supervisor/sub-agent tool, skill, and MCP visibility logic
│   ├── repl.py                # Turn runner plus interactive REPL loop
│   ├── repl_state.py          # ReplState — session, history, config, approval manager
│   ├── slash_commands.py      # /command router and all slash-command handlers
│   ├── sessions.py            # Session snapshot persistence
│   ├── runtime_session.py     # RuntimeSession assembly
│   ├── execution.py           # ExecutionMode enum
│   ├── delegation.py          # Legacy worker runtime kept for compatibility tests
│   ├── post_session.py        # Post-session workspace and profile learning
│   └── sandbox.py             # Sandbox runtime wiring
│
├── sandbox/                   # Docker sandbox execution
│   ├── docker.py              # Docker client wrapper
│   ├── tool.py                # SandboxedBashTool
│   ├── agent_tool.py          # Cognitive SubAgentTool implementation
│   └── factory.py             # Sandbox factory
│
├── security/                  # Permission and approval system
│   ├── permissions.py         # PermissionChecker — tool gating by mode and risk
│   ├── policy.py              # ApprovalPolicy enum
│   ├── classifier.py          # CommandClassifier — bash risk levels
│   └── manager.py             # ApprovalManager — per-turn/session approval tracking
│
├── skills/                    # Skill loading and registry
│   ├── loader.py              # Discover and load Markdown skill files
│   ├── parser.py              # SKILL.md YAML frontmatter + body parser
│   ├── registry.py            # SkillRegistry
│   └── models.py              # Skill dataclass
│
├── tools/                     # Tool abstractions and built-ins
│   ├── base.py                # BaseTool protocol + ToolRegistry
│   ├── mcp.py                 # MCP tool discovery and registry adapters
│   ├── registry.py            # register_core_tools() factory
│   ├── filesystem.py          # Extended filesystem tools with PermissionChecker
│   ├── subagents.py           # Skill sub-agent tool registration
│   └── builtin/               # Individual built-in tool implementations
│       ├── time.py            # get_time
│       ├── read_file.py       # read_file
│       ├── write_file.py      # write_file
│       ├── edit_file.py       # edit
│       ├── smart_edit.py      # insert_edit_into_file
│       ├── patch.py           # apply_patch
│       ├── glob.py            # glob
│       ├── grep.py            # grep
│       ├── lsp.py             # lsp
│       ├── list_dir.py        # ls
│       ├── shell.py           # bash
│       ├── memory.py          # memory
│       ├── todo.py            # todos
│       ├── web_fetch.py       # web_fetch
│       └── web_search.py      # web_search
│
├── ui/
│   └── terminal.py            # TerminalUI — Rich-backed console rendering
│
└── builtin_skills/
    ├── nexus-agent/
    │   └── SKILL.md           # Built-in Nexus self-help skill
    ├── python-code-review/
    │   └── SKILL.md           # PEP 8 + Google Style Guide code review skill
    └── note-taking/
        └── SKILL.md           # Append time-stamped notes to notes.toml

tests/                         # Pytest test suite
docs/                          # Architecture plans and tutorial reference material
workspace/                     # Example project workspace — run nexus here
```

---

## Setup

**Requirements**: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
# Clone and install with dev dependencies
git clone <repo-url>
cd build-an-ai-agent
uv sync --group dev
```

**Create a `.env` file** in the workspace root (loaded automatically at startup):

```bash
# .env — all LLM settings in one place
PROVIDER=openai-compatible
MODEL=mistral-medium-latest
API_KEY=your_api_key_here
BASE_URL=https://api.mistral.ai/v1
```

> **Tip**: `BASE_URL` can point to any OpenAI-compatible endpoint — Mistral, OpenAI,
> Ollama (`http://localhost:11434/v1`), vLLM, LM Studio, etc.

**Initialize workspace state** (creates `.nexus/` state and seeds `.agents/skills/`):

```bash
uv run nexus init
```

---

## Running Nexus

### Interactive REPL

```bash
uv run nexus
```

The startup banner shows the active provider, model, and mode:

```
Provider: openai-compatible  |  Model: mistral-medium-latest  |  Mode: default
```

Resume a specific session with `--session`, or resume the latest saved session with `--resume-last`:

```bash
uv run nexus --session abc123def
uv run nexus --resume-last
```

Type your question at the `>` prompt. Unknown slash commands are forwarded to the agent as natural-language queries.

### Headless One-Shot

```bash
# Inline prompt
uv run nexus --prompt "summarize this repo"

# From a file
uv run nexus --prompt-file task.txt

# From stdin
echo "what time is it" | uv run nexus --stdin

# Write output to a file in JSON format
uv run nexus --prompt "list all tools" --output result.json --output-format json
```

In non-interactive (non-TTY) runs Nexus exits with code `3` when confirmation is required — pass `--auto-confirm` or `--mode plan` to avoid blocking.

### Running in a Custom Workspace

Nexus always uses the **current working directory** as the workspace root. To run the agent against the `./workspace` directory:

**Step 1 — Create a `.env` inside `workspace/`** (if not already present):

```bash
cat > workspace/.env << 'EOF'
PROVIDER=openai-compatible
MODEL=mistral-medium-latest
API_KEY=your_api_key_here
BASE_URL=https://api.mistral.ai/v1
EOF
```

**Step 2 — Initialize Nexus inside the workspace:**

```bash
cd workspace
uv run nexus init
```

This creates `workspace/.nexus/` with a `config.toml`, `sessions/`, `memory/`,
and `knowledge.md`, then copies missing packaged skills into
`workspace/.agents/skills/`.

**Step 3 — Run the agent:**

```bash
# Interactive REPL (from inside workspace/)
cd workspace
uv run nexus

# Headless one-shot (from inside workspace/)
cd workspace
uv run nexus --prompt "what files are here?"
```

You can also stay in the repo root and use a subshell:

```bash
(cd workspace && uv run nexus)
(cd workspace && uv run nexus --prompt "summarize this project")
```

Or with a single command:

```bash
# Init
uv run --directory workspace nexus init

# Interactive REPL
uv run --directory workspace nexus

# Headless
uv run --directory workspace nexus --prompt "summarize this project"
```

> `uv run --directory <path>` changes the working directory before running the command, so Nexus picks up `<path>/.env` and `<path>/.nexus/` automatically.

---

## CLI Arguments

### Main Flags

| Flag | Short | Description |
|---|---|---|
| `--prompt TEXT` | `-p` | Run headless with this prompt |
| `--prompt-file FILE` | `-f` | Read headless prompt from a file |
| `--stdin` | | Read headless prompt from stdin |
| `--session NAME` | `-s` | Resume or create a named session |
| `--resume-last` | | Resume the latest saved session for this workspace |
| `--no-session` | | Skip session persistence for this run |
| `--model NAME` | `-m` | Override the model from config |
| `--provider NAME` | | Override the provider (`anthropic`, `cohere`, `fake`, `gemini`, `mistral`, `ollama`, `openai`, `openai-compatible`) |
| `--allow-hidden-paths` | | Allow hidden/private path reads except `.nexus/`, which remains blocked |
| `--mode MODE` | | Execution mode: `plan`, `default`, or `auto` |
| `--config FILE` | `-c` | Path to a local config TOML file |
| `--global-config FILE` | | Path to a global config TOML file |
| `--max-tokens N` | | Override the compaction hard limit |
| `--max-turns N` | | Override the max agent loop iterations |
| `--output FILE` | `-o` | Write the final response to a file |
| `--output-format` | | Response format: `text`, `json`, or `jsonl` |
| `--stream` | | Enable streamed output (overrides config) |
| `--no-stream` | | Disable streamed output |
| `--quiet` | `-q` | Suppress tool call and progress output |
| `--verbose` | `-v` | Enable debug-level logging |
| `--auto-confirm` | | Auto-approve all mutating tool calls |
| `--deny-mutating` | | Deny all mutating tools (implies plan mode) |
| `--allowed-tools LIST` | | Comma-separated allowlist of tool names |
| `--denied-tools LIST` | | Comma-separated denylist of tool names |
| `--skill NAME` | | Activate a skill for this run (repeatable) |
| `--no-plugins` | | Skip plugin loading |
| `--no-skills` | | Skip skill loading |

### Subcommands

| Command | Description |
|---|---|
| `nexus init [--force]` | Create config/state directories and seed workspace `.agents/skills/` |
| `nexus version` | Print version and exit |
| `nexus doctor [--output-format text\|json\|jsonl]` | Run production-readiness gate checks |
| `nexus config [global\|local\|merged]` | Print a config layer |

---

## Slash Commands

Available inside the interactive REPL. Every command accepts a `help` subcommand (e.g. `/mode help`) that prints a table of its subcommands and examples.

| Command | Description |
|---|---|
| `/help` | Show all available slash commands |
| `/mode [plan\|default\|auto]` | Show or switch execution mode |
| `/context [show\|usage]` | Print system prompt or show supervisor token/context-window usage, including tool, MCP, sub-agent, and skill prompt/schema estimates |
| `/context agents\|agent \<id\>\|usage \<id\>` | Inspect per-agent context isolation, handoffs, and usage |
| `/provider [list\|profiles\|use \<profile\>\|manage\|set \<param\> \<value\>]` | Inspect providers, activate reusable profiles, open Textual settings, or update live parameters |
| `/config [show\|set\|reset\|reset-defaults\|reload\|upgrade\|reinit]` | Inspect or edit configuration; `reset-defaults` rewrites clean defaults |
| `/skills [list\|show\|add\|remove\|reload]` | Manage session skills and skill-backed sub-agent tools |
| `/agent [status\|tools\|skills\|mcp\|allow\|disallow]` | Inspect and scope supervisor tools, skills, and MCP servers |
| `/sub-agent [list\|show\|tools\|skills\|mcp\|allow\|disallow]` | Inspect and scope cognitive sub-agent resources |
| `/session [new\|list\|resume\|save\|export]` | Manage sessions |
| `/memory [list\|search\|save\|show]` | Workspace memory entries |
| `/tools [reload]` | List registered tools or reload core, plugin, MCP, and sub-agent tools |
| `/history [n]` | Show recent conversation messages |
| `/mcp [status\|available\|activate\|deactivate\|tools\|refresh [server]\|reload]` | Inspect and manage MCP server activation, status, tools, and reloads |
| `/abort` | Abort the currently running agent turn |
| `/quit` or `/exit` | Save session and exit |

---

## Examples

### Interactive REPL

```bash
cd workspace
uv run nexus
```

```
Provider: openai-compatible  |  Model: mistral-medium-latest  |  Mode: default
Type /help for available commands.

> what files are in the nexus/tools directory?
nexus/tools/ contains: base.py, registry.py, filesystem.py, builtin/, ...

> /mode auto
Mode set to: auto

> add a docstring to nexus/tools/base.py
[read_file — auto-approved]
[edit — confirmation required]
Done. Added a module-level docstring describing the BaseTool protocol and ToolRegistry.

> /context usage
┌─ Context Usage: supervisor ──────────────────────────┐
│ Provider                       openai-compatible     │
│ Model                          mistral-medium-latest │
│ Context window                 131,072 tokens        │
│ System prompt (est.)           ~420 tokens           │
│ History (est.)                 ~1,840 tokens         │
│ Tool schemas (est.)            ~900 tokens           │
│ Sub-agent schemas (est.)       ~320 tokens           │
│ MCP schemas (est.)             ~180 tokens           │
│ Active skills prompt (est.)    ~240 tokens           │
│ Total used incl. schemas       ~3,660 tokens (2.8%)  │
└──────────────────────────────────────────────────────┘

> /session save
Saved session: abc123def

> /quit
Saving session and exiting.
```

### Headless One-Shot

```bash
# Read-only code review (plan mode — no mutations allowed)
uv run nexus --mode plan --prompt "review nexus/security/permissions.py for issues"

# Auto-approve all changes, write output as JSON
uv run nexus \
  --prompt "add type hints to all functions in nexus/tools/base.py" \
  --auto-confirm \
  --output result.json \
  --output-format json

# Use native Ollama
uv run nexus \
  --provider ollama \
  --model qwen2.5-coder:7b \
  --prompt "summarize this repo in bullet points"

# Use native Anthropic or Gemini
uv run nexus --provider anthropic --model claude-sonnet-4-5 --prompt "summarize this repo"
uv run nexus --provider gemini --model gemini-2.5-pro --prompt "summarize this repo"

# Activate a specific skill for this run
uv run nexus --skill nexus-agent --prompt "how do I configure MCP servers?"

# Pipe output from another command
cat error.log | uv run nexus --stdin --prompt "what is causing this error?"
```

---

## Built-in Tools

All tools pass through the permission system and lifecycle hooks. **Risk level** determines whether user confirmation is required.

| Tool | Risk | Mutating | Description |
|---|---|---|---|
| `get_time` | — | No | Returns the current UTC timestamp |
| `read_file` | low | No | Reads a file or line range within the workspace |
| `glob` | low | No | Finds files by glob pattern within the workspace |
| `grep` | low | No | Searches file content by regex; returns path, line number, match |
| `lsp` | low | No | Inspects Python symbols, definitions, references, and hover details |
| `code_index` | low | No | Builds a lightweight Python AST/import/symbol index |
| `semantic_search` | low | No | Searches Python code by concept using lexical and symbol matches |
| `git_status` | low | No | Returns structured branch and working tree status |
| `git_diff` | low | No | Returns working, staged, file, or ref diffs |
| `run_tests` | low/medium | No | Runs tests with structured pass/fail metadata |
| `run_python_check` | low/medium | No | Runs structured Python syntax verification |
| `run_formatter` | medium | Yes | Runs the formatter command with approval |
| `list_dir` | low | No | Lists directory contents with file sizes |
| `web_fetch` | low | No | Fetches a URL and returns the response body |
| `web_search` | low | No | Runs a web search and returns result snippets |
| `memory` | low/medium | Yes for writes | Saves, retrieves, lists, or searches workspace memory |
| `todos` | low/medium | Yes for writes | Adds, lists, updates, or completes session todos |
| `edit` | medium | Yes | Applies targeted file edits inside the workspace |
| `insert_edit_into_file` | medium | Yes | Inserts or replaces text near an anchor in an existing file |
| `apply_patch` | medium | Yes | Applies a unified patch with workspace-boundary checks |
| `write_file` | **high** | Yes | Creates or fully overwrites a file — **always requires confirmation**, even in auto mode |
| `bash` | **dynamic** | Yes | Runs a bash command; risk classified per command (see below) |

Compatibility tool classes such as `modify_file` and `replace_text` still exist for older tests/docs, but the normal core registry exposes the canonical tools above.

### Cognitive Sub-Agent Tools

Sub-agent tools are normal registry tools that let the supervisor call a focused inner agent. They are disabled by default for conservative single-agent operation. Turn them on per workspace with:

```toml
# .nexus/config.toml
agent_mode = "advanced"

# Easiest option: allow every registered tool, including cognitive tools.
allowed_tools = []
denied_tools = []
```

If the workspace uses a non-empty `allowed_tools` allowlist, it must include the cognitive tool names. For built-in specialists, use:

```toml
agent_mode = "advanced"
allowed_tools = [
  "get_time", "read_file", "write_file", "edit", "insert_edit_into_file",
  "apply_patch", "glob", "grep", "list_dir", "lsp", "git_status", "git_diff",
  "run_tests", "run_python_check", "bash",
  "subagent_planning_analysis", "subagent_execution",
  "subagent_review", "subagent_verification",
]
```

Built-in cognitive tools:

| Tool | Purpose |
|---|---|
| `subagent_planning_analysis` | Read-only repo analysis and implementation planning |
| `subagent_execution` | Focused implementation work using normal workspace tools |
| `subagent_review` | Code review for bugs, regressions, and maintainability risks |
| `subagent_verification` | Runs tests, lint/type checks, and summarizes failures |

Custom workspace sub-agents are configured in `.nexus/config.toml` with `delegation_subagents`. Each entry becomes a tool named `subagent_<name>`:

```toml
agent_mode = "advanced"
delegation_subagents = [
  {
    name = "explore",
    description = "Investigate a focused codebase question.",
    goal_prompt = "Read the relevant code and summarize the answer. Do not modify files.",
    allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp"],
    max_turns = 12,
    timeout_seconds = 300
  }
]

# Required only when allowed_tools is non-empty:
# allowed_tools = ["subagent_explore", "read_file", "glob", "grep", "list_dir", "lsp", ...]
```

Skill-backed sub-agent tools are also supported. Create a skill named `subagent-review` or `subagent_review` under `.nexus/skills/` or `~/.nexus/skills/`, then run `/skills reload`; it registers as `subagent_review` when `agent_mode = "advanced"`.

Agent-scoped resources are layered on top of global activation. Use `/mcp activate` and `/skills activate` to make MCP servers and skills globally available, then use `[agents]` or `[[sub-agents]]` `allowed_*` lists to narrow or expand what each agent can see. `/agent allow ...` and `/sub-agent allow ...` update those allowlists in `.nexus/config.toml`.

```toml
[agents]
allowed_tools = []          # empty = default supervisor behavior; "all" = every normal workspace tool
allowed_skills = []         # empty = all globally active skills; "all" = every active skill
allowed_mcps = []           # empty = default MCP behavior; "all" = every active MCP server

[[sub-agents]]
name = "execution"
allowed_tools = ["read_file", "write_file", "edit", "insert_edit_into_file", "apply_patch", "glob", "grep", "list_dir", "lsp", "git_status", "git_diff", "run_tests", "run_python_check", "bash"]
allowed_skills = []         # empty = no extra skill metadata by default; "all" = every active skill
allowed_mcps = []           # empty = built-in sub-agent MCP inheritance/defaults; "all" = every active MCP server
```

In advanced mode, the supervisor sees cognitive `subagent_*` tools by default and only the direct normal tools, MCP servers, and skills allowed under `[agents]`; work outside that supervisor allowlist should be delegated to an appropriate sub-agent. In basic mode, direct tools remain available unless narrowed by config. Sub-agents start from their normal `allowed_tools`; a non-empty `[[sub-agents]].allowed_tools` list replaces that base. Set an `allowed_*` value to `"all"` to use every workspace-active tool, skill, or MCP server for that scope. Agent-scoped skills are shown as metadata only. Older top-level `agent_*`, `subagent_profiles`, and `allowed_mcp_servers` keys are still accepted as aliases; obsolete attach/detach keys are ignored.

Useful commands after editing `.nexus/config.toml`:

```text
/config upgrade local  # merge new default keys/tool allowlist entries and reload tools
/config reload         # reload config and .env values
/tools reload          # rebuild the live tool registry from the current config
/tools                 # confirm which subagent_* tools are registered
/skills reload         # rescan skills and register skill-backed sub-agent tools
/agent tools           # inspect supervisor-scoped tool visibility
/sub-agent show execution # inspect one sub-agent's effective resources
/context agents        # inspect sub-agent context isolation and handoffs
```

### YAML Sub-Agents

In addition to `delegation_subagents` in `config.toml`, you can define sub-agents as standalone `.yml` files — one file per agent. Nexus discovers them automatically from two directories:

| Scope | Path | Priority |
|---|---|---|
| Global | `~/.nexus/agents/<name>.yml` | Base |
| Local (workspace) | `.nexus/agents/<name>.yml` | Overrides global |

**Minimal example** (`.nexus/agents/explore.yml`):

```yaml
name: explore
description: Investigate a focused codebase question and summarize the answer.
goal_prompt: |
  Read the relevant code and summarize the answer. Do not modify files.
allowed_tools:
  - read_file
  - glob
  - grep
  - list_dir
  - lsp
allowed_skills: []   # omit or leave empty to allow all active skills
allowed_mcps: []     # omit or leave empty to allow all active MCP servers
max_turns: 12
timeout_seconds: 300
```

The file name (without `.yml`) must match the `name` field. The sub-agent is registered as `subagent_<name>` when `agent_mode = "advanced"` or the name appears in `delegation_subagents`.

**REPL commands:**

```text
/sub-agent agents list               — list all discovered YAML agent files
/sub-agent agents new <name>         — scaffold a local .nexus/agents/<name>.yml
/sub-agent agents new <name> global  — scaffold a global ~/.nexus/agents/<name>.yml
/sub-agent agents reload             — re-scan and register new YAML agents live
/sub-agent agents promote <name>     — move local → global
/sub-agent agents demote <name>      — move global → local
```

YAML agents participate in the same definition priority chain as built-in and config agents: built-ins → `delegation_subagents` → YAML files (YAML wins on name collision). `/tools reload` also rebuilds YAML agents. See [`docs/sub-agents-integration.md`](docs/sub-agents-integration.md) for the full field reference and examples.

### Approval Flow

In `default` mode, mutating or risky tools emit a confirmation event before execution. The turn runner owns the user prompt for both interactive and headless flows. After approval, Nexus resumes `Agent.run()` with the exact pending tool call that was displayed in the confirmation panel; it does not ask the model to regenerate the call. This keeps approval behavior deterministic across providers.

Approval policies:

| Policy | Behavior |
|---|---|
| `on-request` | Ask for each confirmable invocation unless already approved |
| `approve-turn` | Approval can cover compatible mutating calls for the current user turn |
| `approve-session` | Approval can persist for the session and matching invocation signature |
| `auto` | Skip confirmations allowed by policy/mode; high-risk bash still requires confirmation |
| `plan` | Deny mutating actions |

### Bash Risk Classification

| Risk | Examples | `default` mode | `auto` mode | `plan` mode |
|---|---|---|---|---|
| **low** | `cat`, `grep`, `ls`, `echo`, `git status`, `git log` | Auto-approved | Auto-approved | Auto-approved |
| **medium** | `rm <file>`, `mv`, `mkdir`, `cp`, `git commit`, `sed -i`, `>` redirect, package installs | Requires confirmation | Auto-approved | **Denied** |
| **high** | `rm -rf`, `sudo`, `kill -9`, `killall`, `\| bash`, `dd if=`, `mkfs` | Requires confirmation | **Requires confirmation** | **Denied** |

High-risk bash commands always require confirmation regardless of execution mode.

---

## Skills

Nexus supports Agent Skills: directory-based instruction packs with a required
`SKILL.md` file containing YAML frontmatter and Markdown instructions. Skills
are discovered as a catalogue, then activated by name for a workspace or one
CLI run.

### Discovery Order

Later roots override earlier roots when skill names collide:

1. Packaged built-ins — `nexus/builtin_skills/`
2. Extra `skill_paths` from config
3. Global catalogue — `~/.nexus/skills/`
4. Workspace skills — `.nexus/skills/`
5. Standard Agent Skills path — `.agents/skills/`

`nexus init` copies missing packaged built-ins into `.agents/skills/`. This
workspace copy wins during discovery and is readable by filesystem tools, so
the agent can inspect full skill instructions and bundled resources when
needed. Existing workspace copies are preserved unless `nexus init --force`
is used.

### Activation

Workspace activation is stored in `.nexus/config.toml`:

```toml
skill_paths = []
enabled_skills = ["nexus-agent"]
disabled_skills = []
```

`enabled_skills` and `disabled_skills` accept exact names, glob patterns such
as `review-*`, and regex patterns prefixed with `re:`.

The system prompt includes skill metadata only: name, description, source,
active state, and `SKILL.md` path. Full skill instructions stay in the skill
file and can be inspected with `/skills show <name>` or read from the listed
path when the task calls for it.

### Managing Skills in the REPL

```
/skills list                 — list discovered skills, source, path, and active state
/skills show nexus-agent     — print the skill's SKILL.md
/skills activate my-skill    — activate a skill in local config
/skills deactivate my-skill  — deactivate a skill in local config
/skills create-local review  — create .nexus/skills/review/SKILL.md
/skills remove-local review  — remove a workspace-local skill
/skills reload               — rescan skills and refresh the prompt
/agent allow skill review   — allow active skill metadata for the supervisor
/sub-agent allow review skill python-code-review — allow active skill metadata for a sub-agent
```

`/skills add` and `/skills remove` remain aliases for activate/deactivate.

### Activating from the CLI

```bash
uv run nexus --skill my-skill --prompt "use the skill"
uv run nexus --no-skills   # disable all skill loading
```

`--skill` is run-only and does not write config.

### Skill Format

```markdown
---
name: code-review
description: Review code changes for bugs, regressions, and missing tests. Use when the user asks for review.
---

# Code Review

Inspect diffs first, report findings with file references, and keep summaries brief.
```

See [`docs/skills.md`](docs/skills.md) for the full Nexus skill guide.

---

## Configuration

Configuration is resolved in this order (later layers override earlier ones):

1. Built-in defaults (`nexus/config/defaults.py`)
2. Global config — `~/.nexus/config.toml`
3. Local config — `.nexus/config.toml`
4. `.env` file in the workspace root (injected at startup; takes priority over system env)
5. `AGENT_*` environment variables
6. CLI flags

### `.env` File (Recommended)

The simplest way to configure the provider is a `.env` file in the workspace root:

```bash
# .env
PROVIDER=openai-compatible          # or: mistral, openai, anthropic, cohere, gemini, ollama, fake
MODEL=mistral-medium-latest         # any model supported by the endpoint
API_KEY=your_api_key_here           # generic key — works for any provider
BASE_URL=https://api.mistral.ai/v1  # any OpenAI-compatible endpoint
```

**Env var resolution order for API key:**

| Provider | Lookup order |
|---|---|
| `openai-compatible` | `API_KEY` → `NEXUS_API_KEY` → `OPENAI_API_KEY` |
| `mistral` | `MISTRAL_API_KEY` → `NEXUS_API_KEY` → `OPENAI_API_KEY` → `API_KEY` |
| `openai` | `OPENAI_API_KEY` → `NEXUS_API_KEY` → `API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` → `API_KEY` |
| `cohere` | `COHERE_API_KEY` → `CO_API_KEY` → `NEXUS_API_KEY` → `API_KEY` |
| `gemini` | `GEMINI_API_KEY` → `GOOGLE_API_KEY` → `API_KEY` |
| `ollama` | No API key required |

**Env var resolution order for base URL:**

| Provider | Lookup order |
|---|---|
| `openai-compatible` / `openai` | `BASE_URL` env var → `api_base_url` in config |
| `mistral` | `MISTRAL_BASE_URL` → defaults to `https://api.mistral.ai/v1` |
| `cohere` | `COHERE_BASE_URL` / `CO_API_BASE_URL` → defaults to `https://api.cohere.com` |
| `ollama` | `OLLAMA_HOST` → `BASE_URL` → defaults to `http://localhost:11434` |
| `anthropic` / `gemini` | Native SDK providers; `api_base_url` is unused by default |

### Workspace-Level Config (`.nexus/config.toml`)

Provider names are fixed adapter cards. Reusable provider settings and model
profiles belong in `~/.nexus/config.toml`; a workspace selects one profile in
`.nexus/config.toml`. Use `/provider manage` in the Textual UI, or
`/provider profiles` and `/provider use <profile>` in either interactive UI.

```toml
# ~/.nexus/config.toml
[providers.ollama]
enabled = true
base_url = "http://localhost:11434"
timeout_seconds = 300
max_retries = 0

[models.fast-local-coder]
provider = "ollama"
model_name = "qwen2.5-coder:7b"
context_length = 32768
max_output_tokens = 8192
reserved_output_tokens = 8192
temperature = 0.05
top_p = 0.9
supports_tools = true
supports_streaming = true
supports_reasoning = false

[models.fast-local-coder.thinking]
enabled = false
mode = "provider_default"
```

```toml
# .nexus/config.toml
active_model_profile = "fast-local-coder"
```

Without an active profile, Nexus synthesizes a `legacy-current` profile from
the existing flat provider fields, so older configs continue to work.

```toml
# Provider and model — can be omitted if set via .env
provider = "openai-compatible"
model_name = "mistral-medium-latest"
api_base_url = "https://api.mistral.ai/v1"
# api_key = "sk-..."  # prefer setting API_KEY in .env

# Execution
default_mode = "default"         # plan | default | auto
max_loop_iterations = 8
auto_confirm_read_only = true
parallel_tools = true             # run eligible non-mutating tools in parallel within a single turn
parallel_tool_window = 4          # max parallel non-mutating tool calls per window (1-8)

# Context and compaction
compaction_soft_limit = 85197    # auto-tuned to 65% of model context window
compaction_hard_limit = 111411   # auto-tuned to 85% of model context window
compaction_keep_recent = 12
context_prune_enabled = true
context_prune_protect_tokens = 40000
context_prune_minimum_tokens = 20000

# Output
stream_output = true
show_tool_calls = true
log_format = "text"              # text | json (json enables structured observability)

# Sessions
save_on_every_turn = true
max_sessions_retained = 50

# Approvals and path policy
approval_policy = "on-request"     # on-request | approve-turn | approve-session | auto | plan
allow_hidden_paths = false         # .agents/skills and .agents/tools stay readable; .nexus stays blocked

# Tool filtering
allowed_tools = []               # empty = all tools allowed
denied_tools = []

# Custom instructions injected into every system prompt
developer_instructions = ""
user_instructions = ""

# Project metadata (injected into system prompt)
project_name = ""
project_description = ""

# MCP servers
# See docs/mcp-integration.md for a full setup guide.
#
# Filesystem MCP (npm package — do NOT use uvx):
#   Install: npm install -g @modelcontextprotocol/server-filesystem
#   Command: ["mcp-server-filesystem", "/absolute/path/to/workspace"]

#### Parallel Tool Execution

- `parallel_tools = true` enables per-turn parallel execution for eligible non-mutating tools.
- `parallel_tool_window = 4` sets the batch size for each parallel window. Valid values are `1` through `8`.
- The scheduler still runs mutating tools sequentially. When a turn mixes reads and writes, Nexus drains the read-only parallel windows first and then executes the remaining sequential tools.
- This applies to both the supervisor agent and sub-agents. Nested `subagent_*` tool calls themselves are still kept out of the parallel lane.
#
# Git MCP (Python package — use uvx or pip install mcp-server-git):
#   Command: ["uvx", "mcp-server-git", "--repository", "/absolute/git/repo/root"]
#   The path must be the git repo root (where .git/ lives).
#   Run: git rev-parse --show-toplevel  to find the correct path.
#
# After editing this list in a running REPL, run /mcp reload.
# Inspect with /mcp status · list tools with /mcp tools · rediscover with /mcp refresh.
mcp_servers = [
  { name = "filesystem", transport = "stdio", command = ["mcp-server-filesystem", "."], prefix = "fs_", startup_timeout_seconds = 10, tool_timeout_seconds = 60 },
  { name = "git", transport = "stdio", command = ["uvx", "mcp-server-git", "--repository", "."], prefix = "git_", startup_timeout_seconds = 15, tool_timeout_seconds = 60 }
]
enabled_mcp_servers = []        # enable global MCP catalog entries by name
disabled_mcp_servers = []       # disable local or global MCP entries by name

# Optional MCP fields: env, cwd, disabled, disabled_tools.

# Agent profile
config_version = 3
agent_mode = "basic" # basic | advanced
# basic = single-LLM execution with no cognitive sub-agent tools.
# advanced = supervisor LLM with cognitive sub-agent tools.
# Built-in cognitive tools in advanced mode: subagent_planning_analysis,
# subagent_execution, subagent_review, subagent_verification
delegation_subagents = []
# Custom sub-agents become tools named subagent_<name>.
# Example:
# delegation_subagents = [
#   { name = "explore", description = "Investigate a focused codebase question.", goal_prompt = "Read the relevant code and summarize the answer.", allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp"], max_turns = 12, timeout_seconds = 300 }
# ]

# Sandbox
sandbox_commands = false
sandbox_image = "nexus-sandbox:latest"
sandbox_timeout_seconds = 30
sandbox_network = "none"
```

When an interactive session starts, Nexus checks the workspace `.nexus/config.toml`
against the current template. If keys are missing, deprecated keys are present,
the config version is old, or the local tool allowlist is missing current
default tools, Nexus asks before upgrading. Legacy keys are accepted for
compatibility; `/config upgrade local` removes deprecated keys, adds the current
schema version, merges default tool allowlist entries, and reloads live tools.

### User-Level Config (`~/.nexus/config.toml`)

Same format as the workspace config. Applied to all workspaces; overridden by workspace-level settings.

---

### Custom Tools

Custom tools are Python plugin files with a `register(registry, hooks)`
function. Nexus discovers them from these roots; later roots override an
earlier file with the same stem:

1. Global plugins — `~/.nexus/plugins/`
2. Workspace compatibility plugins — `.nexus/plugins/`
3. Project-readable custom tools — `.agents/tools/`

Use `.agents/tools/` for project custom tools that the agent should inspect
with `read_file`, `glob`, `grep`, or `list_dir`. Run `/tools reload` after
editing a plugin while the REPL is open. Nexus does not currently ship
packaged custom-tool plugins, so `nexus init` has nothing to copy into this
directory.

Plugin files execute Python during startup. Review project plugins before
running Nexus in an unfamiliar workspace.

---

### MCP Servers (Git + Filesystem)

Nexus can connect to external MCP servers and expose their tools through the normal tool registry. The two most useful servers are the official **filesystem** server and the **git** server.

#### Install

```bash
# Filesystem MCP — npm package (do NOT use uvx)
npm install -g @modelcontextprotocol/server-filesystem

# Git MCP — Python package (uvx fetches on demand, no install needed)
uvx mcp-server-git --help
# or install permanently:
pip install mcp-server-git
```

#### Configure in `.nexus/config.toml`

```toml
mcp_servers = [
  {
    name      = "filesystem",
    transport = "stdio",
    command   = ["mcp-server-filesystem", "/absolute/path/to/workspace"],
    prefix    = "fs_",
    startup_timeout_seconds = 10,
    tool_timeout_seconds    = 60
  },
  {
    name      = "git",
    transport = "stdio",
    command   = ["uvx", "mcp-server-git", "--repository", "/absolute/path/to/repo-root"],
    prefix    = "git_",
    startup_timeout_seconds = 15,
    tool_timeout_seconds    = 60
  }
]
```

> **Git path:** Point `--repository` at the directory containing `.git/` — not a subdirectory.
> Run `git rev-parse --show-toplevel` to confirm the correct path.

> **Filesystem command:** Use `mcp-server-filesystem` directly (npm binary). Do **not** prefix with `uvx` — it is not a Python package.

#### Activate MCP servers

```toml
enabled_mcp_servers = ["filesystem", "git"]
disabled_mcp_servers = []
```

Do not add MCP tool names to `allowed_tools`. Local and global MCP server
definitions form a catalog and are activated per workspace by name. Once a
server is active, Nexus discovers its tools at
startup and registers all discovered tools except any remote names listed in
that server's `disabled_tools`.

#### Verify with REPL slash commands

```bash
uv run nexus
```

```
# Check connection status
/mcp status

# List global/local servers and workspace activation state
/mcp available

# Activate or deactivate a server by name
/mcp activate filesystem
/mcp deactivate filesystem

# List all discovered tools
/mcp tools

# After editing config in a running REPL
/mcp reload

# Rediscover tools without restarting the server process
/mcp refresh
/mcp refresh git
```

See [`docs/mcp-integration.md`](docs/mcp-integration.md) for the full configuration reference, all supported fields, tool name tables, safety behavior, and troubleshooting guide.

### Key Directories

| Path | Purpose |
|---|---|
| `.nexus/config.toml` | Workspace config |
| `.nexus/sessions/` | Session JSON snapshots |
| `.nexus/memory/` | Workspace memory entries |
| `.nexus/knowledge.md` | Workspace knowledge (post-session learning) |
| `.nexus/facts.json` | Structured workspace facts |
| `.nexus/audit-trail.jsonl` | Audit log for every mutating action |
| `.nexus/skills/` | Workspace-level custom skills |
| `.agents/skills/` | Workspace-readable Agent Skills, including seeded packaged skills |
| `.agents/tools/` | Workspace-readable custom-tool plugins (`.py` files) |
| `~/.nexus/config.toml` | Global user config |
| `~/.nexus/skills/` | Global user skills |
| `~/.nexus/plugins/` | Custom tool plugins (`.py` files) |
| `~/.nexus/profile.md` | User profile (post-session learning) |
| `~/.nexus/logs/runtime.jsonl` | JSONL runtime event log |
| `~/.nexus/logs/metrics.json` | Aggregated runtime metrics |

---

## Prompt Architecture

The system prompt is assembled each turn from structured sections:

| Section | Content |
|---|---|
| **Identity** | Agent role, capabilities, pair-programming framing |
| **Environment** | OS, Python version, shell (static, platform-level) |
| **AGENTS.md** | Scope and precedence rules for `AGENTS.md` files |
| **Security** | Secrets policy, path validation, injection defence |
| **Tool Guidelines** | Available tools listed with best-practice usage notes |
| **Developer Instructions** | Injected from `developer_instructions` config |
| **User Instructions** | Injected from `user_instructions` config |
| **Operational** | Tone & style, 6-step workflow, task execution, error recovery, code references, professional objectivity, coding guidelines |
| **Environment (per-turn)** | Current UTC time and date, CWD, workspace, mode, provider, model |
| **Tools (per-turn)** | Live-rendered tool list from the active registry |
| **Skills** | Active skill Markdown content |
| **Project Notes** | Project name, description, knowledge file extract |
| **Carry-Over** | Pinned facts and summaries from prior compaction rounds |
| **Current Task** | The current user task focus |

### Context Compaction

When history grows large the compactor:

1. Estimates token counts with the `len // 4` heuristic
2. Triggers when the soft limit (default: 65% of model context window) is reached
3. Summarises older messages into `CarryOverState` (pinned facts, summaries, constraints)
4. Trims the message list to fit within the hard limit (default: 85%)

The compaction prompt (`nexus/prompts/compression.py`) uses a structured 7-section continuation format:

```
## ORIGINAL GOAL
## COMPLETED ACTIONS (DO NOT REPEAT THESE)
## CURRENT STATE
## IN-PROGRESS WORK
## REMAINING TASKS
## NEXT STEP
## KEY CONTEXT
```

### Loop Detection

`nexus/context/loop_detector.py` tracks recent tool-call signatures and detects exact repeats and short cycles. When triggered, a corrective system notice is injected into the next turn via `nexus/prompts/system.py:create_loop_breaker_prompt()`.

---

## Providers

Provider cards are registered in `nexus/integrations/registry.py`. The provider
name identifies the API adapter class; the user-created entity is the model
profile. All agents and cognitive sub-agents use the same resolved active
profile in this release.

The Textual UI exposes `/provider manage` with **Providers** and **Model
Profiles** tabs. Credential forms store environment-variable names only, never
secret values. Live profile tests require a second confirmation click because
they can incur a small provider charge.

| Provider | Value | Notes |
|---|---|---|
| Fake | `fake` | Deterministic; no API key required; used for CI |
| OpenAI-compatible | `openai-compatible` | **Default.** Any compatible endpoint — Mistral, Ollama, vLLM, LM Studio, etc. Set `BASE_URL` and `API_KEY` in `.env` |
| Mistral | `mistral` | `api_base_url` auto-defaults to `https://api.mistral.ai/v1`; key via `MISTRAL_API_KEY` |
| OpenAI | `openai` | Requires `BASE_URL` (or `api_base_url`) and `OPENAI_API_KEY` |
| Ollama | `ollama` | Native local Ollama client; defaults to `http://localhost:11434`; no API key required |
| Anthropic | `anthropic` | Native Anthropic SDK client; key via `ANTHROPIC_API_KEY` |
| Cohere | `cohere` | Native HTTP Cohere Chat API v2 client; key via `COHERE_API_KEY` or `CO_API_KEY` |
| Gemini | `gemini` | Native Google GenAI SDK client; key via `GEMINI_API_KEY` or `GOOGLE_API_KEY` |

**Common endpoint examples:**

```bash
# Mistral (via OpenAI-compatible)
BASE_URL=https://api.mistral.ai/v1
MODEL=mistral-medium-latest

# OpenAI
BASE_URL=https://api.openai.com/v1
MODEL=gpt-4o

# Local Ollama via OpenAI-compatible
BASE_URL=http://localhost:11434/v1
MODEL=qwen2.5-coder:7b
API_KEY=ollama    # Ollama accepts any non-empty key

# Native Ollama
PROVIDER=ollama
MODEL=qwen2.5-coder:7b
OLLAMA_HOST=http://localhost:11434

# Anthropic
PROVIDER=anthropic
MODEL=claude-sonnet-4-5
ANTHROPIC_API_KEY=your_key_here

# Cohere
PROVIDER=cohere
MODEL=command-a-plus-05-2026
COHERE_API_KEY=your_key_here

# Gemini
PROVIDER=gemini
MODEL=gemini-2.5-pro
GEMINI_API_KEY=your_key_here

# vLLM
BASE_URL=http://localhost:8000/v1
MODEL=your-model-id
```

Override at runtime:

```bash
uv run nexus --provider openai-compatible --model mistral-large-latest --prompt "hello"
uv run nexus --provider openai-compatible --model llama3.2 --prompt "hello"
uv run nexus --provider ollama --model qwen2.5-coder:7b --prompt "hello"
```

---

## Observability

When `log_format = "json"`, Nexus writes structured logs to `~/.nexus/logs/`:

| File | Content |
|---|---|
| `runtime.jsonl` | Lifecycle events: prompt submission, pre/post tool use, notifications, stop |
| `metrics.json` | Aggregated counters — prompt submissions, tool calls, token usage, cost by session |
| `.nexus/audit-trail.jsonl` | Durable record of every mutating action with state (`requested` / `executed`) and rollback notes |

Each hook payload includes `session_id`, `turn_id`, `trace_id`, and `tool_call_id` correlation fields. `runtime.jsonl` now also records turn start/end, model start/end, and context-compaction events.

When tracing is enabled, Nexus also writes span records to `~/.nexus/logs/traces.jsonl`. Those spans cover one root `nexus.turn` span per turn, one `nexus.model` span per model call, one span per tool call, and event spans for compaction, warnings, and errors.

```bash
# Enable JSON observability in local config
echo 'log_format = "json"' >> .nexus/config.toml

# Run production-readiness checks
uv run nexus doctor
uv run nexus doctor --output-format json
```

Sentry remote monitoring is optional and metadata-only by default. Enable it with a DSN in config or env:

```bash
AGENT_SENTRY_ENABLED=true
SENTRY_DSN=https://public@example.ingest.sentry.io/123
SENTRY_ENVIRONMENT=production
```

Use the project DSN from Sentry web: Project Settings -> Client Keys (DSN) -> DSN. You do not need a Sentry auth token for event ingestion. `SENTRY_ENVIRONMENT` is optional but useful for filtering, and `SENTRY_RELEASE` is optional if you want release tagging.

Sentry events include session, turn, trace, provider/model, tool name/source, approval, MCP, usage, and duration fields. Raw prompts and tool outputs are not sent unless `sentry_include_prompts` or `sentry_include_tool_outputs` is explicitly enabled.

OpenTelemetry tracing is optional and is the primary remote tracing path. Nexus writes local JSONL spans directly and can export the same spans over OTLP to Langfuse or any other OTLP-compatible backend.

Install the optional tracing stack and either configure OTLP directly or use Langfuse compatibility keys:

```bash
uv sync --extra observability

AGENT_OTEL_ENABLED=true
OTEL_SERVICE_NAME=nexus
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer your-token

# Or let Nexus derive Langfuse OTLP auth and endpoint
AGENT_LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_ENVIRONMENT=production
LANGFUSE_RELEASE=nexus@local
```

Recommended local config:

```toml
log_format = "json"

otel_enabled = true
otel_endpoint = ""
otel_headers = ""
otel_service_name = "nexus"
otel_environment = "development"
otel_release = ""
otel_trace_content = true
otel_trace_tool_outputs = true
otel_prompt_name = "nexus-system-prompt"
otel_prompt_version = ""
otel_jsonl_enabled = true

# Optional Langfuse compatibility if you want Langfuse as the OTLP backend.
langfuse_enabled = true
langfuse_public_key = ""
langfuse_secret_key = ""
langfuse_base_url = "https://cloud.langfuse.com"

sentry_enabled = true
sentry_dsn = ""
```

Nexus does not import prompts from Langfuse. Local Nexus prompts remain the source of truth; the tracing layer only emits prompt metadata and optional prompt content from the local runtime. Use Langfuse when you want OTLP-backed session replay and trace visualization. Keep using Sentry for exceptions, crashes, breadcrumbs, provider/tool failures, and operational alerting. See `docs/llm-observability-langfuse-sentry.md` for the event map and setup checklist.

---

## Running Tests

```bash
uv run pytest
```
