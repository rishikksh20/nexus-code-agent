# Repository Instructions

These instructions apply to agent work in this repository.

## Scope

- Work in the live Nexus codebase by default.
- Ignore `reference_code/` for normal search, review, implementation, tests, and documentation updates.
- Open `reference_code/` only when the user explicitly asks for a comparison or reference-code review.
- Prefer current patterns in `nexus/`, `tests/`, `README.md`, and `docs/nexus-codebase-context.md`.

## Runtime Invariants

- Keep approval callbacks centralized in `run_agent_turn()`.
- Do not add user-facing approval callbacks back to `Agent.run()`.
- `Agent.run()` should remain event-driven and use `resume_tool_calls` for deterministic execution of approved pending calls.
- Preserve provider-safe history ordering: assistant messages with tool calls must have matching tool result messages before they are persisted.
- Do not re-add legacy compatibility tools such as `write_note`, `modify_file`, or `replace_text` to the default core registry unless explicitly requested.

## Development

- Run tests with `uv run pytest`.
- Use canonical default tool names in new docs and examples: `write_file`, `edit`, `insert_edit_into_file`, `apply_patch`, `list_dir`, `todos`, and `memory`.
- Treat `.nexus/` as runtime-managed state. Do not write directly into it except through the intended runtime/storage APIs.
