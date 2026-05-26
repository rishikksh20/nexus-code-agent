# Langfuse + Sentry Observability Plan

## Summary

Add a unified Nexus observability layer that keeps local JSONL/audit/metrics separate, uses **Langfuse** for LLM/agent/prompt traces, and keeps **Sentry** focused on runtime errors, crashes, breadcrumbs, and performance spans.

Chosen defaults from your preferences:

- Langfuse captures full prompt/model content by default.
- Nexus keeps prompt construction local and links traces to local prompt name/version/hash metadata.
- `langfuse` is an optional dependency with lazy imports.

References used: Langfuse SDK tracing and sessions docs, prompt-to-trace docs, and Sentry Python/LLM monitoring docs:
https://langfuse.com/docs/observability/sdk/overview  
https://langfuse.com/docs/observability/sdk/python/instrumentation  
https://langfuse.com/docs/tracing/sessions  
https://langfuse.com/docs/prompt-management/features/link-to-traces  
https://docs.sentry.io/product/llm-monitoring/getting-started/  
https://getsentry.github.io/sentry-python/apidocs.html  

## Key Changes

- Add `nexus/observability/runtime.py` with a `NexusObservability` facade:
  - Owns configured sinks: JSONL runtime logger, metrics collector, Sentry monitor, Langfuse monitor.
  - Exposes small methods for `turn_start`, `turn_end`, `model_start`, `model_end`, `tool_start`, `tool_end`, `context_update`, `flush`.
  - Keeps hook registration in `setup_hooks(config)` so runtime code remains event-driven.

- Add `nexus/observability/langfuse.py`:
  - `LangfuseSettings`, `LangfuseMonitor`, `LangfuseHookService`, `LangfuseClientProtocol`.
  - Lazy import `from langfuse import get_client, propagate_attributes`.
  - Trace model:
    - Langfuse session = Nexus `session_id`.
    - Langfuse trace = one Nexus user turn using existing `trace_id`.
    - Root span = `nexus.turn`.
    - Generation observations = every LLM call in `Agent._agentic_loop`.
    - Tool observations = every tool call, including sub-agent tools.
    - Event observations = approvals, denials, clarifications, loop/context events.
  - Use Langfuse attribute propagation for `session_id`, `user_id`, tags, and metadata.

- Extend config in `AgentConfig`, loader validation, env aliases, README, and config examples:
  - `langfuse_enabled`
  - `langfuse_public_key`
  - `langfuse_secret_key`
  - `langfuse_base_url`
  - `langfuse_environment`
  - `langfuse_release`
  - `langfuse_trace_content = true`
  - `langfuse_trace_tool_outputs = true`
  - `langfuse_prompt_name = "nexus-system-prompt"`
  - `langfuse_prompt_version = ""`
  - `langfuse_flush_timeout_seconds = 2.0`

- Add optional dependency:
  - `pyproject.toml`: `[project.optional-dependencies] observability = ["langfuse"]`
  - Runtime warns clearly if `langfuse_enabled=true` but the package is missing.

## Instrumentation Details

- Turn tracing:
  - `run_agent_turn()` starts/ends the root Langfuse turn trace through hook events, preserving centralized approval callbacks and `resume_tool_calls`.
  - Turn metadata includes provider, model, mode, agent mode, status, duration, tool count, token usage, cost, active skills count, available tools count, and context/prune/compaction summaries.

- LLM tracing:
  - Wrap model streaming in a Langfuse generation observation.
  - Record input messages, system prompt, output text, provider/model, temperature, max tokens, finish reason, tool-call count, usage, cost, and errors.
  - Attach local prompt metadata: prompt name, configured version, system prompt hash, prompt length, active skill names, and context section counts.

- Tool and sub-agent tracing:
  - Existing `PRE_TOOL_USE` and `POST_TOOL_USE` hooks become Langfuse tool spans.
  - Include tool name/source/origin, mutating flag, arguments, duration, output, error status, actor/subagent, parent session id, worker id, and input packet ids.
  - Add `worker_id` to sub-agent `ToolExecutionContext.metadata` as the sub-agent tool call id.

- Context tracking:
  - Emit `CONTEXT_COMPACTION` when compaction happens in `ReplState.prepare_turn`.
  - Emit a new notification or hook payload for tool-output pruning summaries.
  - Record context before/after message counts, estimated tokens, pruned tool result count, carry-over summary count, memory entry count, active skills, and multi-agent packet ids.

## Sentry Vs Langfuse

- Use Langfuse for:
  - LLM prompt/response tracing.
  - Prompt version tracking and prompt quality comparison.
  - Token usage, cost, latency, generations, sessions, and agent/tool timeline review.
  - Agent debugging where the question is “what did the model see and decide?”

- Use Sentry for:
  - Exceptions, crashes, provider errors, tool exceptions, MCP startup failures, and high-risk denials.
  - Breadcrumbs leading to runtime failures.
  - Stack traces, release/environment grouping, and operational alerting.
  - Performance spans that help correlate failures with runtime code paths.

- Langfuse benefits:
  - Purpose-built for LLM traces, generations, sessions, prompt metrics, and evaluations.
  - Better for reviewing complete agent behavior and prompt/model outcomes.
  - Drawback: captures sensitive AI content unless carefully configured; less ideal as a general crash/issue tracker.

- Sentry benefits:
  - Strong error grouping, stack traces, release tracking, alerts, breadcrumbs, and production incident workflows.
  - Already partially implemented in Nexus.
  - Drawback: not as rich for prompt/version/evaluation workflows; LLM content should stay limited there by default.

## Docs And Tests

- Add `docs/llm-observability-langfuse-sentry.md`:
  - Architecture diagram in prose.
  - Config examples.
  - Event/span naming convention.
  - Session/group/trace mapping.
  - Privacy policy for Sentry vs Langfuse.
  - Sentry vs Langfuse comparison table.
  - Verification checklist.

- Update README observability section with minimal setup:
  - `uv sync --extra observability`
  - Langfuse env vars
  - Sentry env vars
  - Recommended local config block.

- Add tests:
  - Config/env parsing and validation for Langfuse fields.
  - Lazy missing dependency warning.
  - Fake Langfuse client receives turn, generation, tool, and context events.
  - Prompt metadata/hash is attached to generations.
  - Sub-agent tool traces include parent session and worker id.
  - Sentry still redacts prompts/tool outputs unless explicitly configured.
  - Existing `uv run pytest tests/test_sentry_monitoring.py` remains green; add `tests/test_langfuse_observability.py`.
