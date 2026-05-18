# Chapter 8: Cognitive Sub-Agents And Coordination

## Objective

Add advanced-mode assistance without creating a second orchestration runtime. Nexus keeps one supervisor turn loop, one approval path, and one provider-safe history contract. Extra capability comes from optional cognitive `subagent_*` tools.

## Current Model

Only add sub-agent tooling after the single-agent harness already has:

- typed messages and tool results
- session persistence
- permission controls
- structured logging
- a testing strategy

Advanced mode is enabled with:

```toml
agent_mode = "advanced"
```

When advanced mode is active, the tool registry can expose built-in specialist tools:

- `subagent_planning_analysis`
- `subagent_execution`
- `subagent_review`
- `subagent_verification`

Custom specialists come from `delegation_subagents` config entries or skills whose names begin with `subagent-`.

## Coordination Rules

- The supervisor remains the only conversational owner.
- Sub-agents are tools called by the supervisor, not independent command surfaces.
- Tool filters still apply through `allowed_tools` and `denied_tools`.
- Mutating work still routes through the normal permission system.
- Sub-agent output returns as structured tool output for the supervisor to interpret.

## Action Plan

1. Keep `run_orchestrated_turn()` as a thin wrapper over `run_agent_turn()`.
2. Register cognitive tools only when `agent_mode = "advanced"`.
3. Keep built-in sub-agent definitions small, role-specific, and tool-scoped.
4. Allow skill-backed `subagent-*` definitions to extend the tool surface.
5. Show sub-agent state through `/tools`, `/skills`, and `/context` rather than a separate command family.
6. Add tests for registration, tool filtering, structured envelopes, and approval behavior.

## Validation Checklist

- Basic mode does not register `subagent_*` tools.
- Advanced mode registers built-in cognitive tools when allowed by filters.
- Skill-backed sub-agent tools load after `/skills reload`.
- Sub-agent tool calls do not bypass approval checks.
- Context snapshots stay compact and inspectable.

## Definition Of Done

This chapter is complete when advanced mode feels like a disciplined extension of the normal tool system: understandable, approval-safe, and easy to disable.
