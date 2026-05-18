# Cognitive Sub-Agent Context Roadmap

Last updated: 2026-05-18

This roadmap describes the canonical Nexus advanced-mode model. Nexus has one supervisor turn loop, one approval path, and optional cognitive `subagent_*` tools. It intentionally avoids a second scheduler or command surface.

## Current Runtime Shape

```text
User prompt
  -> REPL/headless wrapper
  -> run_orchestrated_turn()
  -> run_agent_turn()
  -> Agent.run()
  -> tool calls / approvals / tool results
  -> ReplState.apply_events()
  -> SessionStore.save()
```

Important files:

- `nexus/runtime/orchestration.py` preserves the common call site and delegates directly to `run_agent_turn()`.
- `nexus/runtime/turn_runner.py` owns user-facing approval callbacks and exact pending-call resume.
- `nexus/runtime/agent.py` remains event-driven and provider-safe.
- `nexus/tools/subagents.py` registers built-in and skill-backed cognitive sub-agent tools.
- `nexus/runtime/context_state.py` stores compact context records and handoff packets when sub-agent state needs to survive later turns.

## Canonical Advanced Mode

Advanced mode is enabled with:

```toml
agent_mode = "advanced"
```

Built-in cognitive tools:

- `subagent_planning_analysis`
- `subagent_execution`
- `subagent_review`
- `subagent_verification`

Custom specialists can be configured through `delegation_subagents` or loaded from skills named `subagent-*`.

## Architecture Principles

- Sub-agents are tools, not independent runtimes.
- The supervisor remains the only conversational owner.
- Sub-agent prompts should be scoped and role-specific.
- Sub-agent output should be compact, structured, and returned to the supervisor.
- Mutating work must still use the normal permission and approval path.
- Session history must preserve assistant/tool-result ordering.

## Near-Term Roadmap

1. Keep `run_orchestrated_turn()` thin and boring.
2. Strengthen tests for advanced-mode tool registration and filtering.
3. Tighten sub-agent result semantics for `failed`, `needs_approval`, and `needs_clarification`.
4. Remove duplicated role text from sub-agent prompts.
5. Keep `/context` as the visibility surface for compact agent state.
6. Store artifacts such as diffs, logs, test output, and review summaries as first-class context records only when they help later turns.

## Validation Checklist

- Basic mode registers no cognitive tools.
- Advanced mode registers built-in cognitive tools when tool filters allow them.
- Skill-backed sub-agent tools load through `/skills reload`.
- Sub-agent mutating calls do not bypass `run_agent_turn()`.
- Compact context records do not copy raw full conversations between agents.

## Boundary

Future coordination work should extend the existing tool and context systems. It should not introduce a parallel approval path, hidden mutation loop, or separate user-facing command family.
