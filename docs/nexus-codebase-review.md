# Nexus Codebase Review

Review date: 2026-05-15

Scope: live Nexus codebase only. `reference_code/` was intentionally excluded from this review because it is reference-only material and should only be opened when explicitly requested.

Verification target:

```bash
uv run pytest
```

Latest known result after the approval-planning cleanup: `273 passed`.

## Executive Summary

The current codebase is in a much healthier state than the previous approval-flow review. The major risk that caused repeated permission prompts has been addressed: user-facing approval callbacks live in `turn_runner.run_agent_turn()`, while `Agent.run()` remains event-driven and resumes exact approved tool calls with `resume_tool_calls`.

No blocking correctness issue was found in the reviewed live code. The remaining items are mostly clarity, consolidation, and long-term hardening work.

## Current Architecture

The runtime is organized around these layers:

- `nexus/app.py`: runtime bootstrap, provider selection, registry/resource setup, headless/interactive dispatch, teardown.
- `nexus/runtime/agent.py`: model stream processing, tool-call handling, permission checks, confirmation events, tool execution, hooks, loop limits.
- `nexus/runtime/repl.py`: interactive REPL loop, terminal-facing prompt handling, slash-command dispatch.
- `nexus/runtime/turn_runner.py`: shared turn runner used by both REPL and headless mode, including `run_agent_turn()`, confirmation helpers, approval resume, and history-safe commit helpers.
- `nexus/runtime/repl_state.py`: system prompt, model history preparation, pruning, compaction, context metadata, event persistence.
- `nexus/security/`: approval policy, approval memory, command classification, hard-deny path rules.
- `nexus/tools/`: first-party tool registry, tool base protocol, builtin tools, legacy compatibility tool classes, sub-agent tools.
- `nexus/integrations/`: fake, OpenAI-compatible, Ollama, Anthropic, Gemini, MCP, retry.
- `nexus/extensions/`, `nexus/hooks/`, `nexus/skills/`, `nexus/memory/`, `nexus/sandbox/`, `nexus/observability/`: optional runtime capabilities.

## Approval Flow Review

Current flow:

1. `Agent._agentic_loop()` receives model tool calls.
2. It evaluates permission with `PermissionChecker`.
3. If approval or clarification is required, it emits `CONFIRMATION_REQUESTED` and returns.
4. `turn_runner.run_agent_turn()` asks the user through the REPL/headless approval callback.
5. On approval, `run_agent_turn()` records approval state and calls `Agent.run(..., resume_tool_calls=...)`.
6. `Agent._execute_approved_tool_calls()` executes the exact previously shown call(s).
7. `ReplState.apply_events()` persists only history-safe assistant/tool message pairs.

This fixes the previous provider-regeneration loop where approving one shown `write_file` call could cause the model to produce another slightly different `write_file` call and prompt again.

The design is now close to a single event-driven approval model. The user-facing callback is not in `Agent.run()`, so lower-level callers cannot accidentally create a second approval UX path.

## Strengths

- Approval prompting is centralized in `turn_runner.run_agent_turn()`.
- Approved execution is deterministic because it resumes exact `ToolCall` objects.
- History persistence protects provider message ordering.
- `ApprovalManager` distinguishes once, turn, session, turn-wide mutating, and refused approvals.
- High and dangerous bash calls still require fresh approval.
- Path policy denies writes outside the workspace and direct writes into `.nexus` state.
- Core registry exposes a clean canonical tool surface.
- Config validation covers providers, modes, approval policies, numeric bounds, tool allow/deny overlap, MCP entries, and delegation definitions.
- Interactive and headless flows share the same turn runner.
- Session sanitation, paused-turn resume, usage accumulation, hooks, plugins, MCP, skills, and delegation are integrated without bypassing the main agent path.

## Findings And Follow-Ups

### 1. `repl.py` Owns More Than REPL UI

Status: addressed

`run_agent_turn()`, confirmation helpers, approval resume logic, telemetry, paused-turn handling, and provider-safe history helpers now live in `nexus/runtime/turn_runner.py`. `nexus/runtime/repl.py` keeps `run_repl()` and terminal-facing helpers only.

### 2. Approval Resume Planning Still Peeks Into Agent Internals

Status: addressed

Same-batch approval planning now goes through `Agent.preapproved_tool_calls_from_batch()`. The turn runner still owns the user callback and approval recording, but the "which calls are executable under current approval state?" decision is behind an agent method with a narrow API.

### 3. Tool Metadata Is Only Partly Semantic

Severity: low

Tools expose `ToolKind`, `is_mutating`, and confirmation previews, but permission policy still relies on tool-name checks for `bash`, `write_file`, `memory`, and legacy write aliases.

Recommendation: over time, move more policy inputs into structured tool metadata, for example write strategy, shell risk source, persistent-memory mutation, and path argument name. Keep explicit special cases only where the policy truly is tool-specific.

### 4. Compatibility Tool Names Remain In Tests And Docs

Severity: low

`write_note`, `modify_file`, and `replace_text` still exist for compatibility tests and older extension expectations, while the default core registry intentionally exposes canonical tools such as `write_file`, `edit`, `insert_edit_into_file`, and `apply_patch`.

Recommendation: keep compatibility classes, but continue updating user-facing docs and prompts to prefer canonical tool names. Do not re-add compatibility aliases to the default registry unless there is a deliberate migration reason.

### 5. Config Editing Does Not Preserve Comments

Severity: low

The config loader correctly merges TOML, env, and CLI values, but config write/update flows that serialize plain values can lose comments and formatting.

Recommendation: if config mutation becomes common UX, use a comment-preserving TOML writer or limit automatic rewrites to generated files.

### 6. Provider Edge Cases Need Ongoing Coverage

Severity: medium-low

The provider surface now includes OpenAI-compatible APIs, Ollama, Anthropic, and Gemini. The core event contract is solid, but native provider tool-call formats and streaming edge cases are easy to regress.

Recommendation: keep adding focused adapter tests for partial tool-call chunks, mixed text/tool responses, no-usage responses, provider errors, and malformed tool arguments.

### 7. Context Compaction Is Heuristic

Severity: low

The context pipeline prunes large tool outputs and compacts when token estimates cross configured thresholds. This is appropriate for now, but it is heuristic and can affect long-running sessions.

Recommendation: add scenario tests for long sessions with many tool results, paused turns, and active skills to ensure compaction preserves the task-critical context.

## Current Tool Surface

Default registry tools:

- `get_time`
- `read_file`
- `write_file`
- `edit`
- `insert_edit_into_file`
- `apply_patch`
- `glob`
- `grep`
- `list_dir`
- `bash`
- `memory`
- `todos`
- `web_fetch`
- `web_search`

Compatibility-only or non-default tool classes include `write_note`, `modify_file`, and `replace_text`.

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

## Files Reviewed

Reviewed live-code areas:

- `README.md`
- `nexus/app.py`
- `nexus/runtime/agent.py`
- `nexus/runtime/repl.py`
- `nexus/runtime/turn_runner.py`
- `nexus/runtime/repl_state.py`
- `nexus/runtime/runtime_session.py`
- `nexus/cli/headless.py`
- `nexus/config/defaults.py`
- `nexus/config/loader.py`
- `nexus/security/permissions.py`
- `nexus/security/manager.py`
- `nexus/security/classifier.py`
- `nexus/tools/base.py`
- `nexus/tools/registry.py`
- `nexus/tools/builtin/__init__.py`
- relevant tests discovered under `tests/`

Excluded:

- `reference_code/`

## Suggested Next Work

1. Continue replacing legacy tool names in docs and manual test plans.
2. Add long-session compaction scenario tests.

## Bottom Line

The codebase is no longer carrying the approval callback split that caused the repeated permission loop. The live architecture is coherent: model/tool execution stays in `Agent`, user approval stays in `turn_runner.run_agent_turn()`, and durable history is committed only after provider-safe message pairs exist.
