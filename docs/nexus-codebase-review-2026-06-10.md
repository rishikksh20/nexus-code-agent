# Nexus Codebase Review - 2026-06-10

## Scope

This review covers the live `nexus/` codebase only. Per request, `docs/`, `tests/`, `reference_code/`, and `workspace/` were not used as review inputs, except that this report is written under `docs/`.

Verification performed:

- `uv run python -m compileall nexus` passed.

The review focused on agent execution flow, approval safety, sub-agent scoping, tools, MCP/plugin boundaries, provider integrations, config, memory, and obvious optimization or maintenance issues.

## Executive Summary

Nexus has a solid event-driven agent core. The main turn loop keeps approval handling outside `Agent.run()`, and provider-safe history ordering is mostly handled correctly by persisting tool-call assistant messages only when their tool results are available. The registry, config, skill, tool, and runtime layers are generally well separated.

The highest-risk issues are around approval broadening and tool mutation safety:

- A turn-wide approval for one mutating action can preapprove later mutating actions with a different tool, different path, or higher risk.
- Shell risk classification treats interpreters such as `python`, `python3`, and `node` as low risk even when they can execute arbitrary inline code.
- `insert_edit_into_file` can show an approval preview that does not match the eventual mutation, and can append code to the end of a file after a fuzzy-match failure.
- `apply_patch` can leave partial file mutations behind if a later file or hunk fails.

The largest architectural drift is sub-agent scoping. The repository instructions say built-in cognitive sub-agent names are `planning_analysis`, `execution`, `review`, and `verification`, with scoping logic centralized in `nexus/runtime/agent_scope.py`. The live code uses and maps to `explorer`, `coding`, `code_reviewer`, and `impact_analyzer` in several places, and repeats built-in sub-agent/scope logic outside `agent_scope.py`.

## Agent Flow

Current flow, simplified:

1. CLI/headless/textual entrypoints load config, sessions, registry, memory, skills, MCP, and plugins.
2. REPL/headless appends a user message to session state.
3. `run_agent_turn()` builds `RunContext`, owns approval callbacks, and calls `Agent.run()`.
4. `Agent.run()` prepares provider-safe model messages, streams model events, executes approved tool calls, and supports `resume_tool_calls`.
5. `run_agent_turn()` applies events to the session only after the turn has reached a persistable point.

Good properties:

- Approval callbacks remain centralized around `run_agent_turn()`.
- `Agent.run()` remains event-driven.
- Approved pending calls are resumed deterministically through `resume_tool_calls`.
- History persistence avoids saving assistant tool-call messages without matching tool results in the normal path.

Flow concerns:

- Headless confirmation flow can return before `state.apply_events(events)`, which means the assistant's pending tool-call plan may not be persisted when approval is required and no callback is available.
- Paused-turn prompts are expanded for model execution, but the persisted user message can remain the literal command such as `continue`. That is usable, but session history becomes less self-describing.
- Context compaction preserves statistics and recent-message metadata, but does not create a semantic summary of the conversation. Important requirements can be lost after compaction.

## High-Risk Findings

### 1. Turn-wide mutating approval is too broad

Relevant files:

- `nexus/runtime/turn_runner.py`
- `nexus/runtime/agent.py`
- `nexus/security/manager.py`
- `nexus/sandbox/agent_tool.py`

`supports_turn_wide_approval()` and `_supports_turn_wide_approval()` currently allow turn-wide approval broadly. Once a user approves one mutating action for the whole turn, `ApprovalManager.is_turn_wide_mutating_preapproved()` can preapprove later mutating calls without checking tool name, affected paths, risk level, or whether the later action is more dangerous.

Impact:

- A user can approve a low-risk write and accidentally allow a later high-risk shell or MCP action in the same turn.
- The approval UI implies scoped intent, but the backend treats the approval as broad mutating consent.

Recommendation:

- Make turn-wide approval tool-aware and risk-aware.
- Do not let turn-wide file-edit approval apply to shell, MCP, plugin, or network-capable tools.
- Store the approved scope as structured data: tool name, risk ceiling, affected path prefixes, and mutation category.
- Require fresh approval if a later call expands any dimension of that scope.

### 2. Shell interpreter commands are under-classified

Relevant files:

- `nexus/tools/builtin/shell.py`
- `nexus/security/classifier.py`

The shell risk classifier treats base commands such as `python`, `python3`, and `node` as low risk. That is reasonable for version checks, but unsafe for inline execution flags such as `python -c`, `python -m`, or `node -e`.

Impact:

- Arbitrary code execution can appear as a low-risk command.
- Default-mode approval behavior can be bypassed for commands that should require confirmation.

Recommendation:

- Classify interpreter inline execution flags as at least medium risk.
- Treat package manager subcommands, shell escapes, and script execution from writable paths as confirmation-worthy.
- Keep allowlisted harmless forms explicit, for example `python --version`, `python -V`, or `node --version`.

### 3. `insert_edit_into_file` approval preview can differ from execution

Relevant file:

- `nexus/tools/builtin/smart_edit.py`

`get_confirmation()` diffs the old file against the raw `code` argument, not necessarily the exact final file content that `_run()` will produce. In no-marker mode, if fuzzy replacement cannot find a good match, the tool appends the code block to the end of the file.

Impact:

- The user can approve one diff while a different mutation is applied.
- A failed fuzzy match can silently append duplicate or misplaced code.
- The low fuzzy threshold increases the chance of surprising edits.

Recommendation:

- Build approval previews by running the same transformation logic used during execution, without writing.
- Remove the append-on-fuzzy-failure fallback.
- Require explicit insertion markers, exact anchors, or an explicit insert mode.
- Increase match confidence requirements for mutation tools.

### 4. `apply_patch` is not transactional

Relevant file:

- `nexus/tools/builtin/patch.py`

`apply_patch` applies file changes sequentially. If an early file succeeds and a later file or hunk fails, the tool returns an error but leaves the earlier mutation on disk.

Impact:

- A failed patch can leave the workspace in a partially changed state.
- The agent may interpret the tool result as a failed operation while the filesystem is already modified.

Recommendation:

- Dry-run all file and hunk operations before writing anything.
- Either make patch application transactional or return a clear partial-mutation result that names already-mutated files.
- Add `get_confirmation()` so approval shows the actual patch/diff preview.

### 5. Workspace plugin loading executes arbitrary Python at startup

Relevant files:

- `nexus/extensions/plugins.py`
- `nexus/app.py`

Plugin discovery imports Python modules from configured plugin roots, including workspace-local locations, by executing module code. Plugins are enabled by default unless explicitly disabled.

Impact:

- Opening an untrusted workspace can execute arbitrary local Python code during initialization.
- This is more sensitive than normal tool execution because it happens before an agent turn and outside the usual per-tool approval model.

Recommendation:

- Add a trusted-workspace gate for workspace plugin execution.
- Consider disabling workspace plugin auto-load by default.
- Prefer manifest-first discovery and require explicit enablement before importing plugin Python.

## Sub-Agent and Scope Findings

### 6. Built-in sub-agent names conflict with repository invariants

Relevant files:

- `nexus/runtime/agent_scope.py`
- `nexus/tools/subagents.py`
- `nexus/config/upgrade.py`
- `nexus/prompts/system.py`
- `nexus/cli/init.py`
- `nexus/ui/textual_rendering.py`

The repository instructions define built-in cognitive sub-agent names as:

- `planning_analysis`
- `execution`
- `review`
- `verification`

The live code defines and prefers:

- `explorer`
- `coding`
- `code_reviewer`
- `impact_analyzer`

`nexus/config/upgrade.py` also maps the former names to the latter names. UI and prompt code contain both naming families.

Impact:

- Config upgrades, docs, prompts, UI, and scope logic can disagree about canonical names.
- The agent may expose or prefer different tools than a maintainer expects from the repository invariants.

Recommendation:

- Pick one canonical built-in sub-agent naming set and update all code paths to match it.
- If the new names are intentional, update the repository invariant and migration docs.
- If the invariant is authoritative, revert built-in declarations and migrations to `planning_analysis`, `execution`, `review`, and `verification`.

### 7. Sub-agent scoping logic is duplicated outside `agent_scope.py`

Relevant files:

- `nexus/runtime/agent_scope.py`
- `nexus/tools/subagents.py`
- `nexus/runtime/agent.py`
- `nexus/config/upgrade.py`
- `nexus/prompts/system.py`

The instructions say all supervisor/sub-agent tool, skill, and MCP visibility logic should live in `agent_scope.py`. The current code repeats related logic in multiple files:

- Built-in sub-agent definitions are hardcoded in `tools/subagents.py`.
- Built-in MCP expansion exists in `_with_builtin_mcp_tools()`.
- Supervisor tool priority and sub-agent preference text are hardcoded in `runtime/agent.py`.
- Name migration is hardcoded in `config/upgrade.py`.

Impact:

- A future change to scoping can miss one of these copies.
- Built-in sub-agent MCP access can drift from the centralized `subagent_tool_names()` rules.

Recommendation:

- Move all built-in name metadata and scope expansion through `agent_scope.py`.
- Make `tools/subagents.py` consume exported definitions instead of owning built-in scope behavior.
- Keep prompt/UI display metadata separate from access-control decisions.

### 8. Built-in sub-agents can receive broad MCP access by default

Relevant files:

- `nexus/runtime/agent_scope.py`
- `nexus/tools/subagents.py`

`subagent_tool_names()` allows all active MCP tools when a sub-agent has no explicit MCP allowlist or is a built-in sub-agent. `tools/subagents.py` also appends MCP tools to built-ins separately.

Impact:

- A built-in sub-agent can gain access to all active MCP tools, including tools the user may have expected to remain supervisor-only.
- Because all MCP tools are treated as mutating by the adapter, this is noisy for benign tools but still broad for dangerous tools.

Recommendation:

- Require explicit MCP scope for built-ins, or split read-only and mutating MCP tools.
- Remove duplicate MCP expansion outside `agent_scope.py`.
- Surface the effective MCP scope in `/agents` and approval prompts.

## Tooling and Mutation Findings

### 9. MCP config accepts transports the client does not implement

Relevant files:

- `nexus/config/defaults.py`
- `nexus/config/loader.py`
- `nexus/tools/mcp.py`

Config validation accepts HTTP-style MCP transports, but `MCPClient._connect()` only supports stdio and raises for other transports.

Impact:

- A user can configure a valid-looking MCP server that always fails at runtime.
- Startup records a failure instead of giving an earlier actionable config error.

Recommendation:

- Either implement HTTP transports or reject unsupported transports during config validation.
- Include the unsupported transport name in user-facing diagnostics.

### 10. Every MCP tool is treated as mutating

Relevant file:

- `nexus/tools/mcp.py`

`MCPToolAdapter` sets `is_mutating=True` for every remote tool.

Impact:

- Read-only MCP tools require unnecessary approval.
- Users may become habituated to approvals, reducing the safety value of real mutating prompts.

Recommendation:

- Use MCP annotations or local policy to distinguish read-only, idempotent, and mutating tools.
- Fall back to mutating when unknown, but allow explicit config overrides.

### 11. Legacy filesystem tools remain in code

Relevant file:

- `nexus/tools/filesystem.py`

`ModifyFileTool` and `ReplaceTextTool` still exist even though legacy compatibility tools should not be part of the default core registry.

Impact:

- They are not currently registered by default, so this is not an active registry violation.
- They add search noise and increase the chance that future code reintroduces deprecated tools.

Recommendation:

- Remove these classes if no external compatibility layer still imports them.
- If retained, mark them clearly as non-default legacy tools and add a guard test around default registration.

### 12. Grep output is insufficiently bounded

Relevant file:

- `nexus/tools/builtin/grep.py`

The grep tool limits traversed files, but match output can still become very large if many lines match.

Impact:

- A single repetitive file can flood the model context.
- Large grep outputs make tool traces and UIs harder to use.

Recommendation:

- Add `max_results` and max-character truncation.
- Report truncation explicitly with enough detail for the agent to refine the query.

## Provider Integration Findings

### 13. OpenAI-compatible and Ollama streaming are buffered

Relevant files:

- `nexus/integrations/openai_compatible.py`
- `nexus/integrations/ollama.py`

Both integrations collect stream events into a list during an attempt, then yield them only after the attempt finishes. Anthropic and Gemini stream more directly.

Impact:

- UI streaming is delayed for OpenAI-compatible and Ollama providers.
- Provider behavior is inconsistent even though the runtime consumes a common event stream.

Recommendation:

- Yield deltas as they arrive while retaining enough state to retry only before any user-visible output is emitted.
- If retry-after-partial-output is not supported, make that explicit and avoid buffering successful streams.

### 14. Streaming cancellation can leave provider requests running

Relevant files:

- `nexus/integrations/openai_compatible.py`
- `nexus/integrations/ollama.py`

The streaming readers run in background daemon threads. If the consumer stops early, the HTTP request may continue until the provider returns or times out.

Impact:

- Cancelled turns can waste network and provider resources.
- Shutdown behavior can be harder to reason about.

Recommendation:

- Propagate cancellation to the HTTP response/context.
- Use a bounded queue and close the response when the async consumer stops.

### 15. Generic API key fallback can select the wrong credential

Relevant files:

- `nexus/integrations/openai_compatible.py`
- `nexus/integrations/anthropic.py`
- `nexus/integrations/gemini.py`

Provider adapters can fall back to a generic `API_KEY`.

Impact:

- Convenient for demos, but easy to misconfigure in multi-provider environments.
- A provider can receive the wrong secret.

Recommendation:

- Warn when a provider-specific key is absent but generic `API_KEY` is used.
- Prefer provider-specific environment variables in generated config examples.

## Config, Memory, and Prompt Findings

### 16. Non-strict config loading can hide broken local config

Relevant files:

- `nexus/config/loader.py`
- `nexus/app.py`

The runtime uses non-strict config loading and records warnings. That helps recovery, but broad exception handling can continue with defaults after a malformed config.

Impact:

- Users may believe local config is active when Nexus has fallen back.
- Subtle behavior changes can come from ignored config files.

Recommendation:

- Make malformed local config highly visible in the UI and headless output.
- Consider strict mode for explicit config-editing commands.

### 17. Memory injection is unbounded and non-relevant

Relevant files:

- `nexus/runtime/repl_state.py`
- `nexus/prompts/__init__.py`
- `nexus/memory/store.py`

All memory entries are loaded into the system prompt.

Impact:

- Long-running memory can bloat every request.
- Irrelevant memory can influence unrelated tasks.

Recommendation:

- Add memory limits, relevance filtering, or recency windows.
- Show truncation behavior clearly in prompt diagnostics.

### 18. Corrupt memory files can be silently overwritten

Relevant file:

- `nexus/memory/store.py`

Memory JSON loading catches broad exceptions and returns an empty list.

Impact:

- A corrupt memory file can look like empty memory.
- A later save can overwrite the corrupt file, losing recoverable data.

Recommendation:

- Rename corrupt files aside before writing new memory.
- Emit a visible warning and avoid overwriting until recovery is attempted.

## Slash Command Findings

### 19. Some slash commands can crash on invalid input

Relevant file:

- `nexus/runtime/slash_commands.py`

Examples include direct enum/int parsing in handlers such as `/mode` and `/history`.

Impact:

- A mistyped command can raise instead of returning a friendly usage error.

Recommendation:

- Validate user input before enum/int conversion.
- Have the router catch handler exceptions and render command errors consistently.

### 20. Config writes use mixed serialization paths

Relevant files:

- `nexus/runtime/slash_commands.py`
- `nexus/config/editor.py`

Slash commands contain ad hoc TOML serialization helpers, while config editing elsewhere uses `tomlkit` and atomic replacement.

Impact:

- Comments and formatting can be lost inconsistently.
- Future schema changes must be reflected in multiple writers.

Recommendation:

- Route slash-command config mutations through the config editor layer.
- Keep atomic writes and formatting behavior consistent.

## Performance and Maintenance Debt

### 21. Large modules are absorbing too many responsibilities

Large files:

- `nexus/runtime/agent.py`
- `nexus/runtime/slash_commands.py`
- `nexus/ui/textual_app.py`
- `nexus/ui/textual_rendering.py`
- `nexus/runtime/turn_runner.py`

Impact:

- Correctness-sensitive logic such as approval, history compaction, event streaming, tool execution, and UI formatting are harder to isolate.
- Small behavior changes require reading very large files.

Recommendation:

- Extract narrow helpers only where boundaries already exist: approval policy, history persistence, provider event normalization, and slash-command config mutations.
- Avoid broad refactors until the high-risk issues above are covered by tests.

### 22. Code search and LSP helpers rebuild indexes repeatedly

Relevant files:

- `nexus/tools/builtin/code_search.py`
- `nexus/tools/builtin/lsp.py`

These tools scan and parse Python files per call.

Impact:

- Repeated calls in a large repo waste time.
- Agent loops that rely on search/navigation can become slow.

Recommendation:

- Cache indexes per workspace and invalidate by file mtime or a cheap content fingerprint.
- Keep cache lifetime bounded to the process or turn.

### 23. Deterministic supervisor routing appears underused

Relevant files:

- `nexus/runtime/supervisor_routing.py`
- `nexus/prompts/system.py`

The routing classifier exists, but advanced-mode execution appears to rely primarily on prompt guidance and exposed sub-agent tools.

Impact:

- Maintainers may assume deterministic routing behavior exists when it is mostly advisory.
- The additional module increases conceptual surface area without clear runtime effect.

Recommendation:

- Either wire deterministic routing into the supervisor flow or mark it as prompt-support/advisory code.
- Add tests around whatever behavior is intended.

### 24. `_parallel_tool_execution_enabled()` currently ignores context

Relevant file:

- `nexus/runtime/agent.py`

The helper accepts `RunContext` but currently returns a constant.

Impact:

- The parameter suggests policy hooks that are not active.
- Future maintainers may miss that parallelism is effectively always enabled.

Recommendation:

- Remove the unused parameter or implement context-aware policy.
- Consider disabling parallel mutation unless non-overlapping paths are proven.

## What Is Working Well

- The main agent runtime is event-driven and structurally compatible with deterministic tool-call resume.
- Provider-safe message persistence is handled with care in normal turns.
- Config merging is centralized in `nexus/config/loader.py`, with schema upgrade handling separated in `nexus/config/upgrade.py`.
- Built-in write tools generally enforce workspace boundaries and avoid `.nexus/`, `reference_code/`, and `workspace/` writes.
- Tool registries and skill registries are clear enough to reason about.
- The `SubAgentTool` direct path correctly reuses the outer approval manager/callback instead of bypassing approval entirely.
- `write_file` and `edit_file` have better confirmation previews and path checks than the older compatibility-style filesystem tools.

## Suggested Fix Order

1. Tighten turn-wide mutating approval so it cannot broaden across tool, risk, or path scope.
2. Reclassify shell interpreter inline execution as confirmation-worthy.
3. Fix `insert_edit_into_file` preview/execution mismatch and remove append-on-fuzzy-failure.
4. Make `apply_patch` dry-run or transactional, and add confirmation previews.
5. Resolve the canonical built-in sub-agent naming mismatch.
6. Move duplicated sub-agent/MCP scope decisions back behind `agent_scope.py`.
7. Add a trusted-workspace gate for workspace plugin imports.
8. Align MCP transport validation with implemented transports.
9. Stream OpenAI-compatible and Ollama responses incrementally.
10. Add bounded output/caching for search/navigation tools.

## Residual Risk

This was a static review plus compilation check, not a behavioral test run. The highest confidence findings are the approval broadening, interpreter classification, smart edit preview mismatch, patch partial-mutation behavior, and sub-agent naming/scope drift because they follow directly from the reviewed control flow.
