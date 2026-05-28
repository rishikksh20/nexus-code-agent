# Supervisor-First Four-Agent Redesign — Phase 1

Date: 2026-05-27

## Status

Implemented the first live slice of the sub-agent redesign.

This phase does not complete the full routing and verification architecture described in `sub-agents-delegation-max-turn-issue.md`, but it does replace the built-in sub-agent model, update the supervisor contract, and align config, upgrade, and primary test surfaces with the new four-agent structure.

## Why This Change Was Made

The previous built-in sub-agent model pushed the supervisor toward delegation-first behavior:

- `planning_analysis`
- `execution`
- `review`
- `verification`

That shape encouraged role-first fan-out, made simple read-only requests feel heavier than necessary, and left the supervisor prompt biased toward calling a sub-agent before doing small local checks.

The redesign goal for this phase was narrower and more practical:

1. Replace the old built-in personas with task-shaped specialists.
2. Move the supervisor prompt away from unconditional delegation.
3. Make the config and runtime surfaces consistent with the new built-ins.
4. Keep the existing turn runner, approval flow, and sub-agent tool architecture intact.

## New Built-In Sub-Agents

Phase 1 replaces the old built-ins with these runtime-default specialists:

### `subagent_explorer`

- Purpose: bounded read-only exploration and summaries
- Allowed tools: `read_file`, `glob`, `grep`, `list_dir`, `lsp`
- Max turns: `10`
- Role: answer-capable read-only investigator for medium exploration work

### `subagent_coding`

- Purpose: focused code changes
- Allowed tools: `read_file`, `write_file`, `edit`, `insert_edit_into_file`, `apply_patch`, `glob`, `grep`, `list_dir`, `lsp`, `git_status`, `git_diff`, `run_python_check`, `run_formatter`
- Max turns: `14`
- Role: the only built-in mutating implementation agent
- Intentional constraint: no broad test ownership and no `bash` in this first slice

### `subagent_code_reviewer`

- Purpose: post-change review and scoped automated verification
- Allowed tools: `git_diff`, `read_file`, `grep`, `lsp`, `git_status`, `run_tests`, `run_python_check`
- Max turns: `8`
- Role: review findings, risk identification, and task-scoped automated checks after impact analysis

### `subagent_impact_analyzer`

- Purpose: blast-radius and verification-scope analysis
- Allowed tools: `read_file`, `glob`, `grep`, `list_dir`, `lsp`, `git_diff`, `git_status`
- Max turns: `10`
- Role: selective read-only specialist for affected files, interfaces, and candidate tests

## Supervisor Contract Changes

The supervisor prompt and tool descriptions were changed from delegation-first to supervisor-first.

### Previous bias

The previous prompt contract strongly implied that any substantive repo inspection, implementation, or validation work should be delegated first.

### New phase-1 contract

The supervisor is now instructed to:

- stay local for tiny read-only work
- delegate when the task exceeds a small local budget
- route edits to `subagent_coding`
- route bounded read-only exploration to `subagent_explorer`
- use `subagent_impact_analyzer` when blast radius or verification scope is unclear
- use `subagent_code_reviewer` for post-change review and scoped automated verification

This change is currently implemented through the prompt contract and tool schema descriptions. It is not yet enforced by a structured routing policy in the runtime.

## Code Changes In This Phase

### Built-in sub-agent definitions

Updated:

- `nexus/tools/subagents.py`

What changed:

- replaced built-in names and prompts
- replaced built-in allowed tool sets
- updated built-in max-turn defaults
- updated builtin-definition detection for MCP tool ingestion

### Supervisor prompt and tool descriptions

Updated:

- `nexus/prompts/system.py`
- `nexus/runtime/agent.py`

What changed:

- rewrote the cognitive sub-agent contract to allow local tiny read-only work
- changed routing text to the new four-agent model
- updated supervisor tool ordering preference to:
  - `subagent_explorer`
  - `subagent_coding`
  - `subagent_impact_analyzer`
  - `subagent_code_reviewer`
- changed direct tool descriptions from “escape hatch” language to “supervisor direct-use path” language

### Scope and config surfaces

Updated:

- `nexus/runtime/agent_scope.py`
- `nexus/config/loader.py`
- `nexus/config/upgrade.py`
- `nexus/cli/init.py`

What changed:

- built-in sub-agent name set now uses `explorer`, `coding`, `code_reviewer`, and `impact_analyzer`
- advanced mode required tool injection now adds:
  - `subagent_explorer`
  - `subagent_coding`
  - `subagent_code_reviewer`
  - `subagent_impact_analyzer`
- legacy config tool names now normalize to the new built-ins
- generated config template now advertises the new built-in tools and default `[[sub-agents]]` profiles

### UX and state cleanup

Updated:

- `nexus/runtime/slash_commands.py`
- `nexus/runtime/context_state.py`

What changed:

- `/sub-agent help` examples now reference `coding` rather than the removed `execution` built-in
- default task role fallback in multi-agent context state now uses `coding`

## Test Changes In This Phase

Updated test surfaces:

- `tests/test_config.py`
- `tests/test_tools.py`
- `tests/test_slash_commands.py`
- `tests/test_prompts.py`
- `tests/test_subagent_defaults.py`
- `tests/test_subagent_tool_flow.py`
- `tests/test_mcp.py`

The test updates in this phase focused on:

- built-in registration and naming
- supervisor default scope behavior
- prompt contract expectations
- advanced-mode allowed tool injection
- default built-in profiles in generated config
- MCP tool ingestion into built-in allowlists

## Validation Performed

Validated with focused test slices after the implementation landed.

### Primary unit slice

```bash
uv run pytest tests/test_config.py tests/test_tools.py tests/test_slash_commands.py tests/test_prompts.py tests/test_subagent_defaults.py -q
```

Result:

- `158 passed`

### Context and slash-command follow-up

```bash
uv run pytest tests/test_context.py tests/test_slash_commands.py -q
```

Result:

- `75 passed`

### Deep rename-specific follow-up

```bash
uv run pytest tests/test_subagent_tool_flow.py::test_supervisor_tool_schemas_prefer_subagents_when_direct_tools_are_available tests/test_mcp.py::test_builtin_subagent_allowlists_ingest_registered_mcp_tools -q
```

Result:

- `2 passed`

### Static error check on touched runtime files

Checked with editor diagnostics on:

- `nexus/tools/subagents.py`
- `nexus/runtime/agent_scope.py`
- `nexus/prompts/system.py`
- `nexus/runtime/agent.py`
- `nexus/config/loader.py`
- `nexus/config/upgrade.py`
- `nexus/cli/init.py`
- `nexus/runtime/slash_commands.py`
- `nexus/runtime/context_state.py`

Result:

- no file errors reported

## What This Phase Does Not Do Yet

This phase is intentionally a first slice. It does not yet implement the full redesign.

Still missing:

1. A structured supervisor routing gate with explicit tool-call budgets.
2. Automatic runtime enforcement of “stay local for tiny read-only work.”
3. Structured impact-analysis payloads consumed by downstream agents.
4. Packet-backed evidence reuse between Explorer, Coding, CodeReviewer, and ImpactAnalyzer.
5. Duplicate read suppression in the sub-agent runtime.
6. Scoped failure attribution and baseline-aware verification logic.
7. Full doc and test refresh across every concept, observability, and UI surface that still mentions the removed built-ins.

## Remaining Documentation Drift

This phase updates the main runtime-facing documentation seam in `docs/sub-agents-integration.md`, but it does not fully rewrite every concept or review note in `docs/` that still references the old built-in names.

Examples of docs that still need follow-up cleanup:

- `docs/concepts/multi-agent-testing-guide.md`
- `docs/concepts/multi-agent-code-agent-architecture-roadmap.md`
- `docs/nexus-codebase-context.md`
- `docs/nexus-codebase-review.md`

Those files are not necessarily wrong about the broader system, but their built-in name references are now stale.

## Recommended Next Phases

### Phase 2 — Runtime routing gate

Add a structured supervisor routing decision so tiny read-only tasks reliably stay local and only larger work delegates.

### Phase 3 — Impact analyzer handoff

Add a structured impact-analysis result model that can drive CodeReviewer verification scope.

### Phase 4 — Packet-backed evidence reuse

Use the existing multi-agent packet layer to avoid cold-start rereads across sub-agents.

### Phase 5 — Broader doc and test cleanup

Replace remaining old built-in names in observability, UI, concept, and end-to-end test surfaces.

## Summary

Phase 1 is a naming and contract reset, not the full architecture.

It delivers:

- the new four built-in sub-agents
- a supervisor-first prompt contract
- aligned config and upgrade behavior
- updated help text and default state naming
- a passing primary unit test surface for the renamed built-ins

It does not yet deliver:

- runtime-scored routing
- impact-analysis artifacts
- repository-memory reuse
- scoped failure attribution

That is the right tradeoff for this first implementation slice because it removes the old built-in model cleanly without mixing the rename with the deeper orchestration work that still needs to be designed and tested.