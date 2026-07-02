# Delta-Only Tool And Sub-Agent Config Plan

## Summary

- Move generated supervisor and sub-agent scope defaults out of `.nexus/config.toml` and into runtime defaults.
- Persist only user changes: added or removed tools, skills, MCP mappings, custom YAML sub-agents, and explicit legacy overrides.
- Add an interactive `nexus init` wizard by default in TTY sessions, writing provider setup to `.env` and keeping TOML small.

## Public Interfaces

- Bump `config_version` to `5`.
- Add delta scope keys:
  - `[agents]`: `add_tools`, `remove_tools`, `add_skills`, `remove_skills`, `add_mcp_servers`, `remove_mcp_servers`.
  - `[[sub-agents]]`: `add_tools`, `remove_tools`, `add_skills`, `remove_skills`, `add_mcps`, `remove_mcps`.
- Keep existing `allowed_tools`, `allowed_skills`, and `allowed_mcps`/`allowed_mcp_servers` as explicit replacement overrides for compatibility.
- Add CLI init behavior:
  - `nexus init` runs the wizard in interactive terminals.
  - `nexus init --yes` skips prompts and writes minimal defaults.
  - `--force` keeps its overwrite behavior, combined with wizard or `--yes`.
- Extend slash commands:
  - `/agent allow|disallow ...` and `/sub-agent allow|disallow ...` write delta keys.
  - Add `/agent reset <tool|skill|mcp>` and `/sub-agent reset <name> <tool|skill|mcp>`.
  - Add `/tools enable <tool>` and `/tools disable <tool>` for workspace-level tool registry allow/deny.
  - Add `/sub-agent new <name> [local|global]` as a friendly alias for YAML sub-agent creation.

## Implementation Changes

- In `agent_scope`, define built-in supervisor defaults matching the old generated `[agents]` behavior: direct `bash`, `read_file`, `ask_user`, no default skills, and active MCP servers available unless narrowed.
- Update effective scope resolution to compute: base defaults or explicit `allowed_*` override, then apply `add_*`, then apply `remove_*`; preserve `all` sentinel behavior.
- Update config loading and validation to normalize and validate the new delta keys while preserving old aliases.
- Update config upgrade to compact generated v4 scope blocks:
  - Remove unchanged generated `[agents]` and built-in `[[sub-agents]]` entries.
  - Convert modified built-in scope lists into add/remove deltas where safe.
  - Preserve custom sub-agent profiles and legacy explicit `allowed_*` replacements.
- Update config writing to use comment-preserving TOML editor helpers instead of whole-file dict rewrites for scope changes.
- Change the local config template to omit full `allowed_tools`, `[agents]`, and built-in `[[sub-agents]]` blocks; include short commented examples only.
- Implement the init wizard:
  - Provider choice from existing provider definitions.
  - Model and base URL prompts with current defaults.
  - API key prompt writes provider-specific env vars to workspace `.env`, never TOML.
  - Agent mode choice writes `agent_mode = "advanced"` only when selected.
  - Print next steps: `nexus doctor`, `nexus`, `/agent status`, `/sub-agent list`, `/tools`.

## Test Plan

- Config tests for delta keys, explicit replacement compatibility, `all`, aliases, and validation errors.
- Upgrade tests for compacting generated defaults, converting modified built-in scopes to deltas, preserving custom profiles, and idempotency.
- Slash-command tests confirming allow/disallow writes only delta fields and reset removes empty scope entries.
- Tool command tests for `/tools enable|disable` with top-level `allowed_tools`/`denied_tools`.
- Init wizard tests with mocked input: `.env` writes, `--yes` skips prompts, existing files are preserved unless `--force`.
- Run `uv run pytest`, with focused reruns for config, slash command, CLI, and sub-agent tool tests if needed.

## Assumptions

- "Adding new tools" means managing registered tool availability and mapping, not creating new built-in tool implementations.
- Custom sub-agent definitions default to `.nexus/agents/<name>.yml`; TOML stores only activation and scope deltas.
- Secrets belong in workspace `.env`; TOML never stores API key values.
