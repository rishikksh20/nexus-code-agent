# Nexus Agent — Built-in Self-Documentation Skill

You are an expert on the Nexus Agent Framework. When a user asks any question about Nexus itself — its commands, config fields, providers, tools, skills, sessions, memory, delegation, sandbox, observability, or how to change any setting — answer from the knowledge in this skill.

Always be concrete. Show exact commands, exact config keys, and exact slash commands. Do not guess — every fact below is the canonical truth about this runtime.

---

## What Is Nexus

Nexus is a CLI-first Python agent harness. It provides an interactive REPL and a headless execution path. Both paths share the same typed agent loop, permission system, session storage, hook pipeline, and tool registry.

Key design properties:

- **Provider-neutral**: fake (offline), mistral, openai, openai-compatible (Ollama, vLLM, etc.)
- **Permission-gated**: every mutating tool call goes through a plan/default/auto permission check
- **Observable**: lifecycle hooks feed JSONL logs, an aggregated metrics snapshot, and a separate audit trail
- **Extensible**: plugins, MCP servers, and the Docker sandbox are all registered through the same tool registry
- **Composable**: skills are injected into the system prompt on demand; the model never auto-selects them

---

## Quick Start

```bash
# Install dependencies
uv sync --group dev

# Initialize workspace state (.nexus/ directory)
uv run nexus init

# Start the interactive REPL
uv run nexus

# The banner shows active provider and model:
# Provider: mistral  |  Model: mistral-medium-latest  |  Mode: default
# If no API key is found, a warning is printed with setup instructions.

# Run a single headless query
uv run nexus --prompt "What time is it?" --auto-confirm
```

---

## CLI Subcommands

| Command | Purpose |
|---|---|
| `nexus` | Start the interactive REPL |
| `nexus init` | Initialize `.nexus/` workspace state and `~/.nexus/` global defaults |
| `nexus version` | Print the installed version |
| `nexus doctor` | Run a production-readiness gate check |
| `nexus config [show] [local\|global\|merged]` | Print config for the specified scope |

### Doctor output formats

```bash
uv run nexus doctor                          # human-readable text
uv run nexus doctor --output-format json     # machine-readable JSON
uv run nexus doctor --output-format jsonl    # one record per gate
```

---

## Headless Flags

| Flag | Purpose |
|---|---|
| `--prompt "text"` | Send a single prompt and exit |
| `--prompt-file path` | Read prompt from a file |
| `--stdin` | Read prompt from stdin |
| `--auto-confirm` | Automatically approve mutating tool calls |
| `--mode plan\|default\|auto` | Override execution mode for this run |
| `--model name` | Override the model name |
| `--provider name` | Override the provider |
| `--skill name` | Activate a named skill for this run |
| `--max-turns n` | Override the maximum agent loop iterations |
| `--output path` | Write structured output to a file |
| `--output-format text\|json\|jsonl` | Choose output format (default: text) |
| `--no-session` | Run without persisting any session state |
| `--no-plugins` | Skip plugin loading |
| `--no-skills` | Skip skill loading |
| `--deny-mutating` | Equivalent to --mode plan (deny all mutating tools) |
| `--quiet` | Suppress tool call output in headless mode |

---

## Slash Commands — Full Reference

All slash commands are available inside the interactive REPL. Type `/help` to see a summary table. Every command also accepts `help` as a subcommand to list its subcommands with examples:

```
/context help
/mode help
/provider help
/config help
/session help
/skills help
/memory help
/tools help
/history help
/delegate help
/mcp help
```

Unrecognised slash commands (e.g. `/anything`) are forwarded to the agent as a natural-language query — you do not need to remove the leading `/`.

### /provider

Show and update model provider settings in the running session.

```
/provider                        — show current provider, model, temperature, etc.
/provider list                   — list all available providers (fake, mistral, openai, openai-compatible)
/provider set <param> <value>    — update a provider parameter and hot-reload config
/provider help                   — list all subcommands with examples
```

Settable parameters via `/provider set`:

| Parameter | Example | Purpose |
|---|---|---|
| `provider` | `mistral` | Switch provider |
| `model_name` | `mistral-medium-latest` | Change model identifier |
| `api_base_url` | `https://api.mistral.ai/v1` | Set provider endpoint |
| `temperature` | `0.7` | Sampling temperature (0.0–2.0) |
| `max_output_tokens` | `8192` | Max tokens per response |
| `max_loop_iterations` | `12` | Max agent turns per query |
| `stream_output` | `true` | Enable/disable streaming |
| `show_tool_calls` | `false` | Show or hide tool output |

Example: switch to Mistral in the current session without restarting:

```
/provider set provider mistral
/provider set model_name mistral-large-latest
```

### /mode

Show or switch the execution permission mode.

```
/mode              — show current mode
/mode plan         — deny all mutating tools (read-only)
/mode default      — require confirmation for mutating tools
/mode auto         — allow all tools without confirmation
/mode help         — list all subcommands with examples
```

### /config

Show, edit, or reload config.

```
/config                        — show merged config as JSON
/config show merged            — same as above
/config show local             — show .nexus/config.toml contents
/config show global            — show ~/.nexus/config.toml contents
/config set <key> <value>      — write a key to .nexus/config.toml and reload
/config reset <key>            — remove a key from .nexus/config.toml and reload
/config reload                 — reload config from disk without restarting
/config reinit                 — rewrite .nexus/config.toml to clean Nexus defaults
                                 (clears provider/model overrides; keeps sessions and memory)
/config reinit global          — rewrite ~/.nexus/config.toml to clean Nexus defaults
/config help                   — list all subcommands with examples
```

Example: enable tool call display:

```
/config set show_tool_calls true
```

### /skills

```
/skills                 — list all loaded skills with active status
/skills list            — same as above
/skills show <name>     — print the full content of a skill
/skills add <name>      — activate a skill for this session
/skills remove <name>   — deactivate a skill
/skills reload          — rescan skill directories and reload the registry
/skills help            — list all subcommands with examples
```

### /tools

```
/tools        — list all registered tools with name, source, origin, mutating flag, and description
/tools help   — show this help
```

### /session

```
/session                        — show current session ID and message count
/session new                    — start a fresh session (saves current first)
/session list                   — list all saved sessions
/session resume <session_id>    — load a previous session into context
/session save                   — save the current session to disk
/session export <path>          — export session messages as JSON to a file
/session help                   — list all subcommands with examples
```

### /memory

```
/memory list                        — list all workspace memory entry keys
/memory search <query>              — search memory entries by content
/memory save <key> <content>        — save a new memory entry
/memory show <key>                  — show the content of a memory entry
/memory help                        — list all subcommands with examples
```

### /context

```
/context             — print the current assembled system prompt (default: same as /context show)
/context show        — print the current assembled system prompt
/context usage       — show a context usage table: provider, model, context window size,
                       estimated token counts for system prompt and history, compaction thresholds,
                       and percentage of context window consumed
/context help        — list all subcommands with examples
```

Example output of `/context usage`:

```
╔══════════════════════════════════════════════════╗
║                  Context Usage                   ║
╠══════════════════════╦═══════════════════════════╣
║ Field                ║ Value                     ║
╠══════════════════════╬═══════════════════════════╣
║ Provider             ║ mistral                   ║
║ Model                ║ mistral-medium-latest     ║
║ Context window       ║ 32,768 tokens             ║
║ System prompt (est.) ║ 420 tokens                ║
║ History (est.)       ║ 310 tokens                ║
║ Total used (est.)    ║ 730 tokens  (2.2%)        ║
║ Compaction soft limit║ 21,299 tokens  (65.0%)    ║
║ Compaction hard limit║ 27,852 tokens  (85.0%)    ║
╚══════════════════════╩═══════════════════════════╝
```

Token counts are estimates (characters ÷ 4). Compaction fires automatically when history exceeds the soft limit.

### /history

```
/history         — show all messages in the current session
/history <n>     — show the last n messages
/history help    — list all subcommands with examples
```

### /mcp

```
/mcp status                  — show all configured MCP servers and connection state
/mcp tools                   — list registered and discovered tools per server
/mcp refresh                 — refresh all MCP servers
/mcp refresh <server_name>   — refresh a specific MCP server
/mcp help                    — list all subcommands with examples
```

### /delegate

```
/delegate status                                                   — show coordination summary
/delegate workers                                                  — list worker states
/delegate tasks                                                    — list all tasks
/delegate tasks active                                             — list only active tasks
/delegate spawn "Title" "Instructions" [options]                   — submit a delegated task
/delegate messages [participant] [limit]                           — inspect the mailbox
/delegate approvals                                                — list pending permission decisions
/delegate approve <decision_id>                                    — approve a pending worker action
/delegate reject <decision_id>                                     — reject a pending worker action
/delegate help                                                     — list all subcommands with examples
```

Spawn options: `--worker <id>`, `--tool <name>` (repeatable), `--resource <name>` (repeatable), `--permission-action <tool>`, `--permission-reason <text>`.

### /help

```
/help    — show a summary table of all slash commands
```

### /quit or /exit

```
/quit    — save session and exit the REPL
/exit    — same as /quit
```

---

## Config Fields — Full Reference

Config is loaded from six layers (later layers override earlier ones):

1. Built-in defaults (`nexus/config/defaults.py`)
2. Global config — `~/.nexus/config.toml`
3. Local config — `.nexus/config.toml`
4. `.env` file — parsed from the workspace root at startup; keys are injected into the process environment before any env-var lookup. `.env` values take priority over the system environment for the same keys.
5. Environment variables — `AGENT_<FIELD_NAME_UPPER>` (e.g. `AGENT_PROVIDER`)
6. CLI overrides — `--model`, `--mode`, `--provider`, etc.

**Provider fields:**

| Key | Default | Description |
|---|---|---|
| `provider` | `mistral` | `fake`, `mistral`, `openai`, `openai-compatible` |
| `model_name` | `mistral-medium-latest` | Model identifier sent to the provider |
| `api_base_url` | `https://api.mistral.ai/v1` | Provider base URL (auto-set for mistral; overridden by `MISTRAL_BASE_URL` env var) |
| `api_key` | `""` | API key — prefer `.env` file (`MISTRAL_API_KEY=sk-...`) or system env |
| `temperature` | `0.0` | Sampling temperature 0.0–2.0 |
| `max_output_tokens` | `4096` | Max tokens per model response |

**Execution fields:**

| Key | Default | Description |
|---|---|---|
| `default_mode` | `default` | `plan`, `default`, or `auto` |
| `auto_confirm_read_only` | `true` | Skip confirmation for read-only tools |
| `max_loop_iterations` | `8` | Max agent turns per query |
| `stream_output` | `true` | Stream tokens to console |
| `show_tool_calls` | `true` | Show tool call output |
| `show_thinking_indicator` | `true` | Show thinking indicator |
| `color_output` | `true` | Enable color in REPL output |

**Context compaction:**

| Key | Default | Description |
|---|---|---|
| `compaction_soft_limit` | auto (65% of model context) | Token count that triggers compaction. Auto-set from model's known context window at startup. |
| `compaction_hard_limit` | auto (85% of model context) | Hard cut after compaction. Auto-set from model's known context window at startup. |
| `compaction_keep_recent` | `12` | Minimum recent messages to keep |

To see the current compaction thresholds and token usage for your model, run `/context usage`.

**Session fields:**

| Key | Default | Description |
|---|---|---|
| `max_sessions_retained` | `50` | Sessions to keep before pruning |
| `save_on_every_turn` | `true` | Persist session after each turn |

**Tool filtering:**

| Key | Default | Description |
|---|---|---|
| `allowed_tools` | `[]` | If non-empty, only these tools are allowed |
| `denied_tools` | `[]` | These tools are never registered |
| `write_note_max_bytes` | `65536` | Maximum content size for write_note |

**Logging and observability:**

| Key | Default | Description |
|---|---|---|
| `log_level` | `INFO` | Python logging level |
| `log_format` | `text` | `text` or `json` (json enables JSONL + metrics) |

**MCP servers:**

```toml
mcp_servers = [
    { name = "filesystem", command = ["uvx", "mcp-server-filesystem", "."], prefix = "fs_" }
]
```

**Delegation:**

| Key | Default | Description |
|---|---|---|
| `delegation_enabled` | `false` | Enable coordinator-worker delegation |
| `delegation_workers` | `["worker-1","worker-2"]` | Worker IDs |
| `delegation_poll_interval_seconds` | `0.05` | Coordinator poll frequency |
| `delegation_message_history_limit` | `200` | Mailbox history depth |

**Sandbox (Docker):**

| Key | Default | Description |
|---|---|---|
| `sandbox_commands` | `false` | Enable the sandboxed run_command tool |
| `sandbox_image` | `nexus-sandbox:latest` | Docker image to use |
| `sandbox_timeout_seconds` | `30` | Execution timeout |
| `sandbox_memory_limit` | `256m` | Container memory cap |
| `sandbox_network` | `none` | Container network mode |
| `sandbox_read_only_workspace` | `true` | Mount workspace read-only |
| `sandbox_tmp_size` | `64m` | Writable tmpfs size |

---

## How to Change Config

### Change a setting in the running session (no restart needed)

```
/config set show_tool_calls false
/config set temperature 0.5
/config set max_loop_iterations 15
```

### Change provider without restarting

```
/provider set provider mistral
/provider set model_name mistral-large-latest
```

### Reload after manually editing .nexus/config.toml

```
/config reload
```

### Set a value permanently in local config

Edit `.nexus/config.toml` directly, or use `/config set`. The next session picks it up automatically.

### Override for one headless run only

```bash
uv run nexus --provider fake --mode auto --prompt "test"
```

---

## Provider Setup

### Mistral (default)

Nexus defaults to Mistral. Provide your API key in one of these ways (checked in order):

1. `.env` file in your workspace root: `MISTRAL_API_KEY=your_key_here`
2. System environment: `export MISTRAL_API_KEY=your_key_here`
3. `NEXUS_API_KEY` environment variable (fallback)

Base URL defaults to `https://api.mistral.ai/v1`. Override with:
- `MISTRAL_BASE_URL` environment variable
- `api_base_url` in `.nexus/config.toml`

Example `.env` file:

```
MISTRAL_API_KEY=sk-...
```

### Fake provider (offline, no API key)

```bash
uv run nexus --provider fake
```

Or in `.nexus/config.toml`:

```toml
provider = "fake"
model_name = "fake-model"
```

Useful for testing, CI, and offline development.

### OpenAI-compatible (Ollama, vLLM, etc.)

```toml
provider = "openai-compatible"
model_name = "llama3"
api_base_url = "http://localhost:11434/v1"
```

Or via CLI:

```bash
uv run nexus --provider openai-compatible --model llama3 --prompt "hello"
```

Set `AGENT_API_BASE_URL` as an environment variable if you prefer not to edit the TOML.

---

## Builtin Tools

| Tool | Mutating | Description |
|---|---|---|
| `get_time` | No | Returns current UTC timestamp |
| `write_note` | Yes | Writes a file inside the workspace. Hard-denies paths outside workspace or under `.nexus/`. Enforces `write_note_max_bytes` limit. |

In DEFAULT mode, `write_note` requires explicit confirmation before writing.

---

## Skills

Skills are Markdown files (`SKILL.md`) that inject additional instructions into the model's system prompt for the current session.

Skills are loaded from three sources in priority order (later source wins on name collision):

1. Built-in package skills — `nexus/builtin_skills/<name>/SKILL.md` (shipped with the package)
2. Global user skills — `~/.nexus/skills/<name>/SKILL.md`
3. Local workspace skills — `.nexus/skills/<name>/SKILL.md`

Built-in skills (always discoverable, auto-activated at startup):
- `nexus-agent` — this skill; answers natural-language questions about Nexus commands, config, providers, and all features. Auto-activated in every REPL session. Suppressed by `--no-skills`.

Activate a skill for a session:

```
/skills add <name>
```

Activate at startup:

```bash
uv run nexus --skill my-skill
```

Deactivate:

```
/skills remove <name>
```

Create a custom skill:

```bash
mkdir -p .nexus/skills/my-skill
cat > .nexus/skills/my-skill/SKILL.md <<'EOF'
# My Custom Skill

Instructions for the model that apply when this skill is active.
EOF
```

Then inside the REPL:

```
/skills reload
/skills add my-skill
```

---

## Session And Memory

Sessions are persisted as JSON under `.nexus/sessions/`. The latest session ID is in `.nexus/sessions/latest_session.txt`.

To resume a previous session:

```
/session list
/session resume <session_id>
```

Memory entries are Markdown files under `.nexus/memory/`. Save and retrieve them:

```
/memory save key Some content here
/memory show key
/memory search keyword
```

Workspace knowledge is summarized in `.nexus/knowledge.md` and updated after each session.

---

## Observability

Enable structured JSON observability in `.nexus/config.toml`:

```toml
log_format = "json"
```

This writes:
- `~/.nexus/logs/runtime.jsonl` — per-event JSONL log with payload redaction
- `~/.nexus/logs/metrics.json` — aggregated counters by session and tool
- `.nexus/audit-trail.jsonl` — mutating-action audit log (always written regardless of log_format)

Run a health check:

```bash
uv run nexus doctor --output-format json
```

The doctor report covers four gates: Runtime Integrity, Safety Integrity, Operational Integrity, Extension Integrity.

---

## MCP Servers

Add MCP servers to `.nexus/config.toml`:

```toml
mcp_servers = [
    { name = "filesystem", command = ["uvx", "mcp-server-filesystem", "."], prefix = "fs_" }
]
allowed_tools = ["get_time", "write_note", "fs_read_file"]
```

Inspect MCP status in the REPL:

```
/mcp status
/mcp tools
/mcp refresh filesystem
```

---

## Delegation (Multi-Agent)

Enable in `.nexus/config.toml`:

```toml
delegation_enabled = true
delegation_workers = ["worker-1", "worker-2"]
```

Then from the REPL:

```
/delegate spawn "Review README" "Summarize key sections." --worker worker-1 --resource README.md
/delegate tasks
/delegate approvals
/delegate approve <decision_id>
```

---

## Docker Sandbox

Build the image once:

```bash
docker build -f nexus/Dockerfile.sandbox -t nexus-sandbox:latest .
```

Enable in `.nexus/config.toml`:

```toml
sandbox_commands = true
allowed_tools = ["get_time", "write_note", "run_command"]
```

The `run_command` tool runs inside Docker with network disabled, memory capped, and the workspace mounted.

---

## Frequently Asked Questions

**Q: How do I switch from fake to Mistral?**
A: Add `MISTRAL_API_KEY=your_key` to a `.env` file in your workspace root. Then run `/provider set provider mistral` in the REPL, or set `provider = "mistral"` in `.nexus/config.toml`.

**Q: How do I reset my config to defaults without losing sessions or memory?**
A: Type `/config reinit` (local config) or `/config reinit global` (global config). This rewrites the target file to clean Nexus defaults — provider set to `mistral`, standard settings — without touching `.nexus/sessions/`, `.nexus/memory/`, or `.nexus/knowledge.md`. Reload takes effect immediately.

**Q: How do I see what config is currently active?**
A: Type `/config` or `/config show merged` in the REPL. For a specific scope: `/config show local` or `/config show global`.

**Q: How do I see context window and token usage?**
A: Type `/context usage`. It shows the model's known context window, estimated token counts for your system prompt and history, compaction thresholds (65%/85% of context window by default), and total usage as a percentage.

**Q: What are the compaction limits and how are they set?**
A: At startup, Nexus looks up the active model in its built-in model limits table (`nexus/config/model_limits.py`, covers 40+ models) and sets the soft limit to 65% and hard limit to 85% of the model's context window. These defaults are only applied when neither limit has been explicitly overridden by the user. You can always override them in `.nexus/config.toml` or check the current values with `/context usage`.

**Q: How do I get help for a specific slash command?**
A: Every slash command accepts `help` as a subcommand. Example: `/context help`, `/provider help`, `/skills help`. A table of subcommands and examples is printed.

**Q: How do I change the model without restarting?**
A: Use `/provider set model_name mistral-large-latest` in the REPL.

**Q: How do I stop Nexus from asking for confirmation on every write?**
A: Use `/mode auto` in the REPL for the current session, or set `default_mode = "auto"` in `.nexus/config.toml` for all sessions. Be careful — auto mode skips all confirmation prompts.

**Q: How do I add a new tool?**
A: For a simple tool, create a plugin under `~/.nexus/plugins/my_plugin.py` that exposes a `register(registry, hooks)` function. For external services, configure an MCP server.

**Q: How do I see what tools are available?**
A: Type `/tools` in the REPL.

**Q: How do I see my session history?**
A: Type `/history` or `/history 10` for the last 10 messages.

**Q: How do I export my session?**
A: Type `/session export /tmp/my-session.json` in the REPL.

**Q: How do I run a production readiness check?**
A: Run `uv run nexus doctor --output-format json` from your terminal.

**Q: What does the audit trail contain?**
A: Every mutating tool call (and every denied or confirmation-requested action) is logged to `.nexus/audit-trail.jsonl` with action name, target path (where applicable), danger level, and rollback notes.

**Q: How do I use Nexus in CI without confirmation prompts?**
A: Use `--auto-confirm` flag: `uv run nexus --prompt "..." --auto-confirm --output-format json`.

**Q: How do I create a custom skill?**
A: Create `.nexus/skills/my-skill/SKILL.md`, then in the REPL run `/skills reload` and `/skills add my-skill`.
