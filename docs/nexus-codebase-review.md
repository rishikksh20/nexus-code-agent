# Nexus Codebase Review

Review date: 2026-05-14

Scope:

- Compared Nexus flow against `reference_code/`, especially `core/agent/agent.py`, `core/context/manager.py`, and `core/safety/approval.py`.
- Reviewed the Nexus runtime path from CLI startup through model streaming, tool execution, approval handling, session persistence, context compaction, and extension wiring.
- Ran the automated test suite after fixes.

Verification:

```bash
uv run --group dev python -m pytest -q
```

Result:

```text
257 passed
```

## Executive Summary

The codebase is structurally sound and now has a passing test suite. The core architecture is stronger than the reference implementation in several areas: typed runtime contracts, explicit tool registry records, stronger path policy, richer session persistence, headless mode, MCP/plugin/sandbox/delegation extension points, and better observability.

The largest confirmed issue was in the approval flow. Nexus had two different owners for confirmation handling:

- `Agent._agentic_loop()` could call `approval_callback` directly.
- `run_agent_turn()` also expected to own confirmation, retry, scoped approval, and history-safe event commits.

That split caused subtle logical bugs around turn-wide approval, duplicate tool execution, and denied invocations. The fix makes `run_agent_turn()` the only owner of user-facing confirmation callbacks. `Agent.run()` now emits `CONFIRMATION_REQUESTED` and returns; callers that need approvals must consume the event and re-enter through the shared turn runner.

## Confirmed Issues Fixed

### 1. Test Suite Could Not Collect

Status: fixed.

Symptoms:

- `WriteNoteTool` was referenced by tests, manual docs, and the built-in skill, but was no longer exported or registered.
- `ReplaceTextTool` was documented as present in `nexus/tools/filesystem.py`, and tests imported it, but the class was missing.
- Tests imported `collect_turn_events`, but the implementation had been renamed to `run_agent_turn` without a compatibility alias.

Fixes:

- Restored `WriteNoteTool` as a compatibility wrapper around `WriteFileTool`, with a max-content-size guard.
- Exported and registered `WriteNoteTool` with built-ins.
- Restored `ReplaceTextTool`.
- Added `collect_turn_events = run_agent_turn`.

Why this matters:

The codebase had drift between runtime implementation, docs/skills, and tests. This kind of drift is especially dangerous for coding agents because prompt/tool names become part of the model contract.

### 2. Approval Ownership Was Split

Status: fixed.

Problem:

`run_agent_turn()` passed its `approval_callback` down into `Agent.run()`. That meant the lower-level agent loop sometimes consumed approvals directly before the turn runner could:

- discard unsafe pre-approval batches,
- record turn-wide approval correctly,
- handle user denial without surfacing a stale denied result,
- retry the model with a clean history boundary.

Observed failure:

Approving a first mutating tool with scope `turn` still led to repeated confirmations and duplicate tool results when the model regenerated the same tool calls.

Fix:

`Agent.run()` no longer accepts `approval_callback`. It emits `CONFIRMATION_REQUESTED` and stops. `run_agent_turn()` handles the callback, records approval or refusal in `ApprovalManager`, resumes the exact pending tool call after approval, and then continues the event stream with the resulting tool message in history.

Reference comparison:

The reference agent keeps message mutation in `Session.context_manager` and approval logic in the tool invocation path. Nexus now better preserves that principle at the higher-level turn boundary: the runtime state owner decides when events are committed.

### 3. Denied Tool Calls Could Leak As User-Visible Tool Results

Status: fixed.

Problem:

When `Agent._agentic_loop()` consumed an approval callback directly, a denied approval emitted a `TOOL_RESULT` event for the refused call. In retry flows this produced extra user-visible tool results even though the important model-facing state was "user refused; do not retry this invocation".

Fix:

The direct callback path has been removed from `Agent.run()`. Denials are recorded in `run_agent_turn()`, and any later regenerated matching tool call is blocked by `ApprovalManager` without prompting again.

### 4. Write Path Policy Missed Compatibility Tools

Status: fixed.

Problem:

`PermissionChecker._path_policy()` only applied workspace and `.nexus` hard denials to `write_file` and `modify_file`. Compatibility/newer write tools such as `write_note` and `replace_text` could rely on their own execution-time checks, but the permission layer would not deny them early.

Fix:

The path hard-denial set now includes:

- `write_file`
- `write_note`
- `modify_file`
- `replace_text`

Design note:

The permission layer should be the primary policy gate. Individual tools should still validate defensively, but policy should not depend on each tool remembering the same checks.

### 5. Prompt Guardrails Were Missing Expected Safety Guidance

Status: fixed.

Problem:

Tests and intended behavior expected the system prompt to say:

- repo scan/explain/review tasks are read-only by default,
- mutating tools should not run unless explicitly requested,
- hidden/private dot-path reads are blocked,
- `.nexus` state should not be read directly.

Fix:

Added those instructions to the security and tool-guideline prompt sections.

Why this matters:

The tool policy enforces hard boundaries, but the prompt should prevent unnecessary approval prompts and model attempts to inspect private runtime state.

### 6. Provider Slash Command Had Stale Surface Area

Status: fixed.

Problems:

- `/provider list` omitted `ollama` even though config and app startup support it.
- An unused `VALID_PROVIDERS` tuple had stale values and was not enforcing anything.
- `/provider set model_name ...` wrote local TOML but then reloaded config in a way that allowed environment variables such as `MODEL` to override the just-set live value.

Fixes:

- Added `ollama` to provider list output.
- Removed the unused provider tuple.
- Reloaded config with a CLI-style override for the just-set value so the live REPL state reflects the user command immediately.

### 7. Audit Rollback Metadata Missed File Writes

Status: fixed.

Problem:

Audit records for `write_note` mutating actions reported rollback support as false.

Fix:

`write_file`, `write_note`, `modify_file`, and `replace_text` now report rollback as supported in audit metadata because these file edits can be reconstructed from confirmation diffs or VCS state.

## Remaining Risks And Recommendations

### 1. Tool Surface Area Is Redundant

Current overlapping write/edit tools:

- `write_file`
- `write_note`
- `edit`
- `insert_edit`
- `modify_file`
- `replace_text`
- `apply_patch`

Recommendation:

Keep all of them for now because tests, docs, skills, and model prompts reference them. Over time, decide on a canonical public set and mark the rest as compatibility tools:

- Canonical create/overwrite: `write_file`
- Canonical exact edit: `edit`
- Canonical patch: `apply_patch`
- Compatibility: `write_note`, `replace_text`, possibly `modify_file`

If deprecating, do it in stages:

1. Keep aliases registered.
2. Update skills/docs/prompts to canonical names.
3. Add deprecation notes in tool descriptions.
4. Remove only after tests/manual docs no longer reference them.

### 2. Approval Logic Still Exists In Two Layers

Current state:

- Fixed. `Agent.run()` and `Agent._agentic_loop()` no longer accept an `approval_callback`.
- REPL/headless flows centralize approval callbacks in `run_agent_turn()`.
- Lower-level callers must consume `CONFIRMATION_REQUESTED` events instead of passing a callback into the agent.

Risk:

The previous risk was that future contributors could accidentally use direct `Agent.run(..., approval_callback=...)` and reintroduce different behavior from the main runtime. That call path is now impossible.

Resolution:

Removed direct callback approval from `Agent.run()` and made confirmation event consumption the only approval path.

Follow-up fix:

The first event-only implementation retried the model after approval before executing the pending tool. Some providers regenerated a similar but not identical mutating call, causing repeated approval prompts. The runtime now executes the already-approved pending tool call directly, so the approved action is deterministic and does not depend on provider regeneration.

### 3. Permission Policy Is Name-Based In Places

Examples:

- `PermissionChecker` special-cases `bash`, `write_file`, and `memory`.
- Path policy uses a tool-name allowlist for write tools.
- Audit danger and rollback classification are tool-name based.

Risk:

Adding a new mutating file tool can miss policy/audit behavior unless every name-based list is updated.

Recommendation:

Introduce richer tool metadata later, such as:

- `ToolKind.WRITE` plus `affects_paths=True`
- `supports_diff_preview=True`
- `rollback_strategy="diff" | "none"`
- `requires_confirmation_level`

Then policy can derive behavior from capabilities rather than names.

### 4. Context Compaction Is Heuristic-Only

Current behavior:

- Token estimation uses `len(text) // 4`.
- Compaction summary is structural, not LLM-generated.
- The reference implementation uses an LLM compactor that can preserve more semantic detail.

Risk:

Long coding sessions may lose important intent, decisions, or file-specific details.

Recommendation:

Keep the heuristic path as a safe fallback, but add an optional provider-backed compaction pass once streaming/provider behavior is stable.

### 5. MCP Tools Are Treated As Mutating By Default

Current behavior:

`MCPToolAdapter.is_mutating = True`.

Benefit:

Safe default.

Cost:

Read-only MCP tools always require mutating-tool treatment unless allowed by mode/policy.

Recommendation:

If MCP servers expose annotations or metadata for read-only tools, map them into `ToolKind.READ`/`is_mutating=False`. Until then, the conservative behavior is acceptable.

### 6. Shell Risk Classification Is Duplicated

Current state:

- `nexus/tools/filesystem.py` has `classify_bash_risk`.
- `nexus/tools/builtin/shell.py` has its own `_classify_risk`.
- `nexus/security/classifier.py` wraps the filesystem classifier and adds dangerous promotion.

Risk:

Risk labels can diverge between permission decisions and shell result metadata.

Recommendation:

Move command classification into `nexus/security/classifier.py` as the single source of truth and have `ShellTool` use it for metadata.

### 7. Slash Command Config Writes Are Simple TOML Rewrites

Current behavior:

`_write_toml()` rewrites TOML from a flat dictionary, losing comments and formatting for `/provider set` and `/config set`.

Risk:

User-edited config comments can be destroyed.

Recommendation:

Use the existing "append missing keys" style where possible, or introduce a TOML-preserving writer if config editing becomes a core UX path.

### 8. Extension Features Increase Maintenance Load

Feature areas beyond the reference code:

- plugin loader
- skills
- MCP
- delegation workers
- sandbox commands
- post-session learning
- runtime logs/metrics/audit
- manual CLI test matrix

These are valuable, but each one creates state and lifecycle concerns. The biggest maintenance risk is not any one feature, but the number of pathways that can register tools, mutate state, or keep async resources alive.

Recommendation:

For each extension, keep a small lifecycle checklist:

- where it is configured,
- where it is initialized,
- where it registers tools,
- where it is shown in slash commands,
- how it is closed,
- which tests cover failure and disabled states.

## Reference Flow Comparison

### Reference Strengths To Preserve

The reference implementation is simpler in helpful ways:

- `CLI._process_message()` only renders events and does not own business logic.
- `Agent.run()` adds the user message and then owns the model/tool loop.
- `ContextManager` is the only message-history owner.
- Tool invocation owns approval checks.
- Tool results are added only after a full tool-call batch completes.
- Context pruning happens after usage/tool result updates.

### Nexus Improvements Over Reference

Nexus adds useful production-oriented structure:

- Typed `Message`, `ToolCall`, `ToolResult`, `RuntimeRequest`, and `StreamEvent`.
- Shared `run_agent_turn()` for interactive and headless flows.
- Strict session sanitization for provider wire-format correctness.
- Permission modes (`plan`, `default`, `auto`) and approval scopes.
- Tool registry records with source/origin.
- Config hierarchy and CLI overrides.
- Hooks, metrics, audit, MCP, plugins, skills, sandboxing, delegation, and post-session learning.

### Main Alignment Needed

The reference keeps fewer ownership boundaries, so state flow is easier to reason about. Nexus should preserve this rule:

Only one layer should own each kind of state transition.

Recommended ownership:

- CLI/app startup: `NexusApp`
- one user turn, confirmation, event commit: `run_agent_turn()`
- model/tool loop without UI state: `Agent`
- durable conversation state: `ReplState` and `SessionStore`
- permission decision: `PermissionChecker` plus `ApprovalManager`
- provider wire format: provider adapter
- tool side effects: tool implementation

## Files Modified During Review

- `nexus/runtime/repl.py`
- `nexus/runtime/agent.py`
- `nexus/tools/builtin/write_file.py`
- `nexus/tools/builtin/__init__.py`
- `nexus/tools/registry.py`
- `nexus/tools/filesystem.py`
- `nexus/security/permissions.py`
- `nexus/prompts/system.py`
- `nexus/observability/audit.py`
- `nexus/runtime/slash_commands.py`

## Follow-Up Backlog

High value:

- Remove or formally deprecate duplicate edit/write tools after updating skills/docs/tests.
- Make command risk classification single-source.
- Add metadata-driven policy for path-affecting mutating tools.

Medium value:

- Add tests for `/provider list` including `ollama`.
- Add tests proving `write_note` is registered in the default core registry.
- Add tests for `replace_text` path-policy denial at the permission layer, not just tool execution.
- Add tests for audit rollback support for `write_file`, `modify_file`, and `replace_text`.

Lower value:

- Preserve TOML comments in slash-command config writes.
- Add lightweight static analysis to CI for unused constants/imports.
- Create an extension lifecycle matrix for MCP/plugins/skills/delegation/sandbox.
