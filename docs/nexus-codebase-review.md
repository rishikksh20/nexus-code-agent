# Nexus Codebase Review

Review date: 2026-05-18

Scope: live Nexus codebase only. `reference_code/` was intentionally excluded per repository instructions.

## Executive Summary

Nexus is now architecturally centered on a clean single-agent turn loop with optional cognitive sub-agent tools. The strongest part of the design is the approval invariant: user-facing approval lives in `nexus/runtime/turn_runner.py`, while `Agent.run()` remains event-driven and can resume exact pending tool calls through `resume_tool_calls`.

The biggest problem is not the core loop. The biggest problem is contract drift: some tests and docs still describe legacy tool/module surfaces such as `WriteFileTool`, `nexus.tools.filesystem`, and `nexus.runtime.sandbox` that no longer exist. As of this review, `uv run pytest` does not collect successfully.

Verification performed:

```bash
uv run pytest
# result: collection stops with 6 import errors in legacy tool/module tests

uv run pytest tests/test_config.py tests/test_tools.py tests/test_slash_commands.py tests/test_orchestration.py
# result: 99 passed

uv run python -m compileall -q nexus
# result: passed
```

## Concept Flow

The intended Nexus flow is:

1. `nexus/app.py` loads config, initializes `.nexus/`, wires hooks, registers core/plugin/MCP/sandbox/sub-agent tools, creates the model client, and dispatches interactive or headless execution.
2. `RuntimeSession.create()` creates `ReplState` with session storage, memory, skills, tool registry, MCP servers, and approval policy.
3. `run_repl()` or `run_headless()` appends the user message, begins a new approval turn, and calls `run_orchestrated_turn()`.
4. `run_orchestrated_turn()` is currently a pass-through to `run_agent_turn()`. Advanced mode is handled by exposing cognitive sub-agent tools, not by a separate scheduler.
5. `ReplState.prepare_turn()` builds the system prompt, sanitizes and prunes history, compacts when needed, and creates `ToolExecutionContext`.
6. `Agent.run()` streams provider events, builds assistant messages, validates tool calls, asks `PermissionChecker`, executes tools, and emits typed `AgentEvent`s.
7. When approval is needed, `Agent.run()` emits `CONFIRMATION_REQUESTED` and returns.
8. `run_agent_turn()` asks the user, records approval/refusal/clarification in `ApprovalManager`, then resumes exact approved tool calls with `Agent.run(..., resume_tool_calls=...)`.
9. `ReplState.apply_events()` persists only provider-safe assistant/tool message pairs and saves the session.

The key invariant is provider-safe ordering: an assistant message with `tool_calls` should not be persisted unless matching tool result messages are also present.

## Architecture Map

- `nexus/app.py`: top-level runtime orchestration, provider construction, registry/resource setup, dispatch, teardown.
- `nexus/runtime/agent.py`: provider stream consumption, tool-call validation, permission evaluation, tool execution, loop detection, event emission.
- `nexus/runtime/turn_runner.py`: shared approval-aware turn runner, approval prompt parsing, exact pending-call resume, provider-safe event commits.
- `nexus/runtime/repl.py` and `nexus/cli/headless.py`: user interaction wrappers around the shared turn runner.
- `nexus/runtime/repl_state.py`: prompt assembly, history preparation, compaction/pruning, context metadata, durable history updates.
- `nexus/runtime/orchestration.py`: now a thin compatibility wrapper around `run_agent_turn()`.
- `nexus/tools/`: tool protocol, registry, MCP adapters, core tools, cognitive sub-agent tool registration.
- `nexus/security/`: approval state, approval policy, shell classifier, permission rules.
- `nexus/config/`: defaults, TOML/env/CLI merge, validation, legacy config normalization.
- `nexus/integrations/`: fake, OpenAI-compatible, Ollama, Anthropic, Gemini adapters.
- `nexus/ui/`: Rich terminal UI and Textual UI.
- `nexus/hooks/` and `nexus/observability/`: lifecycle hooks, JSON logging, metrics, audit trail.
- `nexus/memory/`, `nexus/skills/`, `nexus/sandbox/`: optional persistent memory, skill loading, Docker sandbox and cognitive sub-agent machinery.

## Strengths

- Approval callbacks are centralized in `run_agent_turn()`, not reintroduced into `Agent.run()`.
- Approved tool execution resumes exact `ToolCall` objects, avoiding provider-regenerated approval loops.
- `apply_events_to_messages()` skips assistant tool-call messages unless all matching tool results exist.
- `PermissionChecker` blocks plan-mode mutations and keeps high/dangerous bash commands confirmation-gated even in auto mode.
- Core tools are small, separately testable classes with JSON schemas and mutating flags.
- Provider-specific wire formats are isolated behind adapters.
- Textual and line-oriented REPLs share the same runtime path.
- Context compaction and tool-output pruning are centralized in `ReplState.prepare_turn()`.
- Config upgrade code explicitly removes deprecated advanced-mode keys rather than silently preserving stale behavior.

## Findings

### Critical: Test Suite Contract Is Broken

`uv run pytest` currently fails during collection with 6 import errors:

- `WriteFileTool` is imported by `tests/test_agent.py`, `tests/test_cli.py`, `tests/test_hooks.py`, and `tests/test_repl.py`, but `nexus/tools/builtin/__init__.py` exports only canonical tools.
- `nexus.tools.filesystem` is imported by `tests/test_filesystem_tools.py`, but that module is absent.
- `nexus.runtime.sandbox` is imported by `tests/test_sandbox.py`, but sandbox code now lives under `nexus/sandbox/`.
- Legacy orchestration tests previously imported removed planning helpers from `nexus.runtime.orchestration`, but that module is now only a pass-through.

The advanced-mode contract tests now pass in the focused subset. The canonical config has `agent_mode` and `delegation_subagents`.

Recommendation: choose one contract and make tests match it. Given the live code and config upgrade path, the cleaner route is to update/delete stale legacy tool tests and add focused tests for cognitive sub-agent tools.

### High: Docs Describe Removed Modules And Old Flow

The README still lists nonexistent paths such as `nexus/runtime/sandbox.py`, `nexus/tools/filesystem.py`, and `nexus/prompts/compression.py`. `docs/nexus-codebase-context.md` should continue to describe `run_orchestrated_turn()` as a thin pass-through to the shared turn runner.

Recommendation: update README and `docs/nexus-codebase-context.md` to reflect the current architecture: cognitive sub-agent tools, the shared turn runner, and no compatibility filesystem module.

### High: Advanced Mode Needs One Mental Model In The Repo

Current code model:

- `agent_mode = "basic"` means normal single-agent execution.
- `agent_mode = "advanced"` means the supervisor can see `subagent_*` tools.
- Built-in cognitive tools are registered from `nexus/tools/subagents.py`.
- Custom specialists come from `delegation_subagents`.

Any docs or tests that imply a second runtime model make future changes risky because contributors will not know which model is canonical.

Recommendation: keep the migration note short and keep tests focused on the cognitive sub-agent contract. Keep `context_state.py` only if packet summaries remain part of the sub-agent design.

### Medium: Permission Policy Is Partly Semantic, Partly Name-Based

Tools expose `ToolKind` and `is_mutating`, but `PermissionChecker` still special-cases `bash`, `write_file`, and `memory`. Its hard path policy only covers `write_file`; other mutating path tools enforce workspace and `.nexus` denial inside their own `execute()` methods.

This is not an immediate bypass because `edit`, `write_file`, and `apply_patch` have execution-time checks. The weakness is consistency: approvals, audit records, and risk labels see `edit`/`apply_patch` as generic medium-risk mutations even when they target sensitive paths and will later be refused.

Recommendation: move path-argument metadata into tools, or expand `_path_policy()` to handle all first-party path-mutating tools before approval/UI/audit.

### Medium: Audit Classification Is Stale

`observability/audit.py` marks only `write_file` as high and `bash` as critical. It treats `edit`, `insert_edit_into_file`, `apply_patch`, `memory`, `todos`, formatter commands, MCP mutations, and sandbox commands as low by default.

Recommendation: classify by `ToolKind`, `is_mutating`, shell risk, and confirmation preview instead of a short hard-coded tool-name list.

### Resolved: Cognitive Sub-Agent Prompt Is Not Duplicated

`definition.goal_prompt` now stays in the sub-agent system prompt as role instructions. The user message passed to the inner agent contains only the task-specific instructions from the supervisor.

### Resolved: `needs_clarification` Sub-Agent Results Are Marked As Errors

`SubAgentTool` now treats `needs_clarification` as an error status. The structured envelope still carries `recommended_next_action: "ask_user"`, but the outer tool result is no longer successful, so the supervisor cannot accidentally treat a blocked sub-agent as completed.

### Medium-Low: Legacy Cleanup Is Still Ongoing

Removed in the current cleanup pass:

- Unrouted `/multi-agent` slash-command helper functions.
- The unused `_render_inner_events()` wrapper in `sandbox/agent_tool.py`.
- The unused module-level Ollama `_chat_url(base_url)` helper.
- The unused `ReplState.disabled_tools` field.
- Legacy context-state projections for removed shared-state planner metadata.

Recommendation: keep compatibility only where tests and docs say why it exists.

### Medium-Low: Config Serialization Is Functional But Loses Formatting

Slash commands that update TOML load it into a plain dict and write a simple top-level TOML file. This loses comments and original formatting.

Recommendation: acceptable for now, but if config editing becomes common UX, use a comment-preserving TOML writer or keep generated config writes isolated.

### Low: Provider Adapters Need Continued Edge Coverage

Provider boundaries are well isolated. The risk is edge behavior:

- malformed non-stream OpenAI-compatible tool arguments can raise during `json.loads`;
- streaming tool-call assembly differs across OpenAI-compatible, Ollama, Anthropic, and Gemini;
- usage accounting is partial for some streaming paths.

Recommendation: keep small adapter tests for malformed tool args, mixed text/tools, partial tool-call chunks, no usage payloads, and transient errors.

### Low: Textual UI Buffers Assistant Streaming

The Textual UI collects assistant deltas and renders the Markdown at completion, unlike the line-oriented UI which can stream text. This is probably a UX tradeoff, but the config name `stream_output` implies live text streaming.

Recommendation: either document this difference or implement incremental Textual rendering.

## Current Tool Surface

Default first-party tools registered by `nexus/tools/registry.py`:

- `get_time`
- `read_file`
- `write_file`
- `edit`
- `insert_edit_into_file`
- `apply_patch`
- `glob`
- `grep`
- `list_dir`
- `lsp`
- `code_index`
- `semantic_search`
- `git_status`
- `git_diff`
- `run_tests`
- `run_python_check`
- `run_formatter`
- `bash`
- `memory`
- `todos`
- `web_fetch`
- `web_search`

Advanced mode adds built-in cognitive tools:

- `subagent_planning_analysis`
- `subagent_execution`
- `subagent_review`
- `subagent_verification`

Compatibility aliases such as `modify_file` and `replace_text` are not present in the live default tool surface.

## Current Provider Surface

Valid providers:

- `anthropic`
- `fake`
- `gemini`
- `mistral`
- `openai`
- `openai-compatible`
- `ollama`

Provider construction is centralized in `NexusApp._build_model_client()`.

## Suggested Remediation Order

1. Fix the test contract first. Delete or rewrite tests for removed legacy tool modules, then add tests for current cognitive sub-agent behavior.
2. Update README and `docs/nexus-codebase-context.md` so they no longer advertise nonexistent modules and old orchestration.
3. Clean dead slash-command helper code and unused compatibility helpers.
4. Generalize permission and audit classification around tool metadata.
5. Tighten sub-agent status semantics and remove duplicated goal prompts.
6. Add provider edge-case tests for malformed tool arguments and streaming tool-call assembly.

## Bottom Line

The live core architecture is coherent: bootstrap in `app.py`, shared turn execution in `turn_runner.py`, event-driven tool execution in `Agent`, durable history in `ReplState`, and optional cognitive sub-agents layered through the tool registry.

The repo is not currently healthy as a project artifact because the tests and docs still describe an older architecture. Fixing that drift should be the next priority before adding more runtime features.
