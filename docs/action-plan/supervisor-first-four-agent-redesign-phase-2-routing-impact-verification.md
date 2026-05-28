# Supervisor-First Four-Agent Redesign — Phase 2 Routing, Impact, and Verification

Date: 2026-05-27

## Status

Phase 2 turns the Phase 1 four-agent rename into a concrete supervisor-first flow for the live Nexus runtime.

This design stays inside the current architecture:

- one supervisor turn loop
- cognitive sub-agents exposed as normal `subagent_*` tools
- centralized approvals in `run_agent_turn()`
- provider-safe event/history ordering in `Agent.run()`
- scope calculations centralized in `nexus/runtime/agent_scope.py`

It deliberately does not reintroduce the removed multi-agent scheduler, background task DAG, or separate approval path.

## Current Runtime Ground Truth

The live codebase already has the four built-in specialists:

- `subagent_explorer`
- `subagent_coding`
- `subagent_code_reviewer`
- `subagent_impact_analyzer`

Those agents are registered by `nexus/tools/subagents.py` in advanced mode. The supervisor sees them through the same `ToolRegistry` and permission machinery as any other tool.

The existing implementation already provides important foundations:

- `SubAgentTool` isolates sub-agent local history and returns a structured JSON envelope.
- `ToolExecutionContext.metadata` carries active skills, MCP scope, packet summaries, approval state, and supervisor tool scope.
- `ContextPacket`, `ArtifactRecord`, and typed multi-agent state in `nexus/runtime/context_state.py` can store compact handoffs.
- `run_tests`, `run_python_check`, `run_formatter`, `git_status`, and `git_diff` expose structured metadata rather than raw-only command output.

The remaining gap is that these pieces are not yet tied into a durable routing, impact, verification, and failure-attribution contract. Phase 1 mostly changed names and prompts; Phase 2 makes the behavior reusable and testable.

## Design Goals

Nexus should behave like a supervisor-led coding system, not a role-fanout system.

The supervisor owns:

- understanding the user request
- deciding whether the task is tiny enough to handle directly
- creating bounded delegation packets
- sequencing sub-agents
- integrating results into the final user response
- deciding whether unresolved uncertainty needs user or manual validation

Sub-agents own focused work only:

- Explorer investigates read-only context.
- Coding performs mutations.
- Impact Analyzer scopes blast radius and verification.
- Code Reviewer reviews and verifies within the provided scope.

The central rule is:

> Verification must be scoped, risk-aware, delta-aware, baseline-aware when possible, and explicit about manual validation.

## Supervisor Routing Policy

The supervisor may use direct tools only for tiny read-only work that fits about three simple tool calls or fewer.

Examples:

- list one directory and read a README
- inspect one known file
- answer a small “where is this defined?” question

Everything else should use the appropriate sub-agent:

| Task shape | Route |
| --- | --- |
| Tiny read-only inspection | Supervisor direct tools |
| Medium or broad read-only exploration | `subagent_explorer` |
| Any code/file mutation | `subagent_coding` |
| Unknown blast radius or test scope | `subagent_impact_analyzer` |
| Review, verification, or failure attribution | `subagent_code_reviewer` |

This routing policy is represented by a small deterministic helper in `nexus/runtime/supervisor_routing.py`. It is not a scheduler. Its purpose is to give prompts, tests, docs, and future UI affordances one shared vocabulary for the supervisor-first contract.

## Delegation Packet Contract

Every sub-agent call should be bounded. The supervisor should pass only focused instructions and relevant packet ids.

Each delegation packet should include:

- objective
- files, symbols, or directories if known
- constraints and non-goals
- expected JSON fields
- stop condition
- tool budget
- relevant `input_packet_ids`

The supervisor should not paste the full conversation into sub-agents. It should prefer `input_packet_ids` and compact summaries.

## Agent Responsibilities

### Explorer

Explorer is read-only.

It should:

- inspect only the requested codebase slice
- prefer packet summaries before rereading files
- read targeted snippets before whole files
- stop after enough evidence is gathered
- return related files, findings, risks, and open questions

It should not:

- modify files
- invent implementation plans unless explicitly requested
- perform verification or broad test planning

### Coding

Coding is the only built-in mutating implementation agent.

It should:

- implement only the assigned change
- follow existing project patterns
- keep edits focused
- use cheap local validation directly tied to the edit
- report changed files, validation run, risks, and follow-up context

It should not:

- choose broad verification scope
- run full test suites by default
- use `bash` by default
- fix unrelated failures

### Impact Analyzer

Impact Analyzer is read-only and exists to prevent noisy verification.

It should return:

```json
{
  "changed_files": [],
  "affected_modules": [],
  "public_interfaces_changed": [],
  "risk_level": "low|medium|high",
  "validation_category": "auto_validatable|partially_validatable|manual_required",
  "candidate_review_targets": [],
  "candidate_tests": [],
  "verification_policy": {
    "syntax_check": true,
    "formatter_check": false,
    "unit_tests": [],
    "integration_tests": [],
    "e2e_tests": [],
    "manual_validation": []
  },
  "failure_attribution_hints": []
}
```

The first implementation uses hybrid lightweight evidence:

- `git_diff` and `git_status`
- file paths and naming conventions
- local grep/glob/LSP inspection
- existing test layout
- LLM semantic reasoning over the gathered evidence

It should not attempt a heavy repository-wide dependency graph in the first slice.

### Code Reviewer

Code Reviewer handles post-change review and scoped automated verification.

It should:

- consume impact-analysis packets first when available
- inspect diffs and targeted files
- run `run_python_check` when syntax validation is useful
- run `run_tests` only with focused args unless broad regression is justified
- classify failures before recommending fixes
- explicitly report manual validation needs

Broad `uv run pytest` is allowed only when impact analysis marks medium/high risk in shared infrastructure, config, tool runtime, provider integration, or cross-cutting behavior.

## Typed Handoff Packets

Sub-agent results should become compact context packets when they contain durable handoff value.

Phase 2 packet types:

- `exploration_summary`
- `coding_summary`
- `impact_analysis`
- `review_findings`
- `verification_result`
- `failure_analysis`

Packets should include only compact fields:

- summary
- related files
- modified files
- behavior changes
- recommended tests
- failure summary
- confidence
- small structured artifacts when needed

The current runtime now records these packets from structured sub-agent envelopes when the result includes meaningful files, tests, risks, findings, verification policy, or failure analysis.

## Failure Attribution

Reviewer verification should not hand raw logs back to the supervisor without classification.

Failure analysis should include:

```json
{
  "related_to_task": true,
  "confidence": 0.0,
  "reasoning_summary": "",
  "suspected_causes": [],
  "likely_preexisting": false,
  "recommended_next_action": ""
}
```

Supervisor behavior:

- Fix task-related failures when confidence is high enough.
- Mark unrelated or likely pre-existing failures without chasing them.
- Ask the user before expanding scope.
- If no baseline exists, say attribution confidence is limited.

## Repository Memory and Read Reuse

The first implementation keeps read reuse session-scoped and turn-local.

Rules:

- use packet summaries before rereading
- prefer targeted snippets before full files
- cache identical same-turn read-only calls for stable tools such as `read_file`, `list_dir`, `grep`, `glob`, `lsp`, `git_status`, and `git_diff`
- clear the cache after any mutation because prior read results may be stale
- do not write directly to `.nexus/`; persist only through existing runtime/context APIs

This reduces repeated reads across supervisor/sub-agent loops without weakening isolation.

## Implementation Phases

### Phase 2A — Spec and Tests

- Add this spec.
- Add routing helper tests.
- Add packet persistence tests.
- Add prompt contract assertions.
- Add duplicate read-cache tests.

### Phase 2B — Routing Contract

- Strengthen supervisor prompt language around tiny direct work and delegation packets.
- Keep `run_orchestrated_turn()` as a thin wrapper.
- Do not introduce a second scheduler.

### Phase 2C — Typed Handoff

- Parse structured sub-agent envelopes.
- Persist useful handoff packets.
- Return output packet ids in sub-agent results.
- Feed packet summaries through existing `multi_agent_packet_summaries`.

### Phase 2D — Scoped Verification

- Require reviewer consumption of impact packets when available.
- Require focused `run_tests` args by default.
- Store failure-analysis packets for failed or uncertain verification.

### Phase 2E — Read Reuse

- Reuse identical same-turn read-only tool results.
- Clear read cache after mutation.
- Keep read reuse internal to runtime metadata.

### Phase 2F — Config Upgrade Path

- Generated local configs should advertise the four built-ins: `explorer`, `coding`, `code_reviewer`, and `impact_analyzer`.
- Runtime loading should normalize legacy top-level tool allowlists such as `subagent_execution` to `subagent_coding` so existing workspaces do not lose access to the current tools.
- Normal config loading should not rename arbitrary profile names, because users may define custom YAML or skill-backed sub-agents with legacy-looking names.
- `/config upgrade` should perform the durable on-disk cleanup for stale built-in names:
  - `planning_analysis` -> `explorer`
  - `execution` -> `coding`
  - `review` -> `code_reviewer`
  - `verification` -> `code_reviewer`
  - `subagent_execution` -> `subagent_coding`
  - `subagent_planning_analysis` -> `subagent_explorer`
  - `subagent_review` and `subagent_verification` -> `subagent_code_reviewer`
- Upgrade should rewrite legacy `[agents]`, `[[sub-agents]]`, `subagent_profiles`, and `delegation_subagents` entries while preserving user-owned values.

## Test Plan

Focused validation:

```bash
uv run pytest tests/test_config.py tests/test_tools.py tests/test_subagent_defaults.py tests/test_subagent_tool_flow.py tests/test_context.py tests/test_prompts.py tests/test_slash_commands.py -q
```

Important scenarios:

- Advanced-mode built-ins remain registered and scoped through `agent_scope.py`.
- `/config upgrade` normalizes stale built-in sub-agent names without breaking custom sub-agent names during ordinary config loading.
- Supervisor direct tools remain available only through configured scope.
- Coding mutations route through `subagent_coding`.
- `subagent_coding` still lacks `run_tests` and `bash` by default.
- Impact analyzer structured output creates an `impact_analysis` packet.
- Reviewer output can create `review_findings`, `verification_result`, or `failure_analysis` packets.
- Packet summaries appear in downstream sub-agent context.
- Duplicate same-turn read-only calls are served from cache.
- Provider-safe assistant/tool-result ordering remains intact.

## Non-Goals

- No new scheduler.
- No parallel multi-agent DAG.
- No background autonomous repair loop.
- No broad dependency graph engine in the first implementation.
- No direct `.nexus/` writes outside existing runtime/storage APIs.
- No user-facing approval callback inside `Agent.run()`.

## Acceptance Criteria

Phase 2 is complete when:

- the four-agent routing policy is documented and reflected in prompts
- sub-agent envelopes can persist typed handoff packets
- impact and reviewer outputs carry enough structured data for scoped verification
- same-turn read reuse suppresses identical read-only calls
- tests cover packet creation, routing helper behavior, prompt requirements, and duplicate read reuse
