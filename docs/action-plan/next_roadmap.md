# Nexus Agent Framework - Next Roadmap

## Purpose

This document captures the next implementation steps after the current continuity pass. It is intentionally sectionwise and maps directly to what already exists in the codebase today.

The goal is not to redesign Nexus from scratch. The goal is to extend the current implementation with the smallest set of coherent next changes, while preserving the existing architecture:

- provider-neutral runtime types
- CLI-first REPL and headless execution
- local and global state under `.nexus/` and `~/.nexus/`
- explicit permissions, hooks, and auditability
- minimal but real MCP, delegation, sandbox, and skill seams

## Current Baseline

The current implementation already includes:

- typed runtime request and response models
- REPL and headless flows
- fake model client plus a live OpenAI-compatible `/chat/completions` path
- early provider config validation
- local and global config merging
- session persistence, compaction, workspace learning, and profile updates
- skill loading and explicit session skill activation
- MCP tool discovery, plugin loading, Docker sandboxing, and delegation runtime
- correlated logs, metrics snapshots, and dangerous-action audit trail

The roadmap below focuses on what should come next, section by section.

## 1. Provider Runtime And Model Execution

### Current State

- `fake` remains the default provider for deterministic development.
- `openai` and `openai-compatible` now use the live compatible client path.
- provider config is validated early and requires `api_base_url` for live compatible runs.
- the runtime still uses a `complete()`-only model client contract.

### Next Steps

1. Add true streamed provider output for the live provider path.
2. Separate provider capability flags from provider selection.
3. Add explicit timeout and retry tuning to config instead of keeping them internal constants.
4. Introduce provider error classification that is visible in hooks and user-facing errors.
5. Add a clean path for more than one live provider family without leaking provider-specific JSON into runtime code.

### Changes Required

- extend the model client protocol so streaming is a first-class capability rather than only a rendering option
- add a normalized stream event shape for partial assistant output
- update REPL and headless flows to consume streamed model events without breaking existing tool-call control flow
- move retry count, base delay, jitter, and request timeout into validated config fields
- introduce provider capability metadata such as `supports_streaming`, `supports_tools`, and `supports_usage`
- add a provider error normalization layer so transport failures, rate limits, auth failures, and malformed responses are surfaced consistently

### Validation Targets

- streaming works with the live provider path and still preserves deterministic fake-model behavior in tests
- provider misconfiguration still fails before runtime startup
- retryable and non-retryable failures remain distinguishable
- tool-call behavior remains unchanged when streaming is disabled

## 2. Config, Secrets, And Environment Management

### Current State

- config merges built-in defaults, global config, local config, environment, and CLI overrides
- provider auth is environment-based via `NEXUS_API_KEY` and `OPENAI_API_KEY`
- provider names and `api_base_url` are now validated during config load

### Next Steps

1. Expand config validation to cover more operational fields.
2. Add explicit config for live provider transport settings.
3. Clarify the environment variable contract and keep it consistent with the actual loader.
4. Add a doctor/report surface that explains where each effective config value came from.

### Changes Required

- add validated fields for provider timeout, retry count, retry backoff, and optional streaming enablement
- document and possibly standardize environment variable naming if the repo wants to move from generic `AGENT_*` names to `NEXUS_*` names for config overrides
- extend `nexus doctor` or `nexus config show` so operators can inspect source-of-truth origin for important settings
- add negative tests for invalid MCP, sandbox, and provider-related config combinations

### Validation Targets

- config errors point to the exact invalid field or incompatible combination
- effective config can be inspected without reading multiple TOML files manually
- env and CLI precedence remain stable and covered by tests

## 3. Agent Loop, REPL, And Headless UX

### Current State

- the agent loop is typed and provider-neutral
- REPL and headless execution share the same runtime model
- slash commands already cover mode, config, session, tools, MCP, memory, skills, context, history, and delegation
- streaming flags exist in config, but live streaming is not implemented yet

### Next Steps

1. Add true incremental rendering for live model output.
2. Improve confirmation and clarification UX for longer multi-step tasks.
3. Make slash-command output more operationally useful for long-running sessions.
4. Add better resume and recovery ergonomics for interrupted sessions.

### Changes Required

- thread streamed token events through the existing event model
- add richer confirmation views that show scope, reason, rollback notes, and recent context together
- extend `/session` with richer summary or checkpoint inspection if needed
- add explicit state transitions for partial, interrupted, or resumable runs in session metadata
- ensure headless mode exposes the same structured status outcomes as the REPL where possible

### Validation Targets

- REPL output stays readable under streaming and tool-heavy turns
- headless mode keeps stable JSON and JSONL contracts
- interrupted sessions can be resumed without corrupting state

## 4. Context, Sessions, Memory, And Learning

### Current State

- sessions are persisted with retention pruning and `latest_session.txt`
- carry-over summaries exist for compacted history
- workspace learning updates `.nexus/knowledge.md` and `.nexus/facts.json`
- user-scoped learning updates `~/.nexus/profile.md` and `~/.nexus/workspaces.json`
- memory search is still simple file scanning

### Next Steps

1. Improve retrieval quality for memory and workspace facts.
2. Make compaction and carry-over more structured and testable.
3. Prevent low-signal post-session learning from accumulating noise.
4. Add clearer boundaries between durable facts, recency notes, and summaries.

### Changes Required

- replace naive memory scanning with indexed or scored lookup
- add structured carry-over entries instead of only summary strings where useful
- introduce pruning or deduplication rules for workspace facts and profile updates
- add a clearer schema for learned facts versus generated summaries
- make post-session updates more observable, including when they are skipped, merged, or deduplicated

### Validation Targets

- long-running sessions retain useful context without unbounded growth
- post-session learning stays stable across repeated similar sessions
- memory retrieval returns more relevant results than simple file-order scans

## 5. Skills System

### Current State

- skills can be loaded from global and local directories
- local skills can override global ones
- skills are activated explicitly through CLI or slash commands
- the model does not auto-select or request skills

### Next Steps

1. Add skill metadata beyond name, description, and content.
2. Support controlled automatic skill selection.
3. Improve skill lifecycle and visibility for operators.
4. Separate stable skill instructions from ephemeral runtime hints.

### Changes Required

- add skill metadata fields such as category, tags, version, and optional safety constraints
- define a policy for automatic activation that remains deterministic and inspectable
- add diagnostics for duplicate, invalid, or conflicting skills
- extend `/skills` output with source, override status, and validation state
- add tests for automatic selection policy once that feature exists

### Validation Targets

- skill resolution stays deterministic when both local and global versions exist
- automatic selection is explainable and can be disabled cleanly
- invalid skills do not break session startup

## 6. MCP, Plugins, And Sandboxed Execution

### Current State

- MCP tools can be discovered over stdio
- plugin loading exists from the global plugin directory
- Docker sandbox execution is available when configured
- MCP refresh hot-registers newly discovered tools into an active session; see `docs/mcp-integration.md`

### Next Steps

1. Improve lifecycle control for MCP servers.
2. Tighten plugin trust and validation rules.
3. Expand sandbox guardrails and operator visibility.
4. Keep dynamic tool refresh behavior covered by regression tests.

### Changes Required

- add stronger MCP reconnect and status reporting semantics
- keep runtime tool hot-registration observable and approval-safe
- introduce plugin manifest validation and safer load diagnostics
- add sandbox execution metadata to hooks, audit, and doctor reporting
- define per-tool or per-source safety policies so plugin and MCP tools can be constrained more explicitly

### Validation Targets

- MCP failures degrade safely and are inspectable from the REPL and doctor output
- plugin load failures do not destabilize runtime startup
- sandbox execution remains auditable and bounded under failure conditions

## 7. Delegation, Mailboxes, And Multi-Agent Coordination

### Current State

- delegation runtime exists with typed mailbox messages and coordinator-owned task state
- workers use restricted registries and the same core agent loop
- approvals route back through the coordinator
- optimistic resource coordination exists

### Next Steps

1. Strengthen task lifecycle state and recovery.
2. Improve delegated-task observability.
3. Add clearer mailbox retention and summarization strategy.
4. Clarify resource contention and retry semantics for workers.

### Changes Required

- add task checkpointing or recovery markers for interrupted delegated work
- record worker-level telemetry and correlation more explicitly
- define how much mailbox history is retained, summarized, or compacted
- extend resource claim behavior beyond optimistic version checks if stricter coordination is needed
- add more coverage for worker clarification, cancellation, timeout, and partial-completion flows

### Validation Targets

- delegated tasks can be diagnosed after failure without reading raw logs only
- worker approval and clarification flows remain correct under concurrency
- mailbox history remains bounded and useful

## 8. Observability, Audit, And Cost Control

### Current State

- runtime JSONL logging exists
- metrics aggregation exists
- audit trail exists for mutating actions
- correlation IDs exist for turns, traces, and tool calls
- traces are not exported externally yet

### Next Steps

1. Add external trace/export support.
2. Improve redaction coverage and audit retention policy.
3. Make cost reporting more provider-aware.
4. Separate developer diagnostics from operator-facing telemetry more clearly.

### Changes Required

- introduce an export layer for OpenTelemetry or another trace sink
- define retention rules for `runtime.jsonl`, `metrics.json`, and `.nexus/audit-trail.jsonl`
- add stronger secret redaction tests for nested and provider-shaped payloads
- move from placeholder provider cost logic to explicit cost estimation rules where possible
- extend doctor output to verify observability readiness beyond log-format toggles

### Validation Targets

- external trace export does not break local-only operation
- secret redaction remains correct for provider headers and nested payloads
- audit and metrics outputs remain consistent across REPL, headless, MCP, sandbox, and delegation flows

## 9. Permissions, Dangerous Actions, And Safety Hardening

### Current State

- execution modes are implemented
- `write_note` has argument-aware hard-deny rules
- mutating actions produce audit records with rollback notes
- dangerous actions are visible, but rollback is still descriptive rather than automated

### Next Steps

1. Generalize argument-aware permission rules beyond `write_note`.
2. Improve dangerous-action classification.
3. Add stronger safety boundaries for external tool sources.
4. Make rollback guidance more actionable where possible.

### Changes Required

- move from single-tool hardening to a broader rule model that can reason about path, source, tool kind, and risk level
- classify MCP, plugin, and sandboxed actions with more explicit danger tiers
- add permission policy tests for more than one mutating tool family
- decide whether rollback plans stay descriptive or become executable helpers for some built-in actions

### Validation Targets

- policy decisions are consistent across built-in, MCP, plugin, and sandbox tools
- hard-deny rules cannot be bypassed by mode shortcuts or worker delegation
- dangerous-action records remain complete and correlated

## 10. Production Readiness, Multi-User Controls, And Operations

### Current State

- `nexus doctor` provides local rollout checks
- the system is documented honestly as a stable scaffold rather than a full platform
- multi-user authn/authz and hosted deployment controls are not implemented

### Next Steps

1. Decide whether Nexus remains a single-user local harness or grows toward a hosted platform.
2. Add explicit operational readiness gates for the chosen deployment model.
3. Define backup, restore, retention, and incident-response guidance.
4. Introduce multi-user controls only if the product direction requires them.

### Changes Required

- document the deployment target clearly: local-only, team-shared, or hosted service
- extend doctor/reporting to check more operational gates if hosted use is planned
- define data retention and cleanup policies for sessions, memory, logs, metrics, and audit trail
- add user identity, quotas, and authorization layers only if a multi-user runtime becomes a real goal

### Validation Targets

- release posture matches the actual control surface
- operators can verify readiness without reading source code
- state durability and recovery expectations are documented explicitly

## 11. Documentation, Testing, And Maintenance

### Current State

- README and action-plan docs have been updated incrementally
- pytest coverage exists across the implemented slices
- the codebase is still evolving chapter by chapter

### Next Steps

1. Keep docs synchronized with implementation changes in the same pass.
2. Add broader integration coverage for multi-surface flows.
3. Create a clearer release checklist for future chapters.
4. Keep roadmap and README aligned so they do not drift.

### Changes Required

- add integration tests that span provider selection, permissions, session persistence, and observability together
- keep README, chapter docs, and roadmap docs updated whenever runtime behavior changes
- add a small release checklist covering tests, docs, diagnostics, and doctor output
- add explicit coverage for negative paths, not only happy-path runtime flows

### Validation Targets

- every major runtime feature has both behavior tests and documentation coverage
- docs do not claim features that are only partial or planned
- future roadmap items are measurable and can be marked complete with clear criteria

## Recommended Execution Order

To keep scope controlled, the next implementation passes should follow this order:

1. Provider runtime completion:
   add true streaming, provider capability metadata, and configurable transport settings.
2. Config and observability hardening:
   extend validated provider settings, doctor output, and telemetry export groundwork.
3. Memory and skills improvement:
   improve retrieval quality, fact deduplication, and skill metadata and selection policy.
4. MCP, plugin, and sandbox hardening:
   tighten trust, lifecycle control, and source-aware safety policy.
5. Delegation and operational maturity:
   improve recovery, mailbox retention, worker telemetry, and release gates.

## Definition Of Done For The Next Roadmap Phase

The next roadmap phase should be considered complete only when these outcomes are true:

- the live provider path is feature-complete enough to be a real alternative to the fake client for normal development
- config, observability, and safety policies fail early and explain failures clearly
- long-session continuity is more structured and useful than simple file scanning and summary accumulation
- docs and tests stay aligned with every implementation change
- the project remains honest about what is stable, what is experimental, and what is still deferred
