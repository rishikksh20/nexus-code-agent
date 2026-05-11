# Nexus Agent Framework Audit Report

_Date:_ 2026-04-27  
_Scope:_ `nexus/`, `tests/`, root docs, and `docs/action-plan/`

## Executive Summary

This audit reviewed the live implementation, tests, and documentation for the Nexus agent scaffold. The project is structurally strong and testable, with a clear runtime architecture, passing regression suite, and honest README-level status reporting.

The most important findings fell into three groups:

1. runtime correctness issues in compaction, delegation, and config propagation
2. documentation drift in `docs/action-plan/`
3. a few config surfaces that existed in code but were not fully honored at runtime

This pass also fixed the highest-priority runtime issues listed below and added targeted regression tests.

## Audit Method

- reviewed runtime entrypoints and execution flow from `nexus/app.py`
- traced config loading, permissions, compaction, delegation, provider integration, and observability
- reviewed the behavior-level test suite under `tests/`
- spot-checked current-state docs in `README.md` and design-oriented docs under `docs/action-plan/`
- executed the test suite and local doctor command

## Verified Commands

### Passed

```bash
uv run nexus version
uv run nexus doctor --output-format json
uv run --group dev python -m pytest -q
```

### Doc mismatch observed

The README currently documents this test command:

```bash
uv run --group dev pytest -q
```

On the audited environment, that exact command failed because `pytest` was not exposed as a standalone executable through `uv run`, while `python -m pytest` worked.

## Findings Summary

| ID | Area | Severity | Status |
|---|---|---:|---|
| F-01 | Delegated worker tool execution used `Path.cwd()` instead of configured workspace root | High | Fixed in this pass |
| F-02 | `collect_turn_events()` applied a second compaction pass after `ContextCompactor.compact()` | High | Fixed in this pass |
| F-03 | Compaction could orphan tool-result messages by starting a retained window with `tool` messages | High | Fixed in this pass |
| F-04 | `temperature` and `max_output_tokens` config were not forwarded into `RuntimeRequest` | High | Fixed in this pass |
| F-05 | `auto_confirm_read_only` config existed but was not honored by the runtime | High | Fixed in this pass |
| F-06 | MCP tools are all classified as mutating | Medium | Open |
| F-07 | Metrics collector flushes synchronously on every event | Medium | Open |
| F-08 | MCP response reader can discard non-matching lines without a cap | Medium | Open |
| F-09 | Post-session fact extraction is prone to false positives | Medium | Open |
| F-10 | Session storage is atomic but not concurrency-safe across processes | Medium | Open |
| F-11 | `docs/action-plan/` contains stale `.agent` / `agent_harness` naming and outdated config examples | Medium | Open |
| F-12 | README test command does not match verified invocation on this environment | Low | Open |

## Detailed Findings

### F-01 — Delegated worker workspace boundary mismatch

**Files:** `nexus/runtime/delegation.py`, `nexus/app.py`

**Issue:** Worker tool execution used `Path.cwd()` instead of the configured workspace root, which meant delegated `write_note` operations could validate paths against the wrong directory.

**Risk:** Boundary enforcement could be applied to the process launch directory rather than the active Nexus workspace.

**Resolution in this pass:**
- threaded `workspace_root` from app config into `DelegationRuntime`
- propagated that root into each `WorkerAgent`
- created a regression test proving delegated writes land in the configured workspace even when the process CWD differs

### F-02 — Double compaction in REPL turn collection

**File:** `nexus/runtime/repl.py`

**Issue:** `collect_turn_events()` compacted once through `ContextCompactor.compact()` and then compacted the result again via `compact_messages()`.

**Risk:** Silent message loss and harder debugging for long sessions.

**Resolution in this pass:**
- removed the redundant second compaction pass
- added a regression test that ensures the compactor output is passed unchanged into the agent

### F-03 — Compaction orphaned tool-result context

**File:** `nexus/runtime/context.py`

**Issue:** Compaction could start a retained window with `tool` messages, leaving tool outputs without the preceding assistant turn that requested them.

**Risk:** Invalid provider message sequences in live compatible providers.

**Resolution in this pass:**
- introduced safe recent-boundary handling so retained windows do not begin with `tool` messages
- dropped orphaned leading tool messages when budget pressure would otherwise keep only tool outputs without their assistant context
- added focused compaction tests for safe boundaries and tight-budget behavior

### F-04 — Request settings not forwarded from config

**Files:** `nexus/runtime/agent.py`, `nexus/runtime/repl.py`, `nexus/runtime/delegation.py`

**Issue:** `AgentConfig.temperature` and `AgentConfig.max_output_tokens` were defined and validated, but not forwarded into `RuntimeRequest`.

**Risk:** Live provider calls silently used fallback request defaults instead of configured behavior.

**Resolution in this pass:**
- threaded both fields through `collect_turn_events()` and delegated worker runs
- updated `Agent.run()` to populate `RuntimeRequest.temperature` and `RuntimeRequest.max_output_tokens`
- added a regression test confirming the request carries the configured values

### F-05 — `auto_confirm_read_only` was not honored

**Files:** `nexus/runtime/permissions.py`, `nexus/runtime/agent.py`, `nexus/runtime/repl.py`, `nexus/runtime/delegation.py`

**Issue:** The config field existed but the runtime never passed it into permission evaluation, so read-only tools were always auto-allowed.

**Risk:** Misleading config surface and inability to require explicit approval for read-only tools.

**Resolution in this pass:**
- threaded `auto_confirm_read_only` into permission checks
- implemented the behavior so read-only tools require confirmation in `default` mode when the flag is disabled
- added a regression test proving `get_time` now requests approval when configured that way

## Open Issues

### F-06 — MCP tools are all treated as mutating

**File:** `nexus/integrations/mcp.py`

All MCP tools currently set `is_mutating = True`. This is conservative but creates friction for clearly read-only MCP operations.

### F-07 — Metrics writes block the event loop

**File:** `nexus/observability/metrics.py`

The metrics collector rewrites `metrics.json` synchronously after each event.

### F-08 — MCP response discard loop is unbounded

**File:** `nexus/integrations/mcp.py`

The response reader ignores non-matching JSON-RPC lines indefinitely until timeout.

### F-09 — Post-session fact extraction is overly broad

**File:** `nexus/runtime/post_session.py`

Pattern-based fact extraction can persist facts from examples or discussion rather than actual actions.

### F-10 — Session storage is not concurrency-hardened

**File:** `nexus/runtime/sessions.py`

Atomic replacement protects against partial writes but not concurrent multi-process overwrites.

### F-11 — `docs/action-plan/` documentation drift

Multiple chapter docs still use `.agent`, `~/.agent`, `agent_harness`, or `agent --prompt` examples instead of current Nexus names and defaults. Some config examples also describe values that differ from the live code.

### F-12 — README test command mismatch

`README.md` documents `uv run --group dev pytest -q`, while the audited environment required `uv run --group dev python -m pytest -q`.

## Tests Added In This Pass

- `tests/test_context.py`
  - safe recent boundaries around tool results
  - orphaned-tool dropping under tight token budgets
  - summary behavior at safe compaction boundaries
- `tests/test_repl.py`
  - config-driven forwarding of `temperature` and `max_output_tokens`
  - honoring `auto_confirm_read_only`
  - regression for removal of double compaction
- `tests/test_delegation.py`
  - delegated worker writes use configured workspace root rather than process CWD

## Recommended Next Actions

1. Make MCP mutability configurable per tool or per prefix.
2. Offload or batch metrics flushes to avoid event-loop blocking.
3. Cap discarded MCP response lines before failing.
4. Tighten fact extraction so only stronger evidence is persisted.
5. Decide and document whether concurrent session writers are supported.
6. Bring `docs/action-plan/` back in sync with live Nexus naming and behavior.
7. Update README test commands to the verified invocation used by the repo.

## Current Validation Snapshot

At the end of this pass, the following command succeeded:

```bash
uv run --group dev python -m pytest -q
```

