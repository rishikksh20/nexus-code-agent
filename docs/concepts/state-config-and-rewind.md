**Big Picture**
Vibe separates persistence into two worlds:

| Area | Purpose | Default Location |
| --- | --- | --- |
| Global user home | User config, credentials, logs, history, trusted folders, user tools/skills/agents/prompts | `~/.vibe` or `$VIBE_HOME` |
| Local project config | Repo-specific config, tools, skills, agents, prompts, project instructions | `<repo>/.vibe` and checked-in `AGENTS.md` |
| Session storage | Conversation/message persistence for resume/debug | `~/.vibe/logs/session` by default |
| Rewind checkpoints | In-memory file snapshots for current running session | memory only |

The important thing: **session logs persist across runs; rewind checkpoints do not.**

**VIBE_HOME**
`VIBE_HOME` is resolved in [vibe/core/paths/_vibe_home.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/paths/_vibe_home.py:14).

Default:

```text
~/.vibe
```

Override:

```bash
VIBE_HOME=/some/path vibe
```

Global paths:

```text
$VIBE_HOME/
  config.toml
  .env
  trusted_folders.toml
  vibehistory
  cache.toml
  plans/
  tools/
  skills/
  agents/
  prompts/
  logs/
    vibe.log
    session/
```

Code defines:

```python
GLOBAL_ENV_FILE = VIBE_HOME / ".env"
SESSION_LOG_DIR = VIBE_HOME / "logs" / "session"
TRUSTED_FOLDERS_FILE = VIBE_HOME / "trusted_folders.toml"
LOG_FILE = VIBE_HOME / "logs" / "vibe.log"
HISTORY_FILE = VIBE_HOME / "vibehistory"
PLANS_DIR = VIBE_HOME / "plans"
```

**Global Vs Local Config**
Project-local config is loaded only if the working directory is trusted.

Local project paths:

```text
<repo>/.vibe/config.toml
<repo>/.vibe/hooks.toml
<repo>/.vibe/tools/
<repo>/.vibe/skills/
<repo>/.vibe/agents/
<repo>/.vibe/prompts/
<repo>/.agents/skills/
<repo>/AGENTS.md
```

Trust is stored globally in:

```text
$VIBE_HOME/trusted_folders.toml
```

`TrustedFoldersManager` stores:

```toml
trusted = [...]
untrusted = [...]
```

There is also session-only trust from `--trust`, which is not persisted.

The loader checks project trust in [harness manager](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/config/harness_files/_harness_manager.py:26). If project source is enabled and cwd is trusted, local `.vibe` is considered. If not, project-local behavior files are ignored.

**Config File Selection**
For main `VibeConfig`, the TOML source is selected like this:

1. If trusted project has `<repo>/.vibe/config.toml`, use it.
2. Else use `$VIBE_HOME/config.toml`.
3. Else use defaults/env/init overrides.

This happens in `TomlFileSettingsSource._load_toml()` via `get_harness_files_manager().config_file`.

Important nuance: for `config.toml`, this code chooses **one TOML file**, not a deep merge of global + project config. The effective priority is:

```text
constructor overrides
VIBE_* environment variables
selected TOML file, project if trusted and present, otherwise user
file secrets
defaults
```

`.env` is different: `$VIBE_HOME/.env` is loaded manually into `os.environ` for API keys and credentials. It is intentionally not treated as normal Pydantic config.

**What Is Saved To Config**
`VibeConfig.save_updates()` writes updates back to the active writable config target.

Target:

```python
target = mgr.config_file or mgr.user_config_file
```

So if a trusted project config file is active, updates can go to project `.vibe/config.toml`; otherwise they go to `$VIBE_HOME/config.toml`.

Examples of persisted config changes:

- model thinking level
- active model
- permanent tool permission/allowlist updates
- user config bootstrap defaults

API keys normally live in `$VIBE_HOME/.env`, not `config.toml`.

**Tools, Skills, Agents, Prompts**
These are discovered from both global and trusted local sources.

Global:

```text
$VIBE_HOME/tools/
$VIBE_HOME/skills/
$VIBE_HOME/agents/
$VIBE_HOME/prompts/
```

Trusted local:

```text
<repo>/.vibe/tools/
<repo>/.vibe/skills/
<repo>/.vibe/agents/
<repo>/.vibe/prompts/
<repo>/.agents/skills/
```

Discovery behavior:

- tools: builtin tools + config `tool_paths` + project dirs + user dirs
- skills: builtin skills + config `skill_paths` + project dirs + user dirs
- agents: builtin agents + config `agent_paths` + project dirs + user dirs
- prompts: project prompts first, then user prompts, then builtin prompts

Project discovery walks local `.vibe` and `.agents` directories up to bounded depth, so Vibe does not scan the whole world forever.

**Session Storage**
Session logging is handled by [SessionLogger](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/session/session_logger.py:27).

Default config:

```toml
[session_logging]
enabled = true
save_dir = ""       # becomes ~/.vibe/logs/session
session_prefix = "session"
```

Each session directory is named like:

```text
session_YYYYMMDD_HHMMSS_<short-session-id>/
```

Inside:

```text
messages.jsonl
meta.json
```

`messages.jsonl` is append-only. It stores non-system messages:

```text
user
assistant
tool
```

System messages are not appended as normal conversation lines.

`meta.json` stores:

- session id
- parent session id
- start/end time
- git commit
- git branch
- working directory
- username
- title
- stats
- total message count
- available tool schemas
- config snapshot
- active agent profile
- system prompt snapshot
- scheduled loops

Messages are appended incrementally. The logger reads `meta.json["total_messages"]`, then appends only new non-system messages to `messages.jsonl`.

Metadata is written atomically through a temporary `.json.tmp` file and `os.replace`.

**Resume Retrieval**
Resume uses [SessionLoader](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/session/session_loader.py:20).

For `--continue`, it finds the latest valid session for the current working directory:

```text
session_logging.save_dir/session_prefix_*
```

Then it filters by:

```text
meta.json environment.working_directory == current cwd
```

For `--resume SESSION_ID`, it matches by shortened session id.

Loading does:

1. Read `messages.jsonl`.
2. Parse each line as JSON.
3. Convert each to `LLMMessage`.
4. Skip any system messages.
5. Read `meta.json`.
6. Create a fresh `AgentLoop`.
7. Add loaded non-system messages to the new loop.
8. Point `SessionLogger` back at the existing session directory.

Important: on resume, the old system prompt is not blindly reused as active context. Vibe creates a fresh system prompt from current config/tools/skills/project instructions, then appends historical non-system messages.

So resume means:

```text
current system prompt
+ old user/assistant/tool messages from messages.jsonl
```

**Compaction Storage**
Compaction first saves the full current session, then creates a new child session.

Flow:

1. Save current full messages to current session directory.
2. Ask compaction model for summary.
3. Reset active messages to:

```text
system message
summary-as-user-message
```

4. Generate a new session id, preserving the old suffix.
5. Set old session id as `parent_session_id`.
6. Create a new session directory.
7. Save compacted messages and metadata there.

So old detailed logs are still in the old session folder, and the compacted working context continues in a new session folder linked by parent id.

**Fork Storage**
Fork creates a new `AgentLoop` with selected historical messages.

It:

- copies base config
- preserves the active agent name
- creates a new session id with same suffix
- sets parent session id to the source session
- saves forked messages into a new session directory

Fork does not mutate the original session.

**Subagent Storage**
Subagents created by the `task` tool get their own logs under the parent session:

```text
<parent-session-dir>/agents/<agent-name>_<timestamp>_<short-session-id>/
  messages.jsonl
  meta.json
```

The parent session only gets the final `TaskResult` as a tool result. The detailed subagent conversation is separate.

**Rewind Checkpoints**
Rewind is handled by [RewindManager](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/rewind/manager.py:27).

This is important: **rewind checkpoints are in-memory only.**

Before every user turn, Vibe creates a checkpoint:

```python
Checkpoint(message_index=len(messages), files=[...])
```

Editing tools can provide file snapshots before mutation:

- `write_file.get_file_snapshot()`
- `search_replace.get_file_snapshot()`

A `FileSnapshot` stores:

```python
path: str
content: bytes | None
```

`content = None` means the file did not exist before.

When rewind restores files:

- if snapshot content is bytes, write those bytes back
- if content is `None`, delete the file if it now exists

Rewind also truncates message history back to the selected user message and resets the session id.

But these file snapshots are not persisted in `messages.jsonl` or `meta.json`. If you quit and resume later, the old session messages are available, but rewind file checkpoints are gone.

**What Is Persisted Vs Not Persisted**
Persisted:

- global config
- selected project config, if active/trusted
- trusted folder decisions
- user `.env`
- user history
- logs
- plans
- session messages
- session metadata
- subagent session logs
- compacted session parent links
- custom tools/skills/agents/prompts as files

Not persisted:

- in-memory rewind file snapshots
- active scratchpad registry
- active middleware internal state
- current tool manager instances
- current MCP/connector runtime cache after process exit
- active approval session rules, unless saved permanently to config allowlists

**Scratchpad**
Scratchpad is temporary and session-scoped:

```text
/tmp/vibe-scratchpad-<short-session-id>-...
```

It is not under `VIBE_HOME` by default and is not treated as durable storage. It is shared with subagents by prompt text. File tools auto-allow scratchpad paths, but the scratchpad itself is not the same as session logs.

**Local Project Instructions**
`AGENTS.md` is loaded into the system prompt, not into config.

Sources:

- `$VIBE_HOME/AGENTS.md`
- trusted project `AGENTS.md` files from trust root to cwd
- subdirectory `AGENTS.md` files can be lazily injected when `read_file` reads files deeper in the tree

Project `AGENTS.md` loading depends on trust.

**Mental Model**
Think of Vibe storage like this:

```text
VIBE_HOME
  = user memory and durable app state

trusted .vibe/
  = repo-specific behavior overrides

session logs
  = durable transcript and metadata for resume/debug

rewind checkpoints
  = temporary in-memory undo snapshots for current live session

scratchpad
  = temporary working files, not durable memory

filesystem
  = source of truth for current code
```

The cleanest summary:

```text
Config decides behavior.
Session logs preserve conversation.
Rewind snapshots support live undo.
Trusted folders decide whether local project config is allowed.
VIBE_HOME is global user storage.
.vibe is local project storage, only when trusted.
```
