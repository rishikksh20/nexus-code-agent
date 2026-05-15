# Nexus Codebase Context

Last updated: 2026-05-15

This document is the working map for the live Nexus codebase. It intentionally excludes `reference_code/`; that directory is historical reference material and should only be opened when a user explicitly asks for it.

## Current Shape

Nexus is a CLI-first coding agent implemented as a Python package under `nexus/`.

- `nexus/app.py` is the top-level runtime orchestrator. It loads config, builds the tool registry, starts optional resources, creates the model client and agent, and dispatches either interactive or headless runs.
- `nexus/runtime/agent.py` owns model streaming, tool-call interpretation, permission checks, tool execution, hook emission, and agent events.
- `nexus/runtime/repl.py` owns the interactive REPL loop: terminal setup, prompt reading, slash-command dispatch, and interactive approval input.
- `nexus/runtime/turn_runner.py` owns the shared turn runner used by both interactive and headless mode. `run_agent_turn()` is the bridge between user-facing approval UX and the lower-level event-driven agent.
- `nexus/runtime/repl_state.py` prepares each turn: system prompt construction, history preparation, tool-output pruning, context compaction, metadata, and durable history updates.
- `nexus/runtime/runtime_session.py` builds `ReplState` from config, sessions, skills, memory, hooks, delegation resources, and approval policy.
- `nexus/config/` contains defaults, TOML/env/CLI merge logic, validation, and model-context limit helpers.
- `nexus/tools/` contains the first-party tool system, registry helpers, compatibility filesystem tools, and sub-agent tool registration.
- `nexus/security/` contains approval policies, approval state, permission checks, and shell-risk classification.
- `nexus/integrations/` contains provider adapters: fake, OpenAI-compatible HTTP providers, native Ollama, native Anthropic, native Gemini, MCP, and retry support.
- `nexus/extensions/`, `nexus/hooks/`, `nexus/skills/`, `nexus/memory/`, `nexus/context/`, `nexus/sandbox/`, and `nexus/observability/` provide optional runtime capabilities around the core loop.

## Runtime Flow

One runtime session is created through `RuntimeSession.create()`.

1. `NexusApp.initialize()` applies model context limits, builds hooks, creates the tool registry, loads plugins, connects MCP servers, registers sandbox and sub-agent tools, and creates `Agent`.
2. Interactive mode calls `run_repl()`. Headless mode calls `run_headless()`.
3. Both paths append the user message, begin a new approval turn when needed, and call `turn_runner.run_agent_turn()`.
4. `run_agent_turn()` asks `ReplState.prepare_turn()` for model-ready messages, context metadata, and the system prompt.
5. `Agent.run()` streams model output, emits assistant events, evaluates tool calls, and either executes allowed tools or emits a `CONFIRMATION_REQUESTED` event.
6. `turn_runner.run_agent_turn()` is the single user-facing approval callback owner. It records approval/refusal/clarification and, on approval, resumes exact pending calls through `Agent.run(..., resume_tool_calls=...)`.
7. `ReplState.apply_events()` appends only model messages whose tool calls have matching tool results, accumulates usage, and saves the session.

The important invariant is provider-safe message ordering: assistant messages with `tool_calls` must not be persisted unless the corresponding tool result messages are also present. This is why pending confirmation events are handled carefully before history is committed.

## Approval Flow

Approval is centralized at the turn-runner layer.

- `Agent.run()` does not accept a direct approval callback.
- `Agent._agentic_loop()` emits `CONFIRMATION_REQUESTED` and returns when a confirmation or clarification is required.
- `turn_runner.run_agent_turn()` invokes the callback provided by the REPL or headless wrapper.
- If the user approves, `run_agent_turn()` records the approval in `ApprovalManager`, commits a narrowed assistant model event for the exact pending tool call(s), and resumes execution with `resume_tool_calls`.
- `Agent._execute_approved_tool_calls()` executes those exact calls without asking the provider to regenerate them.
- One-time approvals are consumed after execution. Turn-wide approval excludes high or dangerous bash calls.
- Refusals are recorded for the current turn so the model receives a denied tool result instead of repeatedly prompting for the same invocation.

This design prevents the previous loop where approving a tool caused the model to regenerate a similar tool call and ask again.

## Tools

The default core registry is built by `nexus/tools/registry.py`.

Default first-party tools:

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

Compatibility classes still exist for older tests and extension boundaries, including `write_note`, `modify_file`, and `replace_text`, but they are not registered by the normal core registry. The current public default surface should be documented with the canonical tool names above.

Tools expose:

- `name`
- `description`
- `input_schema`
- `is_mutating`
- `kind`
- `execute()`
- optional `get_confirmation()` preview data such as diffs, commands, and affected paths

Tool metadata is used by permissions, hook payloads, terminal rendering, and prompts. Some security logic still uses explicit tool-name checks for special cases such as `write_file`, legacy write aliases, `memory`, and `bash`.

## Security

Security is split between stateless decisions and stateful approvals.

- `PermissionChecker.evaluate()` returns `ALLOW`, `CONFIRM`, or `DENY`.
- `ApprovalManager` tracks once, turn, session, turn-wide mutating, and refused approvals.
- Plan mode denies mutating operations.
- Auto mode allows low and medium mutating operations, but high and dangerous bash commands still require confirmation.
- Path policy denies writes outside the workspace and direct writes under `.nexus`, including `.nexus/memory`.
- `CommandClassifier` wraps the shell classifier and promotes catastrophic patterns to `DANGEROUS`.

The security layer is intentionally conservative around shell and whole-file writes.

## Providers

Provider selection is validated in `nexus/config/loader.py`.

Valid providers:

- `anthropic`
- `fake`
- `gemini`
- `mistral`
- `openai`
- `openai-compatible`
- `ollama`

`nexus/app.py` builds the active client:

- `FakeModelClient` for tests and demos.
- `OpenAICompatibleModelClient` for OpenAI-compatible HTTP APIs, including Mistral/OpenAI-style endpoints.
- `OllamaModelClient` for local Ollama.
- `AnthropicModelClient` for Anthropic.
- `GeminiModelClient` for Gemini.

Config supports provider-specific defaults, API-key resolution, and model context-limit adjustments.

## Sessions And Context

Sessions are stored through `SessionStore` unless `--no-session` uses `EphemeralSessionStore`.

Important session behavior:

- Existing session messages are sanitized before reuse.
- Tool-output pruning protects recent context while shrinking large tool outputs.
- Context compaction uses soft and hard limits and writes carry-over state.
- Paused turns are stored in session metadata when the per-turn tool-call limit is reached.
- The user can type `continue` to resume a paused task.
- Usage totals are accumulated in session metadata.

## Extensions

Optional runtime features include:

- Plugins loaded from the configured plugins directory.
- MCP tools registered through connected MCP servers.
- Skills from global, local, and builtin roots.
- Delegation runtime and sub-agent tools.
- Sandbox command execution when enabled.
- Hooks for lifecycle, prompt submit, pre/post tool use, notifications, and stop events.
- Post-session updates for memory/workspace learning.

These features are layered around the same `ToolRegistry`, `Agent`, and `turn_runner.run_agent_turn()` flow.

## Tests

Use:

```bash
uv run pytest
```

Current expected result after the latest approval-planning cleanup: `273 passed`.

The tests cover config loading, CLI/headless flows, REPL approval behavior, session sanitation, security decisions, tools, hooks, plugins, MCP, sandbox, delegation, prompts, retry, and provider adapters.

## Review Notes

Current architectural strengths:

- One user-facing approval owner in `turn_runner.run_agent_turn()`.
- Deterministic approved-tool execution through `resume_tool_calls`.
- Provider-safe history persistence.
- Clear default tool registry.
- Strong config validation.
- Conservative permission policy for writes and shell commands.

Current cleanup opportunities:

- Same-batch approval planning now goes through `Agent.preapproved_tool_calls_from_batch()`, keeping registry and permission details out of the turn runner.
- Permission logic still has some tool-name special cases. More of this could move to `ToolKind` or richer tool metadata.
- Compatibility tool names still appear in older tests/docs. Keep the canonical registry explicit when updating docs.
- Config writes currently serialize plain values and may not preserve comments. That is acceptable for now, but worth revisiting if config editing becomes a first-class UX.

## Reference Code Boundary

Do not scan, edit, test against, or document `reference_code/` during normal Nexus work. Treat it as opt-in reference material only when the user explicitly requests a comparison.
