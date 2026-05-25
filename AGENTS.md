# Repository Instructions

These instructions apply to agent work in this repository.

## Scope

- Work in the live Nexus codebase by default.
- Ignore `reference_code/` and `workspace/` for normal search, review, implementation, tests, and documentation updates.
- Open `reference_code/` only when the user explicitly asks for a comparison or reference-code review.
- Prefer current patterns in `nexus/`, `tests/`, `README.md`, and `docs/nexus-codebase-context.md`.

## Runtime Invariants

- Keep approval callbacks centralized in `run_agent_turn()`.
- Do not add user-facing approval callbacks back to `Agent.run()`.
- `Agent.run()` should remain event-driven and use `resume_tool_calls` for deterministic execution of approved pending calls.
- Preserve provider-safe history ordering: assistant messages with tool calls must have matching tool result messages before they are persisted.
- Do not re-add legacy compatibility tools such as `write_note`, `modify_file`, or `replace_text` to the default core registry unless explicitly requested.

## Agent and Sub-Agent Scoping

- All supervisor and sub-agent tool/skill/MCP visibility logic lives in `nexus/runtime/agent_scope.py`. Do not replicate scoping calculations elsewhere.
- Use `supervisor_tool_names()`, `subagent_tool_names()`, `supervisor_skill_names()`, and `subagent_skill_names()` from `agent_scope.py` when computing effective resource sets.
- The `ALL_SCOPE_SENTINEL = "all"` string is the canonical value for "expose everything active"; use `is_all_scope()` to check it rather than comparing strings directly.
- Built-in cognitive sub-agent names (`planning_analysis`, `execution`, `review`, `verification`) are declared in `BUILTIN_SUBAGENT_NAMES` in `agent_scope.py`.

## Skills

- Built-in skills live under `nexus/builtin_skills/<skill-name>/SKILL.md`. Each skill directory must contain exactly one `SKILL.md`.
- `SKILL.md` files require YAML frontmatter with at minimum `name` and `description` fields; the body is free-form Markdown instructions.
- Skill discovery, parsing, and registry logic lives in `nexus/skills/` (`loader.py`, `parser.py`, `registry.py`, `models.py`). Do not bypass this layer.
- Refer to `docs/skills.md` for the authoritative skill authoring guide and activation patterns.
- Skill-backed sub-agent tools follow the naming convention `subagent_<skill-name>` (hyphens replaced by underscores) and are registered automatically when `agent_mode = "advanced"`.

## Config System

- Config merging (global → local → env → CLI) is handled in `nexus/config/loader.py`. Do not add new resolution logic outside this file.
- Schema upgrades, deprecated-key removal, and default-key backfill are handled in `nexus/config/upgrade.py`. Add new upgrade steps there rather than in the loader.
- `nexus/config/defaults.py` is the single source of truth for all `AgentConfig` field names and default values.

## REPL Slash Commands

- All `/command` handlers are registered in `nexus/runtime/slash_commands.py`. Add new REPL commands there.
- Every command group should implement a `help` subcommand that prints a usage table with subcommands and examples.

## Development

- Run tests with `uv run pytest`.
- Use canonical default tool names in new docs and examples: `write_file`, `edit`, `insert_edit_into_file`, `apply_patch`, `list_dir`, `todos`, and `memory`.
- Treat `.nexus/` as runtime-managed state. Do not write directly into it except through the intended runtime/storage APIs.
