# Chapter 9: Production Rollout Blueprint

## Objective

Finish the action plan with a realistic rollout path. This final chapter turns the prior implementation chapters into a release sequence for a minimal but full-fledged Python agent harness.

The purpose is not to promise instant production readiness. The purpose is to make the transition from prototype to dependable internal tool deliberate.

## The Four Delivery Milestones

### Milestone 1: Minimal Useful Harness

Required chapters:

- Chapter 1 through Chapter 5

Capabilities:

- REPL and typed agent loop
- built-in tools
- streaming output
- session history
- context assembly
- durable memory
- permissions and execution modes

This is the smallest version you should let real users try.

### Milestone 2: Maintainable Harness

Add:

- Chapter 6

Capabilities:

- configuration management
- repeatable tests
- structured logs
- usage and cost awareness

This is the minimum stage for a team-maintained internal project.

### Milestone 3: Extensible Harness

Add:

- Chapter 7

Capabilities:

- plugin loading
- MCP integration
- sandboxed dangerous tools

This is the minimum stage for ecosystem-style growth.

### Milestone 4: Coordinated Harness

Add:

- Chapter 8

Capabilities:

- coordinator-worker delegation
- mailbox communication
- auditable approval routing

Only add this stage if the workload actually benefits from delegation.

## Suggested Implementation Order In A Real Project

Use this sequence even if multiple developers are involved:

1. stabilize data models and the single-agent loop
2. add tool registry and one or two safe tools
3. add permissions before adding more dangerous tools
4. add sessions and context compaction before long testing sessions
5. add tests before adding plugins or MCP
6. add logs before adding delegation
7. add sandboxing before exposing command execution broadly
8. add multi-agent coordination only after single-agent behavior is reliable

This ordering is slower at the start but much faster overall because failures stay local.

## Recommended Release Gates

Before each milestone moves forward, require these gates.

### Gate A: Runtime Integrity

- typed models are used end-to-end
- message history is replayable
- tool calls are logged and attributable

### Gate B: Safety Integrity

- mutating tools require approval or explicit auto-mode allowance
- denied actions are enforced in runtime code
- dangerous execution paths are sandboxed or disabled

### Gate C: Operational Integrity

- sessions can be resumed
- logs contain session and tool identifiers
- tests cover the most important happy and unhappy paths

### Gate D: Extension Integrity

- plugin load failures are isolated
- MCP tools are visible in registry output
- permission policy still applies to external tools

## Current Nexus Notes

The current Nexus implementation now includes a minimal production hardening layer that fits these gates without changing the architecture:

- `nexus doctor` checks runtime, safety, operational, and extension readiness from the live workspace config
- JSON observability writes both `runtime.jsonl` and an aggregated `metrics.json` snapshot for lightweight production review
- `write_file` has a bounded payload size so the default mutating file writer is less likely to be abused accidentally
- runtime permission checks now inspect `write_file` arguments and hard-deny writes outside the workspace or into `.nexus/` managed state

These are still intentionally small mechanisms. They help make rollout deliberate without pretending the harness now needs a large deployment platform.

## A Minimal Project Skeleton To Aim For

```text
agent_harness/
  __init__.py
  app.py
  config.py
  models.py
  prompts.py
  runtime/
    agent.py
    context_builder.py
    execution_modes.py
    hooks.py
    permissions.py
    sessions.py
    streaming.py
  tools/
    base.py
    builtin.py
    registry.py
    sandbox.py
  memory/
    store.py
    workspace.py
  integrations/
    fake_model.py
    openai_client.py
    mcp.py
    plugins.py
  multi_agent/
    coordinator.py
    mailbox.py
    worker.py
  observability/
    logging.py
    metrics.py
  tests/
    test_agent_loop.py
    test_permissions.py
    test_sessions.py
    test_tools.py
```

## What To Delay On Purpose

Do not add these too early:

- vector memory systems
- complex retrieval pipelines
- distributed workers across hosts
- automatic self-modification
- large plugin APIs
- complicated UI rendering layers

The tutorial material strongly suggests a better pattern: build clarity first, then complexity.

## Recommended First Real Use Cases

Choose narrow use cases for the first rollout:

- repository inspection
- documentation drafting
- safe file reads and note generation
- test skeleton generation behind approval
- controlled refactoring suggestions

Delay broad shell automation until sandboxing and audit logging are stable.

## Final Action Plan Summary

1. Build the typed single-agent loop.
2. Add tools and streaming.
3. Persist sessions and shape context dynamically.
4. Add durable memory.
5. Enforce permissions and execution modes in code.
6. Add hooks, config, tests, and structured logs.
7. Extend through plugins and MCP.
8. Sandbox dangerous execution.
9. Add delegation only where it clearly reduces complexity.
10. Roll out in milestones with validation gates.

## Definition Of Success

You have succeeded if your harness is:

- small enough to understand end-to-end
- strict enough to enforce real runtime boundaries
- flexible enough to extend without rewriting the core
- observable enough to debug when behavior goes wrong

That is what a full-fledged but minimal harness should mean.

For Nexus specifically, the practical minimal rollout loop is now:

1. run `uv run --group dev pytest -q`
2. run `uv run nexus doctor --output-format json`
3. verify `log_format = "json"` for environments that need attributable logs and metrics
4. enable sandboxed commands only after the sandbox image is built and the doctor report is clean