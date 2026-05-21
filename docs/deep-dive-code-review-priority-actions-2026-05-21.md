# Nexus Review Priority Actions

Date: 2026-05-21

## P0

1. Lock `ShellTool` to the workspace for `cwd` resolution.
2. Re-establish a passing test baseline by fixing missing compatibility surfaces:
   - `WriteFileTool`
   - `nexus.tools.filesystem`
   - `nexus.runtime.sandbox`
3. Fix `run_python_check` so it fails when targets do not exist and stops reporting false-positive success.

## P1

1. Replace the slash-command TOML writer so nested MCP fields like `env` are preserved.
2. Parse sub-agent JSON output and preserve fields such as `findings`, `tests_run`, `risks`, and `recommended_next_action`.
3. Fix paused-turn resume so the model resumes the original task instead of seeing a last user message of `continue`.

## P2

1. Re-enable the operational prompt section or delete it intentionally and pin the prompt shape with tests.
2. Make verification commands workspace-aware rather than hard-coded to this repository layout.
3. Remove hardcoded `source_of_truth` values from post-session learning unless they are explicitly configured.
4. Revisit the local MCP activation flow to reduce dead-on-arrival configuration.

## Validation Snapshot

- `uv run pytest -q` currently fails at collection with 6 import errors.
- `ShellTool` accepted an external `cwd` and executed successfully outside the workspace.
- `_write_toml()` dropped nested MCP `env` data during serialization.
- `RunPythonCheckTool` reported success in an empty temporary workspace while stdout said it could not list `nexus` or `tests`.
- paused-turn resume sent `continue` as the last user message while the original task only survived in the system prompt.
- the sub-agent envelope ignored structured fields from a valid JSON result.

## Short Recommendation

Treat the next maintenance pass as a boundary-hardening and contract-restoration pass, not a feature pass. The core loop is usable, but the surrounding surfaces need to be made honest, bounded, and internally consistent before adding more capability.
