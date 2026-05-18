# Nexus Codebase Review

Review date: 2026-05-18

Scope: live Nexus codebase only. `reference_code/` was intentionally excluded per repository instructions.

## Executive Summary

Nexus is now architecturally centered on a clean single-agent turn loop with optional cognitive sub-agent tools. The strongest part of the design is the approval invariant: user-facing approval lives in `nexus/runtime/turn_runner.py`, while `Agent.run()` remains event-driven and can resume exact pending tool calls through `resume_tool_calls`.

The biggest problem is not the core loop. The biggest problem is contract drift: tests and docs still describe older worker delegation, DAG orchestration, `WriteNoteTool`, `nexus.tools.filesystem`, and `nexus.runtime.sandbox` surfaces that no longer exist. As of this review, `uv run pytest` does not collect successfully.

Verification performed:

```bash
uv run pytest
# result: collection stops with 9 import errors

uv run pytest --ignore=tests/test_agent.py --ignore=tests/test_cli.py --ignore=tests/test_delegation.py --ignore=tests/test_filesystem_tools.py --ignore=tests/test_hooks.py --ignore=tests/test_orchestration.py --ignore=tests/test_repl.py --ignore=tests/test_sandbox.py --ignore=tests/test_tools.py
# result: 157 passed, 6 failed

uv run python -m compileall -q nexus
# result: passed
```

## Concept Flow

The intended Nexus flow is:

1. `nexus/app.py` loads config, initializes `.nexus/`, wires hooks, registers core/plugin/MCP/sandbox/sub-agent tools, creates the model client, and dispatches interactive or headless execution.
2. `RuntimeSession.create()` creates `ReplState` with session storage, memory, skills, tool registry, MCP servers, and approval policy.
3. `run_repl()` or `run_headless()` appends the user message, begins a new approval turn, and calls `run_orchestrated_turn()`.
4. `run_orchestrated_turn()` is currently a pass-through to `run_agent_turn()`. Advanced mode is handled by exposing cognitive sub-agent tools, not by an automatic DAG scheduler.
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
- Config upgrade code explicitly removes deprecated delegation/DAG keys rather than silently preserving stale behavior.

## Findings

### Critical: Test Suite Contract Is Broken

`uv run pytest` currently fails during collection with 9 import errors:

- `WriteNoteTool` is imported by `tests/test_agent.py`, `tests/test_cli.py`, `tests/test_hooks.py`, `tests/test_repl.py`, `tests/test_tools.py`, and `tests/test_delegation.py`, but `nexus/tools/builtin/__init__.py` exports only canonical tools.
- `nexus.tools.filesystem` is imported by `tests/test_filesystem_tools.py` and `tests/test_tools.py`, but that module is absent.
- `nexus.runtime.delegation` is imported by `tests/test_delegation.py`, but worker delegation has been removed.
- `nexus.runtime.sandbox` is imported by `tests/test_sandbox.py`, but sandbox code now lives under `nexus/sandbox/`.
- `tests/test_orchestration.py` imports `AgentRole`, `TaskDAG`, `parse_task_dag`, and related DAG helpers from `nexus.runtime.orchestration`, but that module is now only a pass-through.

After ignoring those stale files, the remaining suite has 157 passing tests and 6 failures. The failures are still contract drift: tests expect `AgentConfig.multi_agent_show_plan`, `multi_agent_max_parallel_tasks`, `multi_agent_max_repair_iterations`, `multi_agent_complexity_threshold`, and `delegation_enabled`, but `AgentConfig` now has `agent_mode` and `delegation_subagents`.

Recommendation: choose one contract and make tests match it. Given the live code and config upgrade path, the cleaner route is to update/delete stale worker/DAG/legacy tool tests and add focused tests for cognitive sub-agent tools.

### High: Docs Describe Removed Modules And Old Flow

The README still lists nonexistent paths such as `nexus/runtime/delegation.py`, `nexus/runtime/sandbox.py`, `nexus/tools/filesystem.py`, and `nexus/prompts/compression.py`. `docs/nexus-codebase-context.md` says `run_orchestrated_turn()` validates a planner DAG, but the current file explicitly says the old DAG scheduler was removed.

Recommendation: update README and `docs/nexus-codebase-context.md` to reflect the current architecture: cognitive sub-agent tools, no `/delegate` worker mailbox runtime, no automatic DAG scheduler, no compatibility filesystem module.

### High: Advanced Mode Has Two Mental Models In The Repo

Current code model:

- `agent_mode = "basic"` means normal single-agent execution.
- `agent_mode = "advanced"` means the supervisor only sees `subagent_*` tools.
- Built-in cognitive tools are registered from `nexus/tools/subagents.py`.
- Custom specialists come from `delegation_subagents`.

Stale model still present in tests/docs:

- `delegation_enabled`
- `/delegate`
- worker IDs and mailboxes
- DAG planning and repair
- `delegate_task`

This makes future changes risky because contributors will not know which model is canonical.

Recommendation: write a short migration note and remove or archive stale worker/DAG tests. Keep `context_state.py` only if packet summaries remain part of the sub-agent design.

### Medium: Permission Policy Is Partly Semantic, Partly Name-Based

Tools expose `ToolKind` and `is_mutating`, but `PermissionChecker` still special-cases `bash`, `write_file`, and `memory`. Its hard path policy only covers `write_file`; other mutating path tools enforce workspace and `.nexus` denial inside their own `execute()` methods.

This is not an immediate bypass because `edit`, `write_file`, and `apply_patch` have execution-time checks. The weakness is consistency: approvals, audit records, and risk labels see `edit`/`apply_patch` as generic medium-risk mutations even when they target sensitive paths and will later be refused.

Recommendation: move path-argument metadata into tools, or expand `_path_policy()` to handle all first-party path-mutating tools before approval/UI/audit.

### Medium: Audit Classification Is Stale

`observability/audit.py` marks only `write_file` as high and `bash` as critical. It treats `edit`, `insert_edit_into_file`, `apply_patch`, `memory`, `todos`, formatter commands, MCP mutations, and sandbox commands as low by default.

Recommendation: classify by `ToolKind`, `is_mutating`, shell risk, and confirmation preview instead of a short hard-coded tool-name list.

### Medium: Cognitive Sub-Agent Prompt Is Duplicated

`SubAgentTool.execute()` prepends `definition.goal_prompt` to `instructions`, then `_direct_subagent_system_prompt()` also includes `definition.goal_prompt` as role instructions. The same role prompt is therefore presented twice.

Recommendation: keep persona/role in the system prompt and leave the user message as the task-specific instructions only.

### Medium: `needs_clarification` Sub-Agent Results Are Not Marked As Errors

The sub-agent envelope can infer `needs_clarification`, but `is_failed` only includes `failed` and `needs_approval`. A sub-agent that needs clarification returns a successful tool result with `recommended_next_action: "ask_user"`.

This may be intentional, but it should be explicit. If the supervisor ignores the JSON field, it can treat a blocked sub-agent as completed.

Recommendation: either mark `needs_clarification` as `is_error=True`, or add tests proving the supervisor reliably asks the user when that status appears.

### Medium-Low: Dead Or Vestigial Code Is Accumulating

Likely dead or vestigial items:

- `_print_multi_agent_status()`, `_print_multi_agent_plan()`, `_print_multi_agent_tasks()`, `_print_multi_agent_packets()`, and `_public_multi_agent_state()` in `slash_commands.py` are not routed by `build_router()`.
- `_render_inner_events()` in `sandbox/agent_tool.py` is not called.
- Module-level `_chat_url(base_url)` in `integrations/ollama.py` is unused because the class method `_chat_url()` is used.
- `ReplState.disabled_tools` is defined but not used.
- Large parts of multi-agent context state appear retained for legacy compatibility while the automatic DAG runtime is gone.

Recommendation: either wire these surfaces intentionally or remove them in a cleanup pass. Keep compatibility only where tests and docs say why it exists.

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
- `find_references`
- `code_index`
- `semantic_search`
- `git_status`
- `git_diff`
- `run_tests`
- `run_linter`
- `run_typecheck`
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

Compatibility aliases such as `write_note`, `modify_file`, and `replace_text` are not present in the live default tool surface.

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

1. Fix the test contract first. Delete or rewrite tests for removed worker/DAG/legacy tool modules, then add tests for current cognitive sub-agent behavior.
2. Update README and `docs/nexus-codebase-context.md` so they no longer advertise nonexistent modules and old orchestration.
3. Clean dead slash-command helper code and unused compatibility helpers.
4. Generalize permission and audit classification around tool metadata.
5. Tighten sub-agent status semantics and remove duplicated goal prompts.
6. Add provider edge-case tests for malformed tool arguments and streaming tool-call assembly.

## Bottom Line

The live core architecture is coherent: bootstrap in `app.py`, shared turn execution in `turn_runner.py`, event-driven tool execution in `Agent`, durable history in `ReplState`, and optional cognitive sub-agents layered through the tool registry.

The repo is not currently healthy as a project artifact because the tests and docs still describe an older architecture. Fixing that drift should be the next priority before adding more runtime features.
