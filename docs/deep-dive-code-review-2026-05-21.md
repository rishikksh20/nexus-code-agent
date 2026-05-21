# Nexus Deep Dive Code Review

Date: 2026-05-21

## Scope

- Reviewed code under `nexus/`, selected top-level project files, and the active test suite under `tests/`.
- Excluded from analysis as requested: `docs/`, `reference_code/`, and `workspace/`.
- No runtime code was changed during this review. Only review artifacts are added.

## Review Method

- Read the main runtime entry and session assembly flow: `nexus/app.py`, `nexus/runtime/agent.py`, `nexus/runtime/repl.py`, `nexus/runtime/turn_runner.py`, `nexus/runtime/repl_state.py`, `nexus/runtime/runtime_session.py`, and `nexus/runtime/sessions.py`.
- Inspected config loading and upgrade paths: `nexus/config/defaults.py`, `nexus/config/loader.py`, and `nexus/config/upgrade.py`.
- Inspected security, shell, filesystem-adjacent, MCP, sub-agent, memory, observability, and verification surfaces.
- Sampled the tests as an intent oracle, then validated high-risk hypotheses with direct executions.

## Validation Performed

- Baseline suite: `uv run pytest -q`
- Focused repros:
  - `ShellTool` with `cwd` set outside the workspace.
  - `nexus.runtime.slash_commands._write_toml()` writing an MCP config with nested `env`.
  - `RunPythonCheckTool` in a temporary workspace that does not contain `nexus/` or `tests/`.
  - paused-turn resume flow in `ReplState`.
  - `_subagent_result_envelope()` with a structured JSON sub-agent result.

## Executive Summary

The core agent loop is mostly coherent. The strongest parts are the deterministic approval resume path, session history sanitization, and the workspace and `.nexus` protections inside the file-editing tools.

The main problems are around boundary surfaces and feature completeness rather than the happy-path loop itself. The highest-impact issues are:

- the plain shell tool can run from an arbitrary directory outside the workspace;
- the slash-command TOML writer silently drops nested config fields;
- the verification tools can claim success even when they did not inspect any project code;
- the repository no longer satisfies its own compatibility test surface;
- sub-agent structured results are requested but mostly discarded by the supervisor wrapper.

## What Looks Strong

- `nexus/runtime/turn_runner.py` keeps approvals centralized and resumes the exact approved tool call instead of regenerating it through the model.
- `nexus/runtime/sessions.py` sanitizes assistant/tool ordering before persistence and before provider replay.
- `nexus/tools/builtin/write_file.py`, `edit_file.py`, `patch.py`, and `smart_edit.py` all enforce workspace and `.nexus` boundaries locally.
- `nexus/runtime/agent_scope.py` centralizes supervisor and sub-agent visibility logic instead of scattering it across the runtime.

## Findings

### High 1. `ShellTool` can escape the workspace through `cwd`

Relevant code:

- `nexus/tools/builtin/shell.py::ShellTool.execute`
- `nexus/tools/utils.py::resolve_path`

What happens:

- `ShellTool.execute()` resolves `cwd` from user input.
- It checks only `work_dir.exists()`.
- It does not require `work_dir` to remain under `context.working_directory`.

Why this matters:

- The file-editing tools are workspace-bound, but the shell tool can be pointed at any absolute path or any relative path that resolves outside the workspace.
- That breaks the runtime's containment story and lets the model operate on unrelated filesystem locations as soon as shell access is approved.

Validated behavior:

- In a focused repro, `ShellTool` executed `pwd` successfully with `cwd` set to a temporary directory outside the workspace.
- The tool returned `is_error = False` and the output was the external temporary directory path, not the workspace path.

Recommended fix:

- Apply the same boundary rule used by the structured verification tools: reject any `cwd` whose resolved path is outside the workspace.
- Consider refusing `.nexus` as a shell `cwd` unless sandbox mode is explicitly in use.

### High 2. The baseline test suite is broken at collection because compatibility surfaces are missing

Relevant code and missing surfaces:

- `nexus/tools/builtin/__init__.py`
- missing file: `nexus/tools/filesystem.py`
- missing file: `nexus/runtime/sandbox.py`
- missing export/class: `WriteFileTool`

Validated behavior:

- `uv run pytest -q` failed during collection with 6 import errors.
- Failures included:
  - `tests/test_agent.py`, `tests/test_cli.py`, `tests/test_hooks.py`, and `tests/test_repl.py` importing `WriteFileTool` from `nexus.tools.builtin`
  - `tests/test_filesystem_tools.py` importing `nexus.tools.filesystem`
  - `tests/test_sandbox.py` importing `nexus.runtime.sandbox`

Why this matters:

- The repository currently has no green baseline, so regressions can hide behind collection failures.
- The codebase claims to preserve compatibility shims in several places, but the exported surface has drifted past what the tests still depend on.

Recommended fix:

- Either restore the compatibility shims that the test suite still expects, or deliberately remove the legacy expectations and update the tests and docs in the same change.
- Do not leave the repository in the current half-migrated state.

### High 3. `run_python_check` can report success without inspecting project code

Relevant code:

- `nexus/tools/builtin/verification.py::RunPythonCheckTool`
- `nexus/tools/builtin/verification.py::_CommandTool.execute`

What happens:

- The tool was hard-coded to `python -m compileall -q nexus tests`.
- `_CommandTool.execute()` marks the run as passed when `exit_code == 0`.
- `compileall` can emit `Can't list 'nexus'` and `Can't list 'tests'` to stdout while still exiting with code 0.

Validated behavior:

- In a temporary workspace with no `nexus/` or `tests/`, `RunPythonCheckTool` returned:
  - `is_error = False`
  - `exit_code = 0`
  - `passed = true`
  - `stdout_tail = "Can't list 'nexus'\nCan't list 'tests'"`

Why this matters:

- The tool can produce a false-positive validation result.
- This is especially problematic because the README and runtime model present Nexus as workspace-oriented and usable in custom workspaces.
Recommended fix:

- Make verification targets workspace-aware instead of repo-name-aware.
- Treat missing target directories as failures.
- Use real linter and type checker backends when configured, with a fallback that is explicit about degraded validation.

### High 4. Slash-command config rewrites can silently corrupt MCP configuration

Relevant code:

- `nexus/runtime/slash_commands.py::_write_toml`

What happens:

- `_write_toml()` skips child values that are dictionaries when serializing tables and array tables.
- MCP server entries can legitimately contain nested tables like `env = { TOKEN = "abc" }`.

Validated behavior:

- Writing a payload with:
  - `mcp_servers = [{ name = "filesystem", command = ["fake"], env = { TOKEN = "abc" }, prefix = "fs_" }]`
- produced TOML that contained only:
  - `name`
  - `command`
  - `prefix`
- the nested `env` field was dropped completely.

Why this matters:

- Any slash command that rewrites the local config can erase nested MCP settings.
- The user-visible action looks successful, but the resulting runtime behavior changes silently.

Recommended fix:

- Replace `_write_toml()` with a round-trip-preserving serializer or a narrow field editor that does not rebuild unrelated config sections.
- Add tests that prove nested MCP fields survive `/mcp activate`, `/mcp deactivate`, `/mcp reload`, and `/config set` flows.

### Medium 5. The sub-agent wrapper asks for structured JSON but discards most of it

Relevant code:

- `nexus/sandbox/agent_tool.py::_direct_subagent_system_prompt`
- `nexus/sandbox/agent_tool.py::_subagent_result_envelope`

What happens:

- The sub-agent system prompt instructs the inner agent to return JSON with fields like `status`, `summary`, `findings`, `tests_run`, `risks`, and `recommended_next_action`.
- `_subagent_result_envelope()` does not parse that JSON.
- Instead it stores the entire raw JSON string as `raw_result`, derives `summary` from the first non-empty line, and reconstructs only a few fields from the context snapshot.

Validated behavior:

- A repro using a structured JSON `raw_result` showed:
  - `summary` became the full raw JSON string
  - `findings` was absent from the envelope
  - `tests_run` became `[]`
  - `recommended_next_action` was forced back to `continue`

Why this matters:

- The structured contract between supervisor and sub-agent is mostly unused.
- This wastes the strongest value of delegation: actionable structured summaries.

Recommended fix:

- Parse JSON results when they are valid and preserve their structured fields.
- Fall back to the current raw-envelope behavior only when the inner result is unstructured.

### Medium 6. Paused-turn resume records `continue` as the last user message instead of the original task

Relevant code:

- `nexus/runtime/repl.py::run_repl`
- `nexus/cli/headless.py::run_headless`
- `nexus/runtime/repl_state.py::consume_turn_prompt`

What happens:

- When a paused task is resumed, `consume_turn_prompt()` returns the original prompt as `effective_prompt`.
- The REPL and headless runners still append the raw user input (`continue`) to history.
- `prepare_turn()` then sends that literal `continue` user message in `model_messages`, while the original task is only present in the rebuilt system prompt.

Validated behavior:

- In a focused repro:
  - `resumed = True`
  - `effective_prompt = original long task`
  - the last model message content was `continue`
  - the system prompt still contained `original long task`

Why this matters:

- The model sees mixed signals: the conversation says `continue`, while the current-task section says something else.
- This can reduce resume quality and make tool-call-limit continuation less deterministic than it appears.

Recommended fix:

- Do not append `continue` as a normal user message when resuming a paused task.
- Either append the original prompt again with explicit resume metadata, or resume without adding a new user-history message at all.

### Medium 7. Operational prompt guidance exists but is not actually injected

Relevant code:

- `nexus/prompts/system.py::build_base_instruction`
- `nexus/prompts/system.py::_get_operational_section`

What happens:

- `_get_operational_section()` is present, but the call that would append it in `build_base_instruction()` is commented out.

Why this matters:

- The runtime loses a whole block of intended instructions about workflow, verification, concision, and task completion.
- This is exactly the kind of silent prompt drift that changes behavior without any failing tests.

Recommended fix:

- Re-enable the section if it is still intended.
- If it is no longer desired, delete the dead code and update prompt tests to pin the new intended prompt shape.

### Low 8. Post-session learning hardcodes source-of-truth paths that may not match the reviewed repository

Relevant code:

- `nexus/runtime/post_session.py::_update_workspace_payload`

What happens:

- The persisted workspace payload always writes:
  - `docs/action-plan`
  - `docs/openai-code-tutorial`
- into `source_of_truth`.

Why this matters:

- This injects repo-specific assumptions into learned workspace metadata instead of inferring the actual source-of-truth from observed behavior.
- Over time that can poison generated knowledge and profile output.

Recommended fix:

- Derive source-of-truth candidates from explicit config or observed operator behavior.
- If unknown, leave the field empty rather than inventing authority.

## Product and Flow Gaps

### Gap 1. MCP activation is a two-step flow that easily produces inert local config

Relevant code:

- `nexus/config/loader.py::_active_mcp_servers`
- `tests/test_config.py::test_config_keeps_local_mcp_servers_inactive_until_enabled`

Observation:

- Declaring a local `mcp_servers` entry does not activate it.
- A second step through `enabled_mcp_servers` or `/mcp activate <name>` is required.

Assessment:

- This is currently intentional, not an accidental regression.
- It still creates operator confusion because a fully declared local server remains inert until separately enabled.

Recommendation:

- Either keep the current model but make the UX unmistakable, or auto-activate local MCP servers while reserving enable/disable toggles for global catalog entries.

### Gap 2. Verification defaults are repository-centric, not workspace-centric

Relevant code:

- `nexus/tools/builtin/verification.py`
- README claims Nexus can run in arbitrary workspaces using the current directory as root.

Assessment:

- `run_tests`, `run_python_check`, and `run_python_check` are not dynamically derived from workspace content or config.
- This limits Nexus as a general coding agent outside the main repo and makes validation quality depend on the current repository layout.

Recommendation:

- Move verification command defaults into config or workspace detection.
- Report clearly when no meaningful verification strategy is available.

## Suggested Remediation Order

1. Fix the shell `cwd` escape. This is the strongest containment failure in the runtime.
2. Restore a green baseline by resolving the missing compatibility surfaces or removing the stale expectations.
3. Fix verification truthfulness for `run_python_check` and `run_python_check`.
4. Replace the lossy TOML rewrite path in slash commands.
5. Parse and preserve structured sub-agent output.
6. Repair paused-turn resume history.
7. Re-enable or remove the dead operational prompt section intentionally.
8. Make post-session knowledge generation less opinionated and more data-driven.

## Bottom Line

Nexus has a solid core execution model, but several boundary and integration surfaces are under-policed or partially migrated. The biggest risk is not that the central loop is fundamentally unsound; it is that adjacent surfaces currently weaken the guarantees the core loop is trying to provide.
