# Multi-Agent Context Management Architecture Roadmap

Last updated: 2026-05-15

This document turns the raw context-management idea map into a Nexus-specific implementation roadmap. It is based on the live codebase only. `reference_code/` is intentionally excluded from this analysis.

The central design rule is simple:

```text
Agents share compact structured summaries, not raw conversation history.
```

For Nexus, that means multi-agent architecture should grow around the existing event-driven runtime, approval model, session store, and delegation system. It should not replace them with a shared chat transcript or a second approval path.

## Executive Summary

Nexus already has the right foundation for multi-agent context management:

- `nexus/runtime/turn_runner.py` centralizes user-facing approval callbacks in `run_agent_turn()`.
- `nexus/runtime/agent.py` remains event-driven and can resume exact approved calls with `resume_tool_calls`.
- `nexus/runtime/repl_state.py` preserves provider-safe assistant/tool-result ordering before persisting history.
- `nexus/runtime/orchestration.py` adds a conservative supervisor wrapper around the existing turn runner.
- `nexus/runtime/context_state.py` records compact agent context snapshots and handoff packets in session metadata.
- `nexus/runtime/delegation.py` provides in-memory coordinator/worker mailboxes, worker task records, restricted tool registries, and coordinator-routed approvals.
- `nexus/tools/subagents.py` registers built-in `subagent_research`, `subagent_review`, and `subagent_test` tools when delegation is enabled.
- Structured inspection tools now exist for `git_status`, `git_diff`, `run_tests`, `run_linter`, `run_typecheck`, `run_formatter`, `find_references`, `code_index`, and `semantic_search`.

The current implementation is a good v0. It records context boundaries and post-execution review state, but it does not yet execute a full dependency-aware task graph across durable specialist agents.

The next architecture step is not "more agents." The next step is a better context substrate:

- typed shared session state;
- typed task contexts;
- persistent agent local summaries;
- structured handoff packets;
- artifact records for diffs, logs, test outputs, and review summaries;
- explicit repair-loop state that resumes the right agent identity;
- slash-command visibility for each layer.

## Current State

### Existing Runtime Shape

The active runtime path is:

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

- `nexus/runtime/orchestration.py`
  - classifies task complexity;
  - asks a planner model for JSON in `plan_task_dag()`;
  - validates the DAG with `TaskDAG` and `TaskNode`;
  - records supervisor, planner, execution, and test-review context records;
  - injects the validated DAG into the main execution prompt;
  - runs post-execution checks with `git_status`, `run_typecheck`, and `git_diff`;
  - stores `SharedState` under `session.metadata["multi_agent"]`.
- `nexus/runtime/context_state.py`
  - defines `ContextPacket`, `AgentContextRecord`, and `ContextScope`;
  - stores compact records under `session.metadata["multi_agent_context"]`;
  - adds multi-agent carry-over lines to future system prompts.
- `nexus/runtime/delegation.py`
  - implements `InMemoryMailbox`, `DelegationRuntime`, `WorkerAgent`, `TaskRecord`, and `WorkerState`;
  - passes `shared_context` strings to workers;
  - builds restricted worker tool registries from per-task allowlists;
  - returns a compact worker context snapshot after completion.
- `nexus/sandbox/agent_tool.py`
  - exposes delegation through `delegate_task` and named specialist `subagent_*` tools;
  - blocks until the worker task reaches a terminal state;
  - truncates large worker outputs before returning them to the main context.
- `nexus/runtime/slash_commands.py`
  - exposes `/multi-agent status`, `/multi-agent plan`, `/multi-agent state`;
  - exposes `/context agents`, `/context agent <id>`, `/context usage <id>`;
  - folds completed delegated worker snapshots into the `/context` view.

### What Already Matches The Target Idea

The raw idea map says agents should behave like isolated processes that communicate through structured IPC. Nexus is already close to that model in several places.

Current matches:

- Raw worker histories are not copied into the supervisor history.
- Workers receive task instructions plus bounded `shared_context`, not the whole conversation.
- Worker tool registries can be restricted per task.
- Approval requests from workers route back to the coordinator instead of becoming hidden user prompts.
- The supervisor stores compact DAG and post-check summaries in session metadata.
- Post-check feedback is represented as a `ContextPacket` from `test-review` to `execution`.
- Future turns receive only compact carry-over lines, not raw review or test transcripts.
- Provider-safe ordering is preserved by `ReplState.apply_events()` and `turn_runner` history-safe commit helpers.

### What Is Still Missing

The current implementation is still a supervisor-assisted single execution path, not a full multi-agent context operating system.

Key gaps:

- `TaskDAG` is validated and shown, but most DAG nodes are not executed as independent agents yet.
- `multi_agent_max_parallel_tasks` is configured and validated, but not yet used for scheduler concurrency.
- `SharedState` is stored as a single session metadata blob, not through a typed store abstraction.
- `ContextPacket` is generic: it has `summary` and `artifacts`, but no packet type, task id, confidence, file list, behavior changes, test list, or failure schema.
- `ContextPacket.packet_id` uses Python `hash()`, which is process-randomized and not stable across sessions.
- Worker local history exists only while the in-memory worker task is running. There is no durable `AgentSessionState` for repair loops.
- Repair currently records a `RepairDecision`; it does not resume the previous execution agent with an injected failure summary.
- Post-execution checks are lightweight and sequential. They do not yet use `subagent_test` or `subagent_review` as first-class DAG nodes.
- Delegation mailboxes are in-memory. They are useful for a live session but are not restart-safe.
- Artifacts such as diffs, command output, test logs, and review reports are not stored as first-class records.
- Compaction is still mostly message-level carry-over. Multi-agent summaries are not versioned rolling summaries yet.
- The optional vector/semantic store is represented only by lightweight lexical tools, not a durable retrieval layer.

## Architecture Principle

Never share raw full conversations between agents.

Bad architecture:

```text
Supervisor conversation
  -> copied wholesale into research agent
  -> copied wholesale into execution agent
  -> copied wholesale into test agent
  -> copied wholesale back into supervisor
```

This creates context explosion, cross-agent contamination, and repeated hallucinated assumptions.

Preferred architecture:

```text
Global Session State
  -> Task Context
  -> Agent Local Context
  -> Ephemeral Working Memory
  -> Structured Handoff Packet
```

Each boundary should have a typed schema and a loading policy.

## Target Context Layers

### 1. Global Session State

Shared across the whole session. This is operating state, not chat history.

Target contents:

```json
{
  "session_id": "abc123",
  "objective": "Implement multi-agent context management",
  "repo_summary": "CLI-first coding agent with event-driven tools and approval-safe sessions",
  "active_tasks": [],
  "completed_tasks": [],
  "repo_map": {},
  "architecture_notes": [],
  "constraints": [
    "Keep approvals centralized in run_agent_turn()",
    "Do not add user-facing approvals to Agent.run()",
    "Preserve provider-safe tool-call/result ordering"
  ],
  "latest_summary_version": 4
}
```

Current approximation:

- `SessionSnapshot.metadata`
- `session.metadata["multi_agent"]`
- `session.metadata["multi_agent_context"]`
- `CarryOverState`
- workspace memory and knowledge stores

Recommended implementation:

- Add a `MultiAgentSessionState` dataclass in `nexus/runtime/context_state.py` or a new `nexus/runtime/multi_agent_state.py`.
- Keep serialization through `SessionSnapshot.metadata` for v1.
- Add load/save helpers instead of direct metadata mutation at every call site.
- Keep `.nexus/` writes behind `SessionStore`, memory store, or a future runtime storage API.

### 2. Task Context

Scoped per task node. This enables resumability, dependency tracking, retries, and repair loops.

Target contents:

```json
{
  "task_id": "review_orchestration",
  "goal": "Review orchestration changes for approval-flow regressions",
  "role": "review",
  "status": "in_progress",
  "dependencies": ["execute_orchestration"],
  "related_files": [
    "nexus/runtime/orchestration.py",
    "nexus/runtime/turn_runner.py"
  ],
  "assigned_agent": "review",
  "input_packets": ["execution-to-review-..."],
  "output_packets": [],
  "artifact_ids": []
}
```

Current approximation:

- `TaskNode` in `nexus/runtime/orchestration.py`
- `TaskRecord` in `nexus/runtime/delegation.py`

Gap:

- `TaskNode` and `TaskRecord` are separate concepts. One is a planner DAG node, the other is a delegated worker runtime task.

Recommended implementation:

- Introduce a shared `TaskContext` model used by orchestration and delegation.
- Let `TaskNode` remain the planner-facing immutable plan item.
- Convert each executable `TaskNode` into a mutable `TaskContext`.
- Store `TaskContext` under session metadata and mirror live worker status into it.

### 3. Agent Local Context

Private to each agent identity. This should not be shared directly.

Target contents:

```json
{
  "agent_id": "execution:task_42",
  "role": "execution",
  "task_id": "task_42",
  "status": "running",
  "working_summary": "Editing orchestration to store typed handoff packets",
  "local_memory_ref": "agent-state/execution-task-42",
  "recent_artifact_ids": [],
  "last_error": null
}
```

Current approximation:

- `AgentContextRecord`
- worker-local `history` inside `WorkerAgent._execute_command()`
- `TaskRecord.context_snapshot`

Gap:

- The actual local memory is not durable.
- The snapshot is produced after the worker completes, not continuously updated.
- Repair loops cannot resume an execution worker with its prior local context.

Recommended implementation:

- Add `AgentSessionState` with `agent_id`, `task_id`, `role`, `status`, `working_summary`, `execution_history_refs`, and `last_handoff_packet_id`.
- Persist compact local summaries, not full message lists.
- Keep raw worker histories in hot memory only unless an artifact policy explicitly stores them as cold debug artifacts.

### 4. Exchange Context

Structured handoff packets are the most important layer. Agents communicate through packets, not conversations.

Target packet schema:

```json
{
  "packet_id": "execution-to-test-20260515-0001",
  "packet_type": "implementation_complete",
  "source_agent": "execution",
  "target_agent": "test",
  "task_id": "implement_context_packets",
  "summary": "Added typed handoff packet storage and carry-over lines.",
  "modified_files": [
    "nexus/runtime/context_state.py",
    "nexus/runtime/orchestration.py"
  ],
  "behavior_changes": [
    "Supervisor stores feedback packets after post-execution checks"
  ],
  "recommended_tests": [
    "uv run pytest tests/test_orchestration.py"
  ],
  "artifact_ids": ["diff:working:..."],
  "created_at": "2026-05-15T00:00:00Z"
}
```

Current approximation:

- `ContextPacket(packet_id, source_agent, target_agent, summary, artifacts, token_estimate, created_at)`
- `AgentMessage` mailbox payloads
- delegated `shared_context`

Recommended implementation:

- Extend `ContextPacket` with `packet_type`, `task_id`, `schema_version`, `modified_files`, `related_files`, `behavior_changes`, `recommended_tests`, `failure_summary`, `artifact_ids`, and `confidence`.
- Replace process-randomized packet ids with stable ids based on `uuid4().hex` or a session-local monotonic counter.
- Add packet constructors for common flows:
  - `research_complete`
  - `implementation_complete`
  - `test_failure`
  - `test_success`
  - `review_findings`
  - `repair_request`
  - `repair_complete`
- Ensure every packet has a token estimate and a redaction/truncation policy.

### 5. Ephemeral Scratchpad

Ultra-local short-term state used while an agent is actively reasoning or executing.

Current state:

- The model's in-flight context and local Python variables serve this role.
- Nexus does not persist chain-of-thought or speculative scratchpads.

Recommended policy:

- Do not persist raw reasoning.
- Persist only decisions, summaries, file lists, errors, and artifacts needed for deterministic continuation.
- Treat command stdout/stderr as artifacts, not as context unless summarized.

## Target System Architecture

```text
User request
  -> Supervisor
  -> Task DAG
  -> Task Store
  -> Scheduler
  -> Agent Sessions
  -> Structured Handoffs
  -> Verification / Review
  -> Repair Loop
  -> Final Response
```

Expanded view:

```text
                 Global Session State
                         |
                 Structured Task Store
                         |
                    Event Bus
                         |
       +-----------------+-----------------+
       |                 |                 |
 Research Agent    Execution Agent    Test Agent
       |                 |                 |
 Local Summary     Local Summary      Local Summary
       |                 |                 |
       +------ Structured Handoffs --------+
                         |
                  Artifact Store
                         |
                Session Persistence
```

## Event Model

The raw idea map recommends event-driven architecture. Nexus already has two event systems:

- `AgentEvent` from `Agent.run()` for model/tool execution.
- `AgentMessage` from `DelegationRuntime` for coordinator/worker mailboxes.

Target multi-agent events should bridge these without creating a second runtime loop.

Recommended event types:

```text
TASK_CREATED
TASK_STARTED
TASK_BLOCKED
TASK_COMPLETED
TASK_FAILED
PACKET_CREATED
ARTIFACT_CREATED
PATCH_APPLIED
TEST_STARTED
TEST_FAILED
TEST_PASSED
REVIEW_REQUESTED
REVIEW_COMPLETED
REPAIR_REQUESTED
REPAIR_COMPLETED
```

Implementation guidance:

- Keep `AgentEvent` as the low-level model/tool event stream.
- Keep `AgentMessage` as the live worker mailbox protocol.
- Add `MultiAgentEvent` for supervisor-visible task/state transitions.
- Store compact `MultiAgentEvent` records in session metadata or a future store.
- Emit hooks for major multi-agent events so logs and audits can observe them.

## Storage Architecture

### V1 Storage

Use the existing session JSON store:

```text
.nexus/sessions/<session_id>.json
  metadata.multi_agent
  metadata.multi_agent_context
  metadata.turns
```

This keeps v1 simple and compatible with current session loading.

### V2 Storage

Add a typed store facade without changing callers:

```text
MultiAgentStore
  save_session_state()
  load_session_state()
  append_event()
  upsert_task_context()
  upsert_agent_state()
  append_packet()
  append_artifact()
```

Possible backends:

- session metadata JSON for local v1;
- SQLite for durable task/event/artifact history;
- Postgres or Redis only if Nexus later needs multi-process coordination.

### Artifact Store

Artifacts should be references, not automatic prompt content.

Artifact types:

- patch;
- diff;
- git status;
- test output;
- typecheck output;
- linter output;
- review report;
- generated file;
- command log.

Recommended model:

```json
{
  "artifact_id": "test-output-0004",
  "artifact_type": "test_result",
  "task_id": "verify",
  "producer_agent": "test",
  "summary": "Typecheck failed in nexus/runtime/orchestration.py",
  "path": ".nexus/artifacts/test-output-0004.json",
  "token_estimate": 128,
  "created_at": "2026-05-15T00:00:00Z"
}
```

Do not inject full artifacts into prompts by default. Retrieve and summarize them based on task needs.

## Memory Lifecycle

Use three tiers.

### Hot Memory

In-process state for the active turn:

- current model messages;
- pending tool calls;
- live worker history;
- current task status;
- current handoff packet;
- current approval request.

This can be discarded after the active task finishes.

### Warm Memory

Session-resumable compact state:

- task graph;
- task contexts;
- agent summaries;
- handoff packets;
- latest verification and review summaries;
- repair decisions;
- important architecture notes.

This belongs in `SessionSnapshot.metadata` for v1.

### Cold Memory

Archived detail:

- full command logs;
- full diffs;
- historical test outputs;
- verbose trace data;
- old mailbox messages.

This should be loaded only by explicit request or targeted retrieval.

## Context Window Strategy

### Supervisor Context

Should contain:

- user objective;
- task graph;
- active and blocked tasks;
- compact repository map;
- constraints and invariants;
- latest handoff summaries;
- repair-loop status.

Should not contain:

- worker raw histories;
- full tool outputs;
- speculative worker reasoning;
- unrelated repository scans.

### Research Agent Context

Should contain:

- research task objective;
- relevant repo map hints;
- read-only tool allowlist;
- dependency packets from upstream tasks.

Output:

- related files;
- relevant symbols;
- architecture findings;
- risks and open questions.

### Execution Agent Context

Should contain:

- user objective;
- validated task node;
- research packet summaries;
- target files and related symbols;
- recent patch/diff summary;
- active constraints.

Output:

- modified files;
- behavior changes;
- implementation notes;
- commands run;
- recommended tests.

### Test Agent Context

Should contain:

- modified files;
- expected behavior;
- recommended tests;
- relevant failure packets;
- recent artifact summaries.

Output:

- passed/failed status;
- failed tests;
- error summary;
- suspected files;
- repair recommendation.

### Review Agent Context

Should contain:

- working diff summary;
- modified files;
- behavior changes;
- verification summary.

Output:

- blocking findings;
- non-blocking findings;
- missing tests;
- suspected regressions.

## Repair Loop Model

Current behavior:

```text
post-execution check fails
  -> RepairDecision(retry=True)
  -> stored in session metadata
  -> visible in /multi-agent status
  -> carried into future prompt context
```

Target behavior:

```text
test/review failure
  -> create repair_request packet
  -> resume same execution agent identity
  -> inject compact failure summary
  -> execute normal approval-safe turn
  -> run verification again
  -> stop after configured repair limit
```

Critical invariant:

```text
Repair must never bypass run_agent_turn() approvals or Agent.run(resume_tool_calls=...).
```

Implementation guidance:

- Add `repair_iteration` to `TaskContext` or `AgentSessionState`.
- Convert `RepairDecision` into a `repair_request` packet.
- Show the repair prompt to the user or make it explicit in the UI before mutating.
- Reuse the same execution agent state summary instead of starting from zero.
- Stop when:
  - checks pass;
  - `multi_agent_max_repair_iterations` is reached;
  - a dangerous/high-risk approval is required and not approved;
  - review reports an unresolved blocking finding.

## Implementation Roadmap

### Phase 0: Preserve Current Runtime Invariants

Status: already mostly satisfied.

Do not change these rules:

- `run_agent_turn()` owns user-facing approval callbacks.
- `Agent.run()` remains event-driven.
- Approved pending calls execute through `resume_tool_calls`.
- Assistant messages with tool calls are persisted only with matching tool results.
- Legacy compatibility tools such as `write_note`, `modify_file`, and `replace_text` are not added back to the default registry.
- `.nexus/` is runtime-managed and should be written through session, memory, or storage APIs.

Success gate:

```bash
uv run pytest tests/test_orchestration.py tests/test_delegation.py tests/test_sessions.py
```

### Phase 1: Stabilize Context Models

Goal: make structured context the first-class data model.

Changes:

- Extend `ContextPacket` with:
  - `schema_version`;
  - `packet_type`;
  - `task_id`;
  - `related_files`;
  - `modified_files`;
  - `behavior_changes`;
  - `recommended_tests`;
  - `failure_summary`;
  - `artifact_ids`;
  - `confidence`.
- Replace hash-based packet ids with stable ids.
- Add `TaskContext`.
- Add `AgentSessionState`.
- Add `MultiAgentSessionState`.
- Add helper functions:
  - `load_multi_agent_state(metadata)`;
  - `save_multi_agent_state(metadata, state)`;
  - `append_context_packet(metadata, packet)`;
  - `upsert_agent_state(metadata, agent_state)`;
  - `upsert_task_context(metadata, task_context)`.

Files:

- `nexus/runtime/context_state.py`
- `nexus/runtime/orchestration.py`
- `nexus/runtime/slash_commands.py`
- `tests/test_orchestration.py`
- `tests/test_slash_commands.py`

Success gates:

- Existing `/context agents` output still works.
- Existing session metadata loads without migration errors.
- Packet ids are stable and unique within a session.
- Tests cover backward compatibility for old `multi_agent_context` payloads.

### Phase 2: Unify Planner DAG And Runtime Task Context

Goal: connect planner output to executable task state.

Changes:

- Keep `TaskDAG` immutable as the validated planner plan.
- Create mutable `TaskContext` objects from `TaskNode` entries.
- Store task status transitions as `MultiAgentEvent` records.
- Track dependency outputs through packet ids instead of plain strings.
- Use `multi_agent_max_parallel_tasks` in a scheduler.
- Add dependency validation for skipped, failed, and blocked nodes.

Files:

- `nexus/runtime/orchestration.py`
- `nexus/runtime/delegation.py`
- `nexus/runtime/context_state.py`

Success gates:

- A DAG with independent read-only tasks can schedule up to `multi_agent_max_parallel_tasks`.
- A task cannot start until dependency packet requirements are satisfied.
- Failed dependency handling is visible in `/multi-agent state`.

### Phase 3: First-Class Handoff Packets

Goal: remove ad hoc summary strings from inter-agent communication.

Changes:

- Replace delegated `shared_context: tuple[str, ...]` with `input_packet_ids` plus rendered packet summaries.
- Keep backwards-compatible string `shared_context` temporarily for slash commands.
- Add packet constructors:
  - `make_research_complete_packet()`;
  - `make_implementation_complete_packet()`;
  - `make_test_failure_packet()`;
  - `make_review_findings_packet()`;
  - `make_repair_request_packet()`.
- Teach `SubAgentTool` to include packet ids in task metadata when supplied.

Files:

- `nexus/runtime/context_state.py`
- `nexus/runtime/delegation.py`
- `nexus/sandbox/agent_tool.py`
- `nexus/tools/subagents.py`

Success gates:

- Test agent receives only implementation packet fields, not execution history.
- Review agent receives diff/artifact summaries, not full planner history.
- `/context agent <id>` shows packet ids for shared inputs and outputs.

### Phase 4: Artifact Records And Structured Verification

Goal: make diffs, logs, and test results addressable artifacts.

Changes:

- Add `ArtifactRecord`.
- Store post-check outputs as artifact records:
  - `git_status`;
  - `git_diff`;
  - `run_typecheck`;
  - optional `run_tests`;
  - review summary.
- Add artifact summaries to handoff packets.
- Add retrieval helpers that load artifact content only when explicitly needed.

Files:

- `nexus/runtime/context_state.py` or new `nexus/runtime/artifacts.py`
- `nexus/runtime/orchestration.py`
- `nexus/tools/builtin/git.py`
- `nexus/tools/builtin/verification.py`

Success gates:

- Full test output is not injected into supervisor prompts by default.
- Review summaries reference artifact ids.
- Slash commands can show artifact summaries and optionally print one artifact.

### Phase 5: Execute Specialist DAG Nodes

Goal: make research, test, and review nodes real workers instead of only a displayed plan.

Changes:

- For read-only research nodes, use `subagent_research` when delegation is enabled.
- For verification nodes, use structured tools directly or `subagent_test`.
- For review nodes, use `summarize_review_findings()` initially, then optionally `subagent_review`.
- Convert each worker result into a typed packet.
- Keep execution edits in the main agent at first to preserve approval visibility.

Files:

- `nexus/runtime/orchestration.py`
- `nexus/runtime/delegation.py`
- `nexus/tools/subagents.py`

Success gates:

- Research node output becomes a `research_complete` packet consumed by execution.
- Test node output becomes `test_success` or `test_failure`.
- Review node output becomes `review_findings`.
- No specialist receives full supervisor history.

### Phase 6: Durable Agent Identity And Repair Resume

Goal: repair the same task with the same compact agent identity.

Changes:

- Add `AgentSessionState` persistence for execution and worker agents.
- Store `working_summary` after each task transition.
- On repair, load the prior execution `AgentSessionState`.
- Inject:
  - original objective;
  - prior working summary;
  - latest failure packet;
  - modified files;
  - relevant artifacts.
- Run repair through `run_agent_turn()` with a user-visible prompt.
- Increment `repair_iteration`.

Files:

- `nexus/runtime/orchestration.py`
- `nexus/runtime/context_state.py`
- `nexus/runtime/turn_runner.py` only if a narrow helper is needed

Success gates:

- Repair loop does not start from a blank context.
- Repair stops after `multi_agent_max_repair_iterations`.
- Mutating repair actions still ask for approval under normal policy.
- Provider-safe history ordering still passes session tests.

### Phase 7: Store Facade And Restart Safety

Goal: move from direct metadata mutation to a clear storage boundary.

Changes:

- Add `MultiAgentStore` facade.
- Use session metadata as the first backend.
- Add schema version and migration helpers.
- Add optional artifact file storage later.
- Add restart/resume tests that load a saved session and continue multi-agent state.

Files:

- `nexus/runtime/context_state.py`
- `nexus/runtime/sessions.py`
- possible new `nexus/runtime/multi_agent_store.py`

Success gates:

- A saved session can restore DAG, tasks, packets, agent summaries, and repair status.
- Old sessions without multi-agent metadata still load cleanly.
- Runtime code stops manually editing nested metadata dictionaries in multiple places.

### Phase 8: Retrieval And Semantic Memory

Goal: add retrieval only after structured context is solid.

Changes:

- Use `code_index` and `semantic_search` as lightweight local retrieval first.
- Add optional vector/semantic store only behind config.
- Store embeddings or concept indexes for summaries, not raw full histories.
- Keep deterministic lexical fallback.

Success gates:

- Retrieval is scoped by task role and dependency packets.
- Agents cannot pull unrelated session history by default.
- Retrieval results are summarized before cross-agent handoff.

## Suggested Data Model Additions

### MultiAgentSessionState

```python
@dataclass(slots=True)
class MultiAgentSessionState:
    schema_version: int
    session_id: str
    objective: str
    dag: TaskDAG | None
    tasks: dict[str, TaskContext]
    agents: dict[str, AgentSessionState]
    packets: list[ContextPacket]
    artifacts: dict[str, ArtifactRecord]
    events: list[MultiAgentEvent]
    latest_summary: RollingSummary | None
```

### TaskContext

```python
@dataclass(slots=True)
class TaskContext:
    task_id: str
    role: AgentRole
    objective: str
    status: TaskStatus
    dependencies: tuple[str, ...]
    assigned_agent_id: str | None
    input_packet_ids: tuple[str, ...]
    output_packet_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    related_files: tuple[str, ...]
    modified_files: tuple[str, ...]
    repair_iteration: int = 0
```

### AgentSessionState

```python
@dataclass(slots=True)
class AgentSessionState:
    agent_id: str
    role: AgentRole
    task_id: str
    status: str
    working_summary: str
    token_estimate: int
    message_count: int
    allowed_tools: tuple[str, ...]
    input_packet_ids: tuple[str, ...]
    output_packet_ids: tuple[str, ...]
    last_error: str | None = None
```

### RollingSummary

```python
@dataclass(slots=True)
class RollingSummary:
    summary_version: int
    objective: str
    important_decisions: tuple[str, ...]
    implemented_features: tuple[str, ...]
    known_failures: tuple[str, ...]
    pending_work: tuple[str, ...]
    active_constraints: tuple[str, ...]
```

## Prompting Guide

### Supervisor Prompt Inputs

Load:

- objective;
- active task contexts;
- latest rolling summary;
- unresolved repair decisions;
- high-level repo map;
- only packet summaries needed for scheduling.

Do not load:

- raw worker messages;
- full test logs;
- full diffs unless reviewing;
- raw tool outputs older than the active turn.

### Execution Prompt Inputs

Load:

- original user goal;
- execution task objective;
- relevant research packets;
- related files;
- active constraints;
- latest repair packet when repairing.

Do not load:

- full planner conversation;
- full test-agent conversation;
- unrelated research packets;
- historical failed approaches unless summarized as a constraint.

### Test Prompt Inputs

Load:

- implementation packet;
- modified files;
- behavior changes;
- recommended tests;
- artifact summaries.

Do not load:

- execution raw conversation;
- supervisor planning transcript;
- unrelated repo map.

### Review Prompt Inputs

Load:

- diff artifact summary;
- modified files;
- behavior changes;
- verification summary;
- relevant coding standards or invariants.

Do not load:

- raw implementation chat;
- full command logs unless needed for a finding.

## Slash Command Roadmap

Existing:

- `/multi-agent status`
- `/multi-agent plan`
- `/multi-agent state`
- `/context agents`
- `/context agent <id>`
- `/context usage <id>`
- `/delegate status`
- `/delegate tasks`
- `/delegate workers`

Recommended additions:

- `/multi-agent tasks`
- `/multi-agent packets`
- `/multi-agent packet <id>`
- `/multi-agent artifacts`
- `/multi-agent artifact <id>`
- `/multi-agent repair`
- `/context task <id>`
- `/context summary`

These commands should display structured summaries first and require explicit user action to print large artifacts.

## Test Plan

Automated test targets:

- `multi_agent_mode = "off"` delegates to `run_agent_turn()` unchanged.
- Planner receives no tool schemas.
- Invalid planner JSON fails before execution.
- DAG dependency order is enforced.
- `TaskNode` converts into `TaskContext`.
- Context packet ids are stable and unique.
- Handoff packets do not contain raw message histories.
- Workers receive restricted tool registries.
- Worker context snapshots expose only summaries and allowed tools.
- Worker approval requests route through the coordinator.
- Provider-safe history survives approval interruption.
- Post-check artifacts are stored by id.
- Repair decisions become explicit repair packets.
- Repair stops at `multi_agent_max_repair_iterations`.
- Old sessions without multi-agent metadata still load.

Focused commands:

```bash
uv run pytest tests/test_orchestration.py
uv run pytest tests/test_delegation.py
uv run pytest tests/test_slash_commands.py -k "multi_agent or context"
uv run pytest tests/test_structured_tools.py
uv run pytest tests/test_sessions.py
```

Full suite:

```bash
uv run pytest
```

## Rollout Plan

Keep defaults conservative:

```toml
multi_agent_mode = "off"
delegation_enabled = false
multi_agent_show_plan = true
multi_agent_max_parallel_tasks = 3
multi_agent_max_repair_iterations = 2
multi_agent_complexity_threshold = "medium"
```

Recommended rollout:

1. Keep `off` as default while context schemas stabilize.
2. Enable `always` only in fake-provider and local testing.
3. Enable `auto` for read-only analysis and documentation tasks.
4. Enable specialist research/test/review for complex tasks once packet schemas are stable.
5. Enable explicit user-visible repair prompts.
6. Only consider automatic repair after approval, persistence, and stop conditions are proven boringly reliable.

## Near-Term Priority Order

1. Stabilize typed context packet schemas and stable ids.
2. Add `TaskContext`, `AgentSessionState`, and `MultiAgentSessionState`.
3. Replace ad hoc metadata mutation with load/save helpers.
4. Convert post-check outputs into artifacts and packet references.
5. Execute research/test/review DAG nodes as specialists.
6. Implement explicit repair prompting that resumes prior execution state.
7. Add restart/resume tests for multi-agent session metadata.

## Bottom Line

Nexus should treat multi-agent coding as context engineering, not as a group chat. The codebase already has the hard runtime invariants in the right place: event-driven agent execution, centralized approvals, deterministic approved-tool resume, provider-safe history, and restricted worker registries.

The next work is to make the shared state layer as disciplined as the tool layer already is. Once tasks, agent summaries, handoff packets, artifacts, and repair state are typed and durable, Nexus can scale from "single agent with helper workers" into a real supervisor-led multi-agent coding system without flooding every agent with every conversation.
