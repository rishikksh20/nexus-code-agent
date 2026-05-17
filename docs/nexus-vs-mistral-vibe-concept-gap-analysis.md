# Nexus vs. Mistral Vibe Concept Gap Analysis

Last updated: 2026-05-17

This note compares the live Nexus codebase with `./workspace/mistral-vibe` on the concept and product-maturity front. It intentionally does not use `reference_code/`.

The short version: Nexus has a cleaner core agent invariant around centralized approvals, deterministic approved-call resume, provider-safe history persistence, explicit sandboxing, and a focused first-party tool registry. Mistral Vibe is much more mature as a complete CLI product: richer TUI, protocol adapter, session recovery, agent profiles, tool permissions, autocompletion, rewind, packaging, and broad test coverage around user experience.

The goal should not be to clone Vibe. The useful move is to borrow concepts that strengthen Nexus while preserving the Nexus runtime invariants:

- Keep approval callbacks centralized in `run_agent_turn()`.
- Keep `Agent.run()` event-driven and resume approved pending calls via `resume_tool_calls`.
- Preserve provider-safe history ordering.
- Do not re-add legacy compatibility tools to the default core registry.

## Compared Areas

Nexus references:

- `nexus/runtime/agent.py`
- `nexus/runtime/turn_runner.py`
- `nexus/runtime/repl_state.py`
- `nexus/runtime/slash_commands.py`
- `nexus/security/permissions.py`
- `nexus/security/manager.py`
- `nexus/tools/base.py`
- `nexus/prompts/system.py`
- `docs/nexus-codebase-context.md`

Mistral Vibe references:

- `workspace/mistral-vibe/README.md`
- `workspace/mistral-vibe/vibe/core/agent_loop.py`
- `workspace/mistral-vibe/vibe/core/middleware.py`
- `workspace/mistral-vibe/vibe/core/tools/base.py`
- `workspace/mistral-vibe/vibe/core/tools/manager.py`
- `workspace/mistral-vibe/vibe/core/tools/permissions.py`
- `workspace/mistral-vibe/vibe/core/tools/builtins/bash.py`
- `workspace/mistral-vibe/vibe/core/tools/builtins/search_replace.py`
- `workspace/mistral-vibe/vibe/core/tools/builtins/task.py`
- `workspace/mistral-vibe/vibe/core/tools/builtins/ask_user_question.py`
- `workspace/mistral-vibe/vibe/core/agents/models.py`
- `workspace/mistral-vibe/vibe/core/agents/manager.py`
- `workspace/mistral-vibe/vibe/core/rewind/manager.py`
- `workspace/mistral-vibe/vibe/core/session/session_logger.py`
- `workspace/mistral-vibe/vibe/acp/acp_agent_loop.py`
- `workspace/mistral-vibe/vibe/cli/textual_ui/`
- `workspace/mistral-vibe/vibe/core/autocompletion/`

## Nexus Strengths To Preserve

Nexus should keep these architectural choices even if it adopts Vibe-like features:

- Central approval ownership in `nexus/runtime/turn_runner.py`, not inside `Agent.run()`.
- Deterministic resume of exact approved tool calls with `resume_tool_calls`.
- Provider-safe persistence: assistant messages with tool calls are committed only with matching tool results.
- Conservative shell and file-write policy in `nexus/security/`.
- Tool semantics through `ToolKind`, `is_mutating`, `get_confirmation()`, and rich confirmation previews.
- Sub-agent context isolation through Nexus' cognitive sub-agent tools and context packets.
- Docker sandbox as a first-class execution option.

Vibe often keeps approval callbacks directly on `AgentLoop` and lets tools receive user-input callbacks through `InvokeContext`. Nexus can still add equivalent product features while routing all user-facing confirmations and questions through the turn runner.

## Highest-Value Gaps

### 1. First-Class Agent Profiles

Vibe has named profiles such as `default`, `plan`, `chat`, `accept-edits`, `auto-approve`, `explore`, and `lean`. Each profile can override tools, model settings, prompt IDs, permission behavior, and whether it is a primary agent or subagent.

Nexus currently has execution modes (`plan`, `default`, `auto`) plus configurable delegation subagents, but not a full profile layer. Adding profiles would make Nexus easier to operate without multiplying CLI flags.

Recommended Nexus shape:

- Add an `AgentProfile` model with `name`, `description`, `safety`, `agent_type`, and config overrides.
- Load profiles from builtin, global, and workspace roots.
- Keep `ExecutionMode` as the low-level permission mode, but let profiles choose it and override tool availability.
- Add `/agent` or extend `/mode` to switch profiles.
- Ensure profile switching rebuilds the system prompt and tool schemas without changing approval ownership.

Priority: high.

### 2. Turn Middleware Pipeline

Vibe has a small but powerful middleware pipeline for before-turn policy:

- turn limits
- price limits
- context warnings
- auto-compaction triggers
- read-only reminders for plan/chat modes
- injected system/user notices

Nexus has related logic spread across `ReplState.prepare_turn()`, `Agent._agentic_loop()`, slash commands, and config. A middleware layer would reduce branching pressure in the runtime and make features easier to test.

Recommended Nexus shape:

- Add `ConversationMiddleware.before_turn(context) -> MiddlewareResult`.
- Support actions: `continue`, `stop`, `compact`, `inject_message`.
- Move context warning, max turn/cost guardrails, mode reminders, and headless behavior into middleware.
- Keep actual tool confirmation handling in `run_agent_turn()`.

Priority: high.

### 3. Granular Per-Tool Permissions

Vibe tools can resolve per-invocation permissions. The bash tool parses shell commands with tree-sitter, builds required permission scopes such as command pattern and outside-directory access, and supports allowlists, denylists, and sensitive patterns.

Nexus has a solid coarse permission model, but much of it is tool-name special casing in `PermissionChecker`. Useful missing pieces are per-tool allowlists, denylists, sensitive patterns, and structured required-permission labels.

Recommended Nexus shape:

- Add optional `Tool.resolve_permission(arguments, context) -> PermissionContext`.
- Add config shape like `tools.<name>.permission`, `allowlist`, `denylist`, `sensitive_patterns`.
- Let tools provide granular permission scopes while `PermissionChecker` stays the final evaluator.
- Reuse the existing `ApprovalManager`, adding session-pattern approvals only after a user explicitly approves them.

Priority: high.

### 4. Rewind With File Checkpoints

Vibe snapshots files before mutating tools, lets the user rewind to a prior user message, optionally restores file contents, truncates messages, and forks the session.

Nexus has sessions and compaction, but no user-facing rewind or file snapshot recovery. This would be very valuable for a coding agent, especially when edits/tests run for a while and the user wants to back up one decision.

Recommended Nexus shape:

- Add optional `Tool.get_file_snapshot()` or reuse `get_confirmation().affected_paths` to capture pre-mutation bytes.
- Create checkpoints at the start of each user turn.
- Add `/rewind` to list user messages and restore conversation-only or conversation-plus-files.
- Fork to a new session after rewind so old logs remain intact.
- Never write directly into `.nexus/` except through session/storage APIs.

Priority: high.

### 5. Better Interactive Clarification

Vibe has an `ask_user_question` tool that can ask one or more choice-based questions with an automatic free-text option. This is useful for design decisions, refactors, and ambiguous tasks.

Nexus can currently request missing tool arguments as a clarification through confirmation events, but it does not expose a general structured user-question tool to the model.

Recommended Nexus shape:

- Add a `request_user_input` or `ask_user_question` built-in tool.
- Route it through the same event/turn-runner path as confirmations.
- Disable it in headless mode or return a deterministic "user unavailable" tool result.
- Keep a strict schema: short headers, 2-4 choices, optional free text.

Priority: high.

### 6. Tool Result Models And Tool UI Metadata

Vibe tools are generic over Pydantic args, result, config, and state models. Tools can stream progress events and provide UI display adapters. Nexus tools use JSON schemas and string `ToolResult.output` plus metadata.

Nexus does not need to fully adopt Vibe's generic tool base, but typed result objects and display metadata would improve UI rendering, logs, tests, and future protocol adapters.

Recommended Nexus shape:

- Add optional `result_schema` or typed result adapters without breaking existing `ToolResult`.
- Add optional `ToolUIData` methods for call summary, result summary, status text, and warnings.
- Add `ToolStreamEvent` support for long-running tools and subagents.
- Keep string output as the canonical model-facing payload.

Priority: medium-high.

### 7. Same-Turn Concurrent Tool Execution

Vibe executes multiple resolved tool calls concurrently and yields events as they complete. Nexus currently executes tool calls sequentially, though it already blocks same-file mutation conflicts.

Concurrency would help read-heavy turns and independent tasks, but it must be added carefully because Nexus approval and provider-history invariants are stricter.

Recommended Nexus shape:

- Start with concurrent execution for read-only tools only.
- Allow mutating concurrency only for tools with disjoint `affected_paths`.
- Preserve output ordering in model history or explicitly record call IDs so provider order remains valid.
- Keep approval prompts deterministic: if any call needs confirmation, pause before executing unapproved mutating work in that batch.

Priority: medium.

### 8. ACP / IDE Protocol Adapter

Vibe has an ACP agent implementation with initialize, auth, session create/load/fork/close, set model, set mode, set config option, available commands, usage updates, tool updates, and replay of session history. It also ships editor distribution metadata.

Nexus is CLI-first. An ACP or similar adapter would make Nexus usable from IDEs without rewriting the core runtime.

Recommended Nexus shape:

- Keep the current `Agent`, `ReplState`, and `run_agent_turn()` as the core.
- Add an adapter package that maps protocol events to Nexus events.
- Expose session list/load/fork, mode/profile switching, model switching, tool updates, usage updates, and cancellation.
- Add tests before shipping; protocol edge cases are numerous.

Priority: medium-high, after core profiles and middleware.

### 9. Richer TUI And Input UX

Vibe's Textual UI includes path completion, slash-command completion, file indexing, external editor support, tool output toggles, debug console, model/thinking pickers, approval panels, context progress, session picker, rewind UI, and voice/narrator hooks.

Nexus' REPL is simpler and easier to maintain. The biggest UX wins to borrow are not the whole TUI; they are input ergonomics.

Recommended Nexus shape:

- Add `@path` insertion with ignore-aware file indexing.
- Add slash-command completion.
- Add external editor input for long prompts.
- Add context progress display and compact warnings.
- Add session picker/resume UI.
- Consider Textual only after the underlying event model is stable.

Priority: medium.

### 10. Scratchpad Directory

Vibe creates a session-scoped scratchpad directory, auto-allows it, and shares it with subagents. This gives agents a safe place for temporary scripts, intermediate files, and drafts that do not belong in the project.

Nexus currently relies on workspace files, shell temp paths, or tool outputs. A scratchpad would reduce clutter and make subagent coordination cleaner.

Recommended Nexus shape:

- Create a session scratchpad under a runtime-managed temp directory, not arbitrary project state.
- Surface the scratchpad path in system prompt context.
- Allow reads/writes inside it without normal workspace write prompts.
- Clean it up according to session retention policy.

Priority: medium-high.

## Useful Tooling Improvements

### Search/Replace Diagnostics

Vibe's `search_replace` tool supports block-based edits, fuzzy match diagnostics, context snippets, line-ending hints, encoding preservation, optional backups, and sensitive file patterns. Nexus' `edit` and `apply_patch` are functional, but error messages can be improved.

Add fuzzy diagnostics when `old_string` is not found, preserve file encoding when practical, and include nearby candidate matches in tool errors.

Priority: medium.

### Bash Guardrails

Vibe's bash tool has:

- command-prefix allowlist/denylist
- standalone command denylist
- sensitive commands that always ask
- tree-sitter command extraction
- outside-directory permission detection
- noninteractive environment defaults
- platform-specific command policy

Nexus has risk regexes and blocked commands. The next improvement is not more regexes; it is structured command extraction and per-command permission scopes.

Priority: medium-high.

### Per-Tool Prompt Snippets

Vibe tools can ship prompt markdown files that are included in the system prompt. Nexus has centralized tool guardrails. Per-tool prompt snippets would help complex tools like `apply_patch`, `insert_edit_into_file`, `bash`, and subagents without bloating every tool description.

Priority: medium.

### Remote Tool Refresh And Per-Source Filtering

Vibe can refresh MCP/connectors and filter tools per server/connector. Nexus can inspect and refresh MCP status, but the README-level behavior says the registry is unchanged for a session in some flows.

Recommended Nexus shape:

- Refresh MCP discovery into the active `ToolRegistry`.
- Support per-server disabled tools.
- Preserve the provenance fields already present on `ToolRecord`.

Priority: medium.

## Session, Config, And Observability Gaps

### Session Metadata

Vibe writes append-only `messages.jsonl` plus `meta.json` with title, git commit, branch, username, cwd, stats, config, tools available, agent profile, system prompt, loops, and parent session. Metadata writes are atomic.

Nexus already persists sessions, history, and turn telemetry. Useful additions:

- session title with manual/auto source
- parent session ID for compaction/fork/rewind chains
- git commit and branch at session start
- tools available and config snapshot
- integrity validation for corrupt sessions
- append-only message log option for easier protocol replay

Priority: medium.

### Config Layering And Trust

Vibe has a typed, merge-aware config schema and a trust-aware config-layer abstraction. Nexus has dataclass defaults with local/global TOML merging and upgrade support.

Useful additions:

- field-level merge strategies: replace, concat, union-by-key, shallow merge, conflict
- trust state for project-local config and project instructions
- safer config writes that preserve comments or apply focused patches

Priority: medium.

### Observability

Nexus has JSON logs, metrics, audit trail, hooks, and turn telemetry. Vibe goes further with usage/cost tracking, OpenTelemetry hooks, correlation IDs, context progress, request metadata, and session-level event telemetry.

Useful Nexus additions:

- OpenTelemetry export, as already noted in the README gap
- per-turn cost and token budget summaries
- correlation IDs threaded through model calls, tool calls, hooks, and subagents
- protocol-friendly usage updates

Priority: medium.

## Lower-Priority Product Features

These are mature in Vibe but should not distract Nexus unless the project direction changes:

- Voice mode, transcription, TTS, and narrator summaries.
- Browser auth and first-run onboarding.
- Update notifier and release automation.
- Cloud/remote "teleport" workflow.
- Product telemetry beyond local observability.
- Zed/IDE distribution packaging before protocol stability.
- Scheduled `/loop` recurring prompts. Useful, but less central than rewind, profiles, middleware, and permissions.

## Good Practices Worth Copying

- Heavy initialization can be deferred. Vibe starts the UI quickly and discovers MCP/connectors in the background, then refreshes the system prompt.
- Session writes should be robust against interruption. Atomic metadata and append-only message logs are useful.
- Long-running tools should stream progress events rather than appear frozen.
- UI rendering should be driven by structured events, not ad hoc string parsing.
- Test the user experience, not just the core loop. Vibe has ACP tests, TUI snapshots, autocompletion tests, session migration tests, and e2e CLI tests.
- Tool permissions should be declarative and explainable to the user.
- Project context should include cheap git facts: branch, main branch, clean/dirty summary, and recent commits.
- Dangerous directory warnings are useful when the user runs the agent from home, root, or another broad directory.

## Suggested Nexus Roadmap

### Phase 1: Strengthen Core Ergonomics

- Add turn middleware.
- Add first-class agent profiles.
- Add granular per-tool permission context.
- Add scratchpad support.
- Improve `edit` and `bash` diagnostics.
- Enrich session metadata.

### Phase 2: Recovery And Collaboration

- Add rewind checkpoints and `/rewind`.
- Add structured user-question tool routed through `run_agent_turn()`.
- Add per-tool prompt snippets.
- Add better context warnings and manual compaction UX.

### Phase 3: Product Surface

- Add `@path` completion and slash-command completion.
- Add model/profile picker commands.
- Add protocol adapter groundwork for ACP-like clients.
- Add read-only concurrent tool execution.
- Add tool stream events and UI metadata.

### Phase 4: Full Productization

- Add Textual UI if the simple REPL becomes limiting.
- Add IDE/editor distribution.
- Add snapshot/e2e UI tests.
- Consider voice, onboarding, update notifications, and remote workflows only if Nexus becomes a packaged end-user CLI.

## Concepts To Avoid Or Defer

- Do not move user-facing approval callbacks back into `Agent.run()`.
- Do not let tools ask the user directly; route questions through agent events and the turn runner.
- Do not add product telemetry before there is a clear privacy model.
- Do not make a Textual rewrite the prerequisite for core improvements.
- Do not copy Vibe's cloud-specific or Mistral-specific flows unless Nexus intentionally becomes a branded product.

## Bottom Line

The best Nexus additions from Vibe are not flashy UI features first. They are:

1. Agent profiles.
2. Middleware.
3. Granular permissions.
4. Rewind/checkpoints.
5. Structured user questions.
6. Scratchpad.
7. Better edit/bash diagnostics.
8. Richer session metadata.
9. Protocol-ready event/UI metadata.

Those fit Nexus' current architecture and would make it feel much more mature without weakening the approval and history guarantees that are already a strong differentiator.
