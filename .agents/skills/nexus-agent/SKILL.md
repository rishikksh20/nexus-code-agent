---
name: nexus-agent
description: "Use for Nexus self-help: answer questions about slash commands, config, providers, tools, skills, sessions, memory, MCP, sub-agents, sandboxing, and runtime behavior; recover from unknown or mistyped commands by suggesting the closest valid command and a short fix."
license: MIT
metadata:
  bundled: "true"
  purpose: "interactive-help"
---

# Nexus Agent Self-Help

Use this skill when the user asks how Nexus works, asks what command to run,
enters a wrong or unknown slash command that reaches the agent as natural
language, or needs help with configuration, tools, skills, MCP, sessions,
memory, providers, sandboxing, or cognitive sub-agents.

## First Response Pattern

When the user entered a likely command typo, give the closest valid command
first, then one sentence of context. Keep the answer short and practical.

Examples:

- `/skils list` -> suggest `/skills list`
- `/tool` -> suggest `/tools`
- `/models` -> suggest `/provider`
- `/ctx usage` -> suggest `/context usage`
- `/mem save note` -> suggest `/memory save <key> <content>`

If there are multiple plausible commands, list two or three likely choices and
tell the user to run `/help` for the full command list.

## Core Facts

- Nexus is a CLI-first Python agent harness with interactive REPL and headless prompt modes.
- Unknown slash commands are forwarded to the agent as natural-language input so the user can get a helpful correction.
- Slash-command help is available from `/help` and from each command's `help` subcommand, such as `/context help`, `/config help`, `/skills help`, and `/provider help`.
- Use live command output as authoritative when available; static notes can drift.

## Basic Slash Commands

- `/abort`: abort the currently running agent turn.
- `/agent`: inspect and scope supervisor tools, skills, and MCP.
- `/config`: show merged, local, or global config; reload or upgrade config.
- `/context`: show the system prompt, context usage, agent snapshots, task context, or session summary.
- `/exit`: save and exit the REPL; alias for `/quit`.
- `/help`: show available slash commands.
- `/history`: show recent message history.
- `/mcp`: inspect and manage MCP server status, tools, activation, refresh, and reload.
- `/memory`: inspect or update workspace memory entries.
- `/mode`: show or switch execution mode.
- `/provider`: show provider cards, activate profiles, open provider management, or update session parameters.
- `/quit`: save and exit the REPL.
- `/session`: inspect, create, resume, save, or export sessions.
- `/skills`: inspect, activate, deactivate, create, remove, or reload skills.
- `/sub-agent`: inspect and scope cognitive sub-agent resources.
- `/subagent`: alias for `/sub-agent`.
- `/tools`: list registered tools or reload the tool registry from config.

## Useful Slash Subcommands

- `/context usage`: show provider, model, estimated prompt/history tokens, context window, and compaction thresholds.
- `/context agents`: list known supervisor and sub-agent context snapshots.
- `/context agent <id>`: show one recorded agent context snapshot.
- `/context task <id>`: show one typed multi-agent task context.
- `/context summary`: show typed multi-agent session summary.
- `/skills list`: list discovered skills, source, active state, description, and path.
- `/skills show <name>`: print a skill's `SKILL.md`.
- `/skills activate <name>` or `/skills add <name>`: persist skill activation in workspace config.
- `/skills deactivate <name>` or `/skills remove <name>`: persist skill deactivation in workspace config.
- `/skills create-local <name>`: create `.nexus/skills/<name>/SKILL.md` plus resource directories.
- `/skills remove-local <name>`: remove only a workspace-local skill.
- `/skills reload`: rescan skills, refresh skill-backed sub-agent tools, and refresh the cached prompt.
- `/config show merged`: show merged configuration.
- `/config show local`: show workspace `.nexus/config.toml`.
- `/config set <key> <value>`: update workspace config.
- `/config reload`: reload config from disk.
- `/provider`: show provider/model settings.
- `/provider set <param> <value>`: update provider session parameters.
- `/mode plan`, `/mode default`, `/mode auto`: switch permission behavior.
- `/memory`: inspect memory commands.
- `/session`: manage sessions.
- `/history`: inspect conversation history.
- `/abort`: abort the currently running agent turn when the UI can accept commands concurrently.
- `/mcp status`: inspect MCP server state.
- `/mcp tools <server>`: show tools from an MCP server.
- `/mcp reload`: reload MCP servers from config.
- `/agent tools`, `/agent skills`, `/agent mcp`: inspect supervisor-scoped resources.
- `/agent allow tool|skill|mcp <name>` and `/agent disallow tool|skill|mcp <name>`: persist supervisor allowlists.
- `/sub-agent list`, `/sub-agent show <name>`, `/sub-agent tools <name>`: inspect cognitive sub-agent resources.
- `/sub-agent allow <name> tool|skill|mcp <id>` and `/sub-agent disallow <name> tool|skill|mcp <id>`: persist sub-agent allowlists.

## Config Pointers

- Workspace config lives at `.nexus/config.toml`.
- Global config lives at `~/.nexus/config.toml`.
- Skills are discovered from built-ins, `skill_paths`, `~/.nexus/skills/`, `.nexus/skills/`, and `.agents/skills/`.
- Skill activation uses `enabled_skills` and `disabled_skills` in workspace config.
- CLI `--skill <name>` is run-only and does not edit config.
- Project custom-tool plugins live under readable `.agents/tools/`; global plugins live under `~/.nexus/plugins/`.
- MCP server activation is maintained by MCP config; MCP tool names should not be hand-added to `allowed_tools`.
- MCP definitions stay in protected `.nexus/config.toml` or `~/.nexus/config.toml` because they may contain credentials.
- `/agent` and `/sub-agent` allow or disallow only resources that are already globally active through `/skills` or `/mcp`.
- Agent-scoped config lives under `[agents]` and `[[sub-agents]]`; sub-agent entries use `name`, `allowed_tools`, `allowed_mcps`, and `allowed_skills`. In advanced mode, empty supervisor `allowed_*` lists mean delegate through sub-agents by default; add `[agents]` allowlist entries for direct supervisor tools, skills, or MCP servers. The generated config lists the four built-in sub-agents with their code allowlists. Older top-level `agent_*`, `subagent_profiles`, and `allowed_mcp_servers` keys are accepted as aliases; obsolete attach/detach keys are ignored. Empty sub-agent `allowed_*` lists preserve sub-agent defaults; `allowed_* = "all"` means every normal workspace tool, active skill, or active MCP server for that scope.
- Built-in and sub-agent tools may be listed in config tool filters.
- `ask_user` is a supervisor-owned blocking clarification tool for focused `text`, `choice`, and `yes_no` questions. It bypasses narrow supervisor scopes when registered, is always hidden from sub-agents, and is limited by `ask_user_max_questions_per_turn`.
- If a sub-agent returns `status: needs_clarification`, ask the user and resume the same `subagent_*` tool with `resume_task_id` plus a structured `clarification` answer. Nexus rehydrates a bounded logical-task packet into a fresh isolated sub-agent call; do not restart with only the answer or paste the full conversation.
- In non-TTY headless mode, `ask_user` returns exit code `4` with a structured `needs_input` request. Defaults are never selected automatically.

## Generated Agent Scopes

New workspace configs use these conservative defaults:

```toml
[agents]
allowed_skills = []
allowed_mcp_servers = []
allowed_tools = ["bash", "read_file", "ask_user"]

[[sub-agents]]
name = "explorer"
allowed_mcps = []
allowed_skills = []
allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"]

[[sub-agents]]
name = "coding"
allowed_mcps = []
allowed_skills = []
allowed_tools = ["read_file", "write_file", "edit", "insert_edit_into_file", "apply_patch", "glob", "grep", "list_dir", "lsp", "git_status", "git_diff", "run_python_check", "run_formatter"]

[[sub-agents]]
name = "code_reviewer"
allowed_mcps = []
allowed_skills = []
allowed_tools = ["git_diff", "read_file", "grep", "lsp", "git_status", "run_tests", "run_python_check"]

[[sub-agents]]
name = "impact_analyzer"
allowed_mcps = []
allowed_skills = []
allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"]
```

## Answering Style

- Prefer exact live slash commands and config keys.
- For command typos, answer with the correction before explaining.
- If the user asks for a list, point them to `/help`, `/tools`, `/skills list`, or `/mcp status` as appropriate.
- If static skill notes conflict with live `/tools`, `/config`, `/provider`, or `/context` output, treat live runtime output as authoritative.
- Keep answers short unless the user asks for a full walkthrough.
