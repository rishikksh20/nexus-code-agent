# YAML Sub-Agents

Nexus lets you define cognitive sub-agents as individual YAML files — one file per agent — without touching `config.toml`. This is the recommended way to build personal or team-specific agent personas that live alongside the codebase.

---

## Overview

A YAML sub-agent is a `.yml` file that describes a focused cognitive persona: its name, purpose, goal prompt, allowed tools, allowed skills, allowed MCP servers, and resource limits. Nexus discovers these files automatically at startup and during live reloads, then registers each one as a normal tool (`subagent_<name>`) that the supervisor model can call.

---

## Discovery Roots

Nexus scans two directories, in this order:

| Scope | Path | Notes |
|---|---|---|
| **Global** | `~/.nexus/agents/*.yml` | Available in every workspace for the current user |
| **Local** | `.nexus/agents/*.yml` | Workspace-specific; overrides a global agent of the same name |

Neither directory is created by `nexus init`. Nexus creates them on demand when you scaffold a new file via `/sub-agent agents new` or create one manually.

---

## File Format

Each file must be named after the agent (e.g. `explore.yml` → name `explore`) and contain valid YAML with the following fields:

```yaml
name: explore                      # required — must match the file stem
description: "..."                 # required — one-sentence summary shown to the LLM
goal_prompt: |                     # required — system prompt injected into the sub-agent
  Your full instructions here.
  Can span multiple lines.
allowed_tools:                     # optional — list of tool names the sub-agent may use
  - read_file                      #   omit or leave empty to allow all active workspace tools
  - glob
  - grep
  - list_dir
  - lsp
allowed_skills:                    # optional — omit or leave empty to allow all active skills
  - python-code-review
allowed_mcps:                      # optional — omit or leave empty to allow all active MCP servers
  - filesystem
max_turns: 12                      # optional — default 20
timeout_seconds: 300               # optional — default 600.0
```

### Field Reference

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `name` | string | yes | — | Lowercase letters, digits, hyphens, underscores. Must match the file stem. |
| `description` | string | yes | — | One sentence; shown in the tool schema so the LLM can choose the right agent. |
| `goal_prompt` | string | yes | — | Full system prompt for the sub-agent. Use YAML block scalar (`|`) for multi-line text. |
| `allowed_tools` | list of strings | no | `null` (all active tools) | Allowlist of normal tool names. Omit the key or leave it empty to allow all active workspace tools. |
| `allowed_skills` | list of strings | no | `null` (all active skills) | Allowlist of active skill names to expose as sub-agent metadata. Omit the key or leave it empty to allow all active skills. |
| `allowed_mcps` | list of strings | no | `null` (all active MCP servers) | Allowlist of MCP server names whose tools may be used. Omit the key or leave it empty to allow all active MCP servers. |
| `max_turns` | positive integer | no | `20` | Maximum agent loop iterations before the sub-agent stops and returns. |
| `timeout_seconds` | positive number | no | `600.0` | Hard wall-clock timeout in seconds. |

### Naming Rules

- Must be lowercase letters (`a-z`), digits (`0-9`), hyphens (`-`), or underscores (`_`).
- Must not start or end with a hyphen or underscore.
- The file name (without `.yml`) must exactly match the `name` field.
- The resulting tool name is `subagent_<name>` (hyphens replaced by underscores).

---

## Activation

YAML sub-agents are loaded when either of these is true:

- `agent_mode = "advanced"` in `.nexus/config.toml`
- The normalized agent name appears in `delegation_subagents` (as a config entry)

When neither condition is met, the agents are discovered but not registered as tools.

To enable all sub-agents (built-in and YAML) in a workspace:

```toml
# .nexus/config.toml
agent_mode = "advanced"
```

---

## Priority / Override Order

Definitions are merged in this order — later entries win on name collision:

1. **Built-in cognitive personas** (`planning_analysis`, `execution`, `review`, `verification`)
2. **`delegation_subagents`** entries from `config.toml`
3. **Global YAML** files (`~/.nexus/agents/*.yml`)
4. **Local YAML** files (`.nexus/agents/*.yml`) — highest priority

This means a local YAML file can silently override a global agent or even a built-in persona if it uses the same name. Use a unique name to avoid unintended overrides.

---

## REPL Commands

All YAML agent management happens under `/sub-agent agents`:

```
/sub-agent agents list
```
Print a table of all discovered YAML agent files: name, scope (local/global), description, allowed tools, allowed skills, allowed MCP servers, and max turns.

```
/sub-agent agents new <name>
/sub-agent agents new <name> global
```
Scaffold a ready-to-edit `.yml` file at `.nexus/agents/<name>.yml` (default) or `~/.nexus/agents/<name>.yml`. The file is created with a starter template; edit it, then run `reload`.

```
/sub-agent agents reload
```
Re-scan both agent directories and rebuild YAML sub-agent tools in the live tool registry without restarting. Existing YAML-backed tools are replaced so edits to `description`, `goal_prompt`, resource allowlists, or limits take effect immediately. Equivalent to the YAML portion of `/tools reload`.

```
/sub-agent agents promote <name>
```
Move `.nexus/agents/<name>.yml` → `~/.nexus/agents/<name>.yml` (local → global). Fails if a global file with the same name already exists. Triggers a reload automatically.

```
/sub-agent agents demote <name>
```
Move `~/.nexus/agents/<name>.yml` → `.nexus/agents/<name>.yml` (global → local). Fails if a local file with the same name already exists. Triggers a reload automatically.

### Related commands

```
/sub-agent list              — list all registered subagent_* tools (built-in + YAML)
/sub-agent show <name>       — show one sub-agent's config and effective resources
/sub-agent tools <name>      — list effective tool names for one sub-agent
/tools reload                — full tool registry rebuild (includes YAML agents)
/tools                       — verify which subagent_* tools are active
```

---

## Workflow Example

### 1. Scaffold a local agent

```
> /sub-agent agents new summarizer
Created local sub-agent file: /your/workspace/.nexus/agents/summarizer.yml
Edit the file, then run: /sub-agent agents reload
```

### 2. Edit the file

```yaml
# .nexus/agents/summarizer.yml
name: summarizer
description: Summarize a codebase module or file in plain language.
goal_prompt: |
  You are a documentation summarizer. Read the requested file or module,
  then produce a short plain-language summary: purpose, key exports,
  and any notable design decisions. Do not modify files.
allowed_tools:
  - read_file
  - glob
  - grep
  - list_dir
allowed_skills: []
allowed_mcps: []
max_turns: 8
timeout_seconds: 120
```

### 3. Load it live

```
> /sub-agent agents reload
Registered 1 new YAML sub-agent tool(s). Run /tools to verify.
```

### 4. Verify

```
> /tools
...
subagent_summarizer    agent    agent-yaml    summarizer    no    Summarize a codebase module or file in plain language.
```

### 5. Use it

The supervisor can now call `subagent_summarizer` autonomously. You can also inspect it:

```
> /sub-agent show summarizer
```

### 6. Promote to global (share across workspaces)

```
> /sub-agent agents promote summarizer
Promoted 'summarizer' to global: /Users/you/.nexus/agents/summarizer.yml
```

---

## Common Patterns

### Read-only analyst

```yaml
name: analyst
description: Analyze code structure, patterns, and architecture without modifying files.
goal_prompt: |
  You are a read-only code analyst. Investigate the requested codebase area,
  trace call graphs and data flows, and return a structured analysis.
  Never write, edit, or delete files.
allowed_tools:
  - read_file
  - glob
  - grep
  - list_dir
  - lsp
max_turns: 15
timeout_seconds: 300
```

### Test writer

```yaml
name: test-writer
description: Generate pytest tests for a given module or function.
goal_prompt: |
  You are a test generation specialist. Read the target module, understand
  its contracts, then write comprehensive pytest tests covering happy paths,
  edge cases, and error handling. Follow existing test style in the tests/ dir.
allowed_tools:
  - read_file
  - write_file
  - glob
  - grep
  - list_dir
  - lsp
  - run_tests
max_turns: 20
timeout_seconds: 600
```

### Database migration helper

```yaml
name: db-migrate
description: Draft and validate database migration scripts for schema changes.
goal_prompt: |
  You are a database migration assistant. Review the requested schema change,
  produce an Alembic migration script, and run the linter. Do not apply the
  migration or modify production data.
allowed_tools:
  - read_file
  - write_file
  - glob
  - bash
  - run_python_check
max_turns: 10
timeout_seconds: 180
```

---

## Troubleshooting

**Agent not appearing after reload**

- Check that `agent_mode = "advanced"` is set in `.nexus/config.toml`, or that the agent name is in `delegation_subagents`.
- Verify the file name matches the `name` field exactly: `explore.yml` → `name: explore`.
- If `allowed_tools` is a non-empty list in `config.toml`, add `subagent_<name>` to it.

**Parse error on reload**

Run `/sub-agent agents list` — invalid files show an error column with the validation message. Common causes:

- File stem doesn't match `name` field
- `name` contains uppercase letters or special characters
- Missing required fields (`name`, `description`, `goal_prompt`)
- `max_turns` or `timeout_seconds` is not a positive number

**Promote/demote fails with FileExistsError**

An agent with the same name already exists at the destination. Rename or remove the existing file before moving.

**YAML agent overrides a built-in**

If your agent uses the same name as a built-in (`planning_analysis`, `execution`, `review`, `verification`), the YAML definition wins. Choose a different name or intentionally override the built-in's goal prompt this way.
