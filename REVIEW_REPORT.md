# Nexus Agent Framework Review Report

## Review Scope

This review covers all implemented code under `nexus/`, the full test suite under `tests/`, and the design docs under `docs/action-plan/`. The review was conducted against the live codebase as of April 2026.

Review focus:

- correctness and completeness of the current runtime across all implemented subsystems
- accuracy of the previous review against the current code state
- how implemented pieces compose end-to-end
- concrete risks, behavioral gaps, and remaining test blind spots

---

## Executive Summary

The codebase has grown substantially beyond the initial scaffold. The core runtime, permission system, delegation, observability, MCP integration, sandbox, and post-session learning are all implemented and covered by a 67-test suite that passes cleanly. Module boundaries are well-defined and the type system is consistently applied throughout.

The previous review's two medium-severity findings have both been resolved in code and covered by regression tests. The remaining risks are now more nuanced: they involve edge cases in how subsystems compose rather than outright missing safety primitives.

Current overall assessment:

- architecture: good and expanding correctly
- implementation discipline: good
- runtime hardening: substantially improved, some edges remain
- production readiness: approaching, but several gaps listed below must be addressed first

**Test suite status: 67 tests, 67 passing** (Python 3.12, pytest-asyncio auto mode).

---

## Resolution Status Of Previous Findings

### Resolved

1. **Permission evaluation ignored tool arguments entirely.**
   Reference: `nexus/runtime/permissions.py`.
   The `PermissionChecker` now implements `_path_policy()` which inspects `write_note` path arguments. Writes outside the workspace root and writes into the `.nexus/` state directory are both hard-denied regardless of execution mode. Two regression tests cover this: `test_agent_hard_denies_write_note_outside_workspace_even_in_auto_mode` and `test_agent_hard_denies_write_note_into_internal_nexus_state`.

2. **`WriteNoteTool` had no content size guard.**
   Reference: `nexus/tools/builtin.py`.
   `WriteNoteTool` now enforces a configurable `max_bytes` limit (default 65,536). The limit is checked before writing and returns a typed error result without touching the filesystem. Test: `test_write_note_tool_rejects_large_content`.

### Unchanged / Still Present

3. **Memory search is a full-directory scan.**
   Reference: `nexus/memory/store.py:39-47`.
   `MemoryStore.search()` opens and parses every `.md` file in the memory directory on each query. No caching or index has been added. This is acceptable at current scale but will degrade linearly as memory volume grows.

4. **Session storage is not concurrency-hardened.**
   Reference: `nexus/runtime/sessions.py`.
   Atomic file replacement (`tmp.replace(target)`) is used to prevent partial writes, but there is no file locking or advisory lock against concurrent writers from separate processes. For single-process interactive use this is safe, but concurrent headless invocations sharing the same session directory can corrupt state.

---

## What Has Been Implemented

### Models (`nexus/models.py`)

Complete typed contract layer:

- `Message`, `ToolCall`, `ToolResult`, `UsageSnapshot`, `CorrelationContext`, `TurnTelemetry`
- `RuntimeRequest`, `RuntimeResponse` — provider-neutral wire boundary
- `ToolExecutionContext` — carries `session_id`, `working_directory`, and `metadata` dict used for turn/trace correlation
- `ConfirmationKind`, `ConfirmationRequest`, `ConfirmationResponse` — covers both APPROVAL and CLARIFICATION branches
- `AgentEvent` — generic typed event envelope yielded by the agent loop

All types use `dataclass(slots=True)` and `frozen=True` where appropriate. The `CorrelationContext` and `TurnTelemetry` types exist in models but are not directly emitted by the current agent loop; correlation fields are injected into hook payloads manually via `_correlation_payload()` instead.

### Configuration (`nexus/config/`)

`AgentConfig` dataclass with ~50 fields covering every runtime parameter. Config loading merges in this order: built-in defaults → global TOML → local TOML → `AGENT_*` environment overrides → CLI overrides.

Validation enforced at load time via `_validate_config_values()`:

- provider must be `fake`, `openai`, or `openai-compatible`
- `openai` and `openai-compatible` require `api_base_url`
- `default_mode` must be `plan`, `default`, or `auto`
- `log_format` must be `text` or `json`
- all integer fields must be positive
- `compaction_hard_limit >= compaction_soft_limit`
- `temperature` must be between 0.0 and 2.0
- `mcp_servers` entries must have `name` and `command`
- `delegation_workers` must be non-empty when delegation is enabled
- overlapping `allowed_tools` and `denied_tools` are flagged by the doctor check

The loader is tested for all these constraints in `test_config.py` (9 tests).

### Agent Loop (`nexus/runtime/agent.py`)

`Agent` is an async-generator-based loop. Each call to `run()` yields typed `AgentEvent` objects:

1. `thinking_started`
2. `model_response` — carries the raw `RuntimeResponse`
3. `tool_call_requested` — for each tool call in the response
4. `confirmation_requested` — if a required argument is missing (CLARIFICATION) or the tool needs approval (APPROVAL)
5. `tool_denied` — if the permission checker returns DENY
6. `tool_result` — after execution, carries `ToolResult`
7. `turn_completed` — with finish reason (`done` or `max_turns`)

Usage notifications are emitted via `HookEvent.NOTIFICATION` before returning the model response event when the response carries usage data.

Tool argument validation via `_missing_required_fields()` checks the JSON schema `required` array plus `minLength` constraints on string fields. The first missing field triggers a clarification and the loop returns immediately — it does not batch missing fields.

`_correlation_payload()` injects `turn_id`, `trace_id`, and optionally `worker_id` and `tool_call_id` into every hook payload, enabling downstream correlation in logs, metrics, and audit trail entries.

Tool execution duration is measured using `time.perf_counter()` and included in `POST_TOOL_USE` payloads.

### Permissions (`nexus/runtime/permissions.py`)

`PermissionChecker.evaluate()` applies decisions in this order:

1. **Path policy** (`_path_policy`): inspects `write_note` arguments to hard-deny writes outside the workspace or into `.nexus/`. Returns before any mode check.
2. **Plan mode deny**: mutating tools are denied in PLAN mode.
3. **Confirm**: mutating tools require confirmation in DEFAULT mode.
4. **Allow**: read-only tools are allowed automatically; all tools are allowed in AUTO mode.

The path policy currently only handles `write_note`. All other tools bypass argument inspection. This is a known architectural limit: tools registered from MCP, plugins, or the sandbox do not benefit from argument-aware policy unless they are individually added to `_path_policy`.

`MCPToolAdapter.is_mutating = True` unconditionally, meaning all MCP tools are treated as mutating regardless of their actual read/write semantics. This causes all MCP tools to require confirmation in DEFAULT mode even if they are read-only queries.

### Sessions (`nexus/runtime/sessions.py`)

`SessionSnapshot` serializes via `to_dict()` / `from_dict()`. `SessionStore.save()` uses atomic write: JSON is written to a `.tmp` file and then replaced with `Path.replace()`. After each save, `latest_session.txt` is updated with the session ID. `_prune_if_needed()` enforces `max_sessions_retained` by deleting the oldest sessions sorted by `updated_at`.

`EphemeralSessionStore` overrides `save()` to a no-op and `load()` to return a fresh snapshot, supporting `--no-session` mode without any filesystem state.

### REPL And Headless (`nexus/runtime/repl.py`, `nexus/cli/headless.py`)

`collect_turn_events()` manages the approval/clarification retry loop:

- Collects all events from one `agent.run()` pass.
- If a `confirmation_requested` event is found, it either calls the `approval_callback`, accumulates approved tools, or injects clarification text and re-invokes the agent.
- Turn-level telemetry (`_record_turn_telemetry`) is appended to session metadata at the end of the outer loop with status `completed`, `awaiting_confirmation`, or `stopped`.

`apply_events_to_history()` accumulates usage from model response events into `session.metadata["usage"]` and saves the session if `save_on_every_turn` is enabled.

Headless mode exits with `EXIT_NEEDS_CONFIRM` (code 3) if a confirmation is required without `--auto-confirm`. JSON and JSONL outputs emit to stdout when no output file is specified. Quiet mode suppresses tool-call output.

### Context And Compaction (`nexus/runtime/context.py`)

`ContextBuilder.build()` assembles the system prompt from typed `ContextSections`: base instruction, environment, tools, skills, project notes, carry-over, and task focus.

`ContextCompactor` applies two-phase compaction:

1. `should_compact()` checks estimated total tokens against `compaction_soft_limit`.
2. `compact()` splits history into older and recent portions, appends a summarized version of the older portion to `CarryOverState.summarized_history`, then calls `compact_messages()` to trim the recent portion to `compaction_hard_limit`.

`TokenEstimator` uses `len(text) // 4` as a naive byte-to-token heuristic. This is a rough approximation that will be inaccurate for multilingual text, tool result payloads with structured JSON, or model-specific tokenizers.

**Double-compaction issue**: In `collect_turn_events()`, `compact_messages()` is called unconditionally after the conditional `compactor.compact()` call. Both calls operate on the same `model_messages` list. The second `compact_messages()` call applies the hard limit again to already-compacted messages. For most interactions this produces the same result, but when the compactor produces a carry-over summary that pushes the recent set above the hard limit, a second trim occurs silently. This is a logic inconsistency rather than a correctness failure, but it can lose messages.

**Tool-result orphaning risk**: When compaction splits older messages from recent messages, it does not account for the provider's requirement that tool result messages immediately follow the assistant message that requested them. If an assistant message requesting tools is compacted into the carry-over summary while its corresponding tool result messages appear in the recent window, the message sequence will be structurally invalid for a live provider. The fake client is immune because it does not validate this, but the OpenAI-compatible client will receive an invalid message array.

### Observability (`nexus/observability/`)

**Hook executor (`nexus/runtime/hooks.py`)**: `HookExecutor.emit()` catches all exceptions per handler individually, so one failing handler does not prevent subsequent handlers from running. Failures are logged at `WARNING` level.

**JSONL runtime logger (`nexus/observability/logging.py`)**: `JsonlRuntimeLogger.log()` writes a timestamped event record. File write failures are swallowed and logged. `redact_payload()` replaces values for keys matching `SENSITIVE_KEYS` (`api_key`, `authorization`, `token`, `cookie`, `password`) at any nesting depth. The prompt body is replaced with a char count and 80-char preview before logging.

**Metrics collector (`nexus/observability/metrics.py`)**: `RuntimeMetricsCollector` maintains in-memory counters across prompt submissions, tool calls, token usage, confirmations, clarifications, denials, and stop events — both in aggregate and per-session and per-tool. `_flush()` is called on every `record_*` call, doing a full JSON serialize and atomic file replace each time. On a long session with many tool calls, this generates one file write per event. The sync file I/O inside an async method is not awaited and runs on the event loop thread without offloading.

**Audit trail (`nexus/observability/audit.py`)**: `JsonlAuditTrail.write()` records `DangerousActionRecord` entries for `confirmation_requested` and `tool_denied` notifications, and for all mutating `POST_TOOL_USE` events. Each record includes `action_id`, `action_name`, `scope`, `state`, `danger_level`, rollback plan, and correlation fields. `classify_danger()` categorizes by tool name but discards the `arguments` parameter with `del arguments` — argument-sensitive danger classification (e.g., path-based risk of a write) cannot be expressed in the current implementation. `rollback_plan()` for `write_note` includes the target path, which is correct.

### Delegation (`nexus/runtime/delegation.py`)

Full coordinator-worker model:

- **`InMemoryMailbox`**: per-recipient `asyncio.Queue` with a bounded `deque` history. Sends emit `NOTIFICATION` hook events for upstream observability.
- **`TaskRecord`**: coordinator-owned mutable task state with `TaskStatus` enum, notes list, resource version snapshot, and pending decision ID.
- **`ResourceVersionStore`**: optimistic concurrency control via per-resource integer versions. `try_commit()` checks all claimed resources against expected versions atomically within the single asyncio event loop, then increments on success.
- **`WorkerAgent`**: receives `COMMAND` messages, runs an inner `Agent` loop with a restricted tool registry and the shared model client factory, routes APPROVAL requests back to the coordinator via the mailbox, and fails immediately on CLARIFICATION requests (clarification is not interactive for workers).
- **`DelegationRuntime`**: coordinator loop polls the mailbox and routes incoming STATUS/RESULT/PERMISSION messages. Permission decisions land in `pending_permissions` until resolved via `decide_permission()`. Tasks wait on `asyncio.Future` objects for approval responses. The coordinator selects workers round-robin unless overridden by `DelegationRequest.assigned_worker`.

The `WorkerAgent._execute_command()` creates `ToolExecutionContext` with `working_directory=Path.cwd()` rather than the configured `workspace_root`. This means worker-executed `WriteNoteTool` operations enforce boundaries against the process working directory, not the configured workspace path. If the process is started from outside the workspace, the boundary check will be against the wrong root.

### Sandbox (`nexus/runtime/sandbox.py`)

`DockerSandbox.run()` constructs a `docker run` command with:

- `--rm` (auto-cleanup)
- `--interactive`
- `--memory=<limit>` (default 256m)
- `--network=<network>` (default none)
- `--security-opt no-new-privileges`
- `--cap-drop ALL`
- `--pids-limit 50`
- `-w /workspace`
- `-v <workspace>:/workspace:<ro|rw>`
- `--tmpfs /tmp:size=<tmp_size>`

Timeout is enforced via `asyncio.wait_for`. On timeout, the process is killed and a typed error `ToolResult` is returned. On `FileNotFoundError` (Docker not in PATH), a typed error is returned without crashing. `SandboxedCommandTool` still registers as `is_mutating = True` and goes through the permission system, requiring confirmation in DEFAULT mode.

The `SandboxedCommandTool` checks for a missing `command` argument and returns an error result with a clear message rather than raising.

### MCP Integration (`nexus/integrations/mcp.py`)

`MCPClient` implements JSON-RPC over subprocess stdio. The `_read_response()` loop reads lines until it finds the line matching the expected request ID, discarding all others. There is no counter bounding how many non-matching lines are discarded before timeout. A misbehaving server that floods stdout could keep this loop running until the per-method `timeout` fires. The timeout default is 10 seconds.

`MCPToolAdapter.is_mutating = True` for all MCP tools. This is a safe default but may over-restrict genuinely read-only MCP tools in DEFAULT mode by requiring confirmation for every call.

`MCPServerRuntime.refresh()` re-creates the client on each refresh call if the client was closed. The registration flow in `app.py` loops over `await runtime._list_tools()` twice when registering — once to get display names via `refresh()` and once inside the inner loop to resolve the `MCPToolSpec` for the adapter. For large MCP servers with many tools, this doubles the tool-list RPC overhead at startup.

### Skills (`nexus/skills.py`)

`load_skill_registry()` scans `*/SKILL.md` under configured directories. Local skills override global ones when names collide (later directory wins because `register()` overwrites). Skills are activated per-session via `--skill` flag or `/skills add`. Active skill content is injected into the system prompt in `build_context_sections()`. The summary line is taken from the first non-empty non-header line, capped at 120 characters.

The model does not auto-select skills; activation is always explicit.

### Post-Session Learning (`nexus/runtime/post_session.py`)

`run_post_session_updates()` runs synchronously after each completed session:

1. Extracts facts from the session transcript using `FACT_PATTERNS` (regex for venv paths, test commands, build commands).
2. Updates `.nexus/facts.json` with merged facts, tech stack inferences, recent tasks, tool names used, and session count.
3. Writes `.nexus/knowledge.md` as a human-readable Markdown summary of the workspace state.
4. Updates `~/.nexus/workspaces.json` with workspace-level data keyed by absolute path.
5. Writes `~/.nexus/profile.md` aggregating tool preferences and workflow patterns across workspaces.

All writes use atomic replacement. The entire function is wrapped in a broad `except Exception` that logs and swallows failures.

`FACT_PATTERNS` match anywhere in the transcript text, including in examples, comments, and instructions that mention commands without actually running them. This can produce false-positive facts.

### Doctor (`nexus/cli/doctor.py`)

Four gate structure: Runtime Integrity, Safety Integrity, Operational Integrity, Extension Integrity. Gates contain named checks with pass/warn/fail states. The overall report status is the worst gate status.

Checks cover: workspace root existence, tool registry non-empty, audit logging enabled, default mode not `auto`, `write_note_max_bytes > 0`, sandbox availability if enabled, session/memory/log/plugins directory writability, MCP connectivity, and overlapping tool filter detection.

Output supports `text`, `json`, and `jsonl` formats. JSON output writes directly to `console.file` to support stdout redirection.

### OpenAI-Compatible Provider (`nexus/integrations/openai_compatible.py`)

`OpenAICompatibleAdapter` translates `RuntimeRequest` to OpenAI wire format and `RuntimeResponse` from the wire response. System prompt is prepended as the first message. Tool schemas pass through directly as the `tools` array.

`OpenAICompatibleModelClient` uses `urllib.request` (stdlib, no dependencies) with `asyncio.to_thread` to offload the blocking HTTP call. Retryable status codes: 408, 409, 429, 500, 502, 503, 504. `URLError` is also retried. Non-retryable HTTP errors raise `RuntimeError` with the response body.

The client does not support streaming. The `FakeModelClient` has a `stream()` method defined but it is not called anywhere in the runtime.

### Plugins (`nexus/extensions/plugins.py`)

`PluginLoader.load_all()` scans `*.py` files in the configured plugins directory. Each module is loaded via `importlib` and must expose a `register(registry, hooks)` function. Load failures and missing `register()` functions produce warnings, not hard failures. The `_PluginRegistryView` applies the `can_register` allow policy before delegating to the real registry, preventing plugin tools from bypassing `allowed_tools` / `denied_tools` config.

---

## How The System Works End-To-End

### Startup

`nexus.app:main` parses CLI arguments and immediately loads config with `load_config()`, which validates and raises `ConfigError` on any invalid value. Config errors exit with code 1 and a human-readable message before any workspace state is touched.

After config, `_run_runtime()` ensures directories exist, bootstraps workspace knowledge if absent, wires hooks and audit trail, builds the tool registry (plugins, MCP, sandbox), constructs the `Agent`, selects or creates a session, loads the skill registry, and routes into REPL or headless execution.

### Config Hierarchy

The five-layer merge (defaults → global → local → env → CLI) is applied by `load_config()`. Path fields are resolved to absolute paths relative to `workspace_root`. All field types are coerced to their declared Python types. Environment variables use the `AGENT_<FIELD_NAME>` convention with `AGENT_MAX_TOKENS` as a legacy alias for `compaction_hard_limit`.

### Agent Turn

1. `collect_turn_events()` in `repl.py` wraps the agent loop with confirmation/clarification retry.
2. The system prompt is assembled from `build_context_sections()` using current mode, tool list, active skills, carry-over state, and workspace knowledge.
3. Message history is optionally compacted via `ContextCompactor` before being sent to the model.
4. `Agent.run()` is called, yielding events. The loop continues until `turn_completed` or a confirmation is received without a callback to resolve it.
5. Approved tools accumulate in `approved_tools` set so re-entering the loop after approval does not re-trigger confirmation for the same tool.
6. Turn telemetry (tool count, duration, status) is appended to session metadata.

### Permission Decisions

`PermissionChecker.evaluate()` applies path policy first, then mode checks. The mode hierarchy is: PLAN blocks all mutating tools; DEFAULT requires confirmation for mutating tools; AUTO allows everything. Read-only tools are always allowed unless the path policy applies.

`approved_tools` in `collect_turn_events()` accumulates approvals within the retry loop so one confirmation unlocks the tool for the rest of the turn.

### Delegation

The coordinator loop polls the mailbox and dispatches RESULT, STATUS, and PERMISSION messages. Workers receive COMMAND messages, run the inner agent loop with a restricted tool registry, and send back STATUS updates during execution. Approval requests pause the worker loop on an `asyncio.Future` until the coordinator receives a `decide_permission()` call from outside (e.g., from the `/delegate approve` slash command in the REPL). Resource claims are checked via `ResourceVersionStore.try_commit()` before the coordinator routes the COMMAND to a worker.

### Observability Pipeline

All runtime events flow through `HookExecutor.emit()`. The audit hook filters `confirmation_requested` and `tool_denied` notifications plus all mutating `POST_TOOL_USE` events. The runtime logger records all events with payload redaction. The metrics collector updates in-memory counters and flushes to `metrics.json` after every event. The audit trail writes to a separate `audit-trail.jsonl` file.

---

## Findings

### Medium Severity

1. **Double-compaction in `collect_turn_events()` can silently drop messages.**
   Reference: `nexus/runtime/repl.py`, lines where `compact()` and `compact_messages()` are both called on `model_messages`.
   The conditional `compactor.compact()` call is followed by an unconditional `compact_messages()` call on the same list. After compaction, `compact_messages()` trims again to the hard limit. In most cases this is idempotent, but when the compactor's carry-over summary causes the recent window to approach the hard limit, the second pass silently removes messages that the compactor intended to keep. There is no log or event indicating that the second compaction ran.
   Impact: message loss without operator visibility; harder to debug with live providers.

2. **Tool result orphaning after compaction with live providers.**
   Reference: `nexus/runtime/context.py:ContextCompactor.compact()`.
   When the history is split into older and recent portions, the compaction boundary may fall between an assistant message that requested tool calls and the tool result messages that answered them. The recent window then contains tool result messages with no corresponding tool request context. The OpenAI-compatible provider (and other compliant providers) will reject this as a malformed message sequence.
   Impact: the live provider path will produce errors on long sessions that cross the compaction boundary.

3. **`MCPToolAdapter.is_mutating = True` for all MCP tools.**
   Reference: `nexus/integrations/mcp.py:MCPToolAdapter`.
   Every MCP tool is registered as mutating regardless of the tool's actual behavior. In DEFAULT mode, this forces confirmation before every MCP tool call, even for read-only queries like file listings or searches. The MCP spec does not expose a `readOnly` attribute in tool metadata, so this cannot be inferred automatically from the spec, but the `MCPServerConfig` or per-server config could allow an operator to designate tool prefixes as read-only.
   Impact: degraded UX for read-only MCP tools; operators using MCP in DEFAULT mode must constantly confirm safe operations.

4. **Worker `ToolExecutionContext` uses `Path.cwd()` rather than configured workspace root.**
   Reference: `nexus/runtime/delegation.py:WorkerAgent._execute_command()`.
   Worker agents construct `ToolExecutionContext(working_directory=Path.cwd())`. The `WriteNoteTool` enforces its workspace boundary against this path. If the Nexus process is started from a directory other than the project workspace, worker-executed writes will enforce boundaries against the wrong root. Worker agents should inherit `working_directory` from the coordinator's configured `workspace_root`.
   Impact: boundary enforcement for worker-executed mutating tools may be applied against the wrong directory.

5. **Metrics collector flushes synchronous file I/O on every event.**
   Reference: `nexus/observability/metrics.py:RuntimeMetricsCollector._flush()`.
   `_flush()` is synchronous despite being called from `async` methods. It calls `tmp.write_text()` and `tmp.replace()` on the event loop thread without `asyncio.to_thread()`. For sessions with many tool calls, this writes `metrics.json` dozens of times per session on the main loop thread.
   Impact: minor latency on the event loop during busy sessions; negligible for a dev tool but worth addressing before production scale.

### Low Severity / Residual Risks

6. **`classify_danger()` discards arguments with `del`.**
   Reference: `nexus/observability/audit.py:classify_danger()`.
   The function accepts `arguments: dict[str, Any]` but deletes it immediately with `del arguments`. The danger level for `write_note` is always `HIGH` regardless of target path. This prevents expressing argument-sensitive risk levels in audit records (e.g., writing to a sensitive subdirectory vs. a scratch file).
   Impact: audit classification is less precise than it could be.

7. **MCP `_read_response()` discards non-matching response lines without bound.**
   Reference: `nexus/integrations/mcp.py:MCPClient._read_response()`.
   The `while True` loop reads stdout lines until it finds one matching the current request ID, silently discarding others. A misbehaving or noisy MCP server could stall this loop until the 10-second timeout fires. There is no counter or maximum discard limit.
   Impact: slow failure recovery from misbehaving MCP servers; 10-second blocking delay before an error is raised.

8. **`FACT_PATTERNS` regex matching can produce false positives in post-session learning.**
   Reference: `nexus/runtime/post_session.py:FACT_PATTERNS`.
   Patterns match anywhere in the session transcript, including in model explanations, examples, and instructions. Mentioning `pytest` in a sentence does not mean the user ran tests. Facts accumulated this way may be inaccurate and get merged into workspace knowledge persistently.
   Impact: corrupted or misleading workspace knowledge entries over time.

9. **Memory search is still a full-directory scan.**
   Reference: `nexus/memory/store.py:MemoryStore.search()`.
   All `.md` files in the memory directory are opened, read, and parsed on each search call. No caching or index exists. For the current typical memory store size this is acceptable, but there is no natural limit that prevents the store from growing until search becomes a meaningful bottleneck.
   Impact: latency degrades linearly with memory store size.

10. **Session storage is not concurrency-hardened for multi-process access.**
    Reference: `nexus/runtime/sessions.py:SessionStore`.
    Atomic write (`tmp.replace()`) prevents partial-write corruption but does not prevent two concurrent processes from each reading the current state, modifying it, and writing back independently, with one overwriting the other's changes. For single-interactive-user single-process use this is safe.
    Impact: data loss if two headless sessions run concurrently against the same session store.

---

## Testing Coverage Review

**Total: 67 tests, 67 passing.**

### What Is Well Covered

| Area | File | Count |
|---|---|---|
| Agent loop — tool dispatch, permissions, clarification | `test_agent.py` | 5 |
| CLI arg mapping, headless flow, `--no-session`, doctor | `test_cli.py` | 7 |
| Config merge, validation, all constraint types | `test_config.py` | 9 |
| Delegation — task lifecycle, permission routing, resource conflict | `test_delegation.py` | 5 |
| Hooks — PRE/POST tool, usage, isolation, audit trail, metrics, redaction | `test_hooks.py` | 8 |
| MCP — live subprocess client, tool adapter, registry integration | `test_mcp.py` | 2 |
| OpenAI-compatible — wire format, retry, client factory | `test_openai_compatible.py` | 3 |
| Plugins — load, policy, /tools slash command | `test_plugins.py` | 3 |
| Post-session — fact extraction, workspace + profile writes | `test_post_session.py` | 2 |
| Prompts — mode injection, unreadable knowledge, skill + carry-over | `test_prompts.py` | 3 |
| Retry — backoff, non-retryable classification | `test_retry.py` | 2 |
| Sandbox — Docker boundary, missing argument | `test_sandbox.py` | 2 |
| Sessions — round-trip, pruning, latest pointer | `test_sessions.py` | 3 |
| Slash commands — mode, quoting, MCP, delegate, skills, session export | `test_slash_commands.py` | 9 |
| Tools — get_time, write_note boundaries, size limit | `test_tools.py` | 4 |

### Coverage Gaps

- **No test for context compaction edge cases.** The double-compaction issue in `collect_turn_events()` and the tool-result orphaning risk are not covered by any test. The `ContextCompactor` and `compact_messages()` functions have no direct unit tests.
- **No test for the live OpenAI-compatible path with tool calls.** The existing `test_openai_compatible.py` tests cover a simple completion request but not the full round-trip when the provider returns tool calls and the adapter maps them through to `RuntimeResponse.tool_calls`.
- **No test for the MCP `_read_response()` discard loop behavior.** Non-matching response ID handling is untested.
- **No test for multi-turn delegation with tool execution inside a worker.** The delegation tests use the `FakeModelClient` which is scripted or uses heuristic matching. No test exercises a multi-turn worker loop where the inner agent makes multiple tool calls across turns.
- **No test for skill loading when the same skill name exists in both global and local directories.** The override behavior (local wins) is undocumented and untested.
- **No test for `metrics.json` content across a multi-tool session.** The metrics collector is partially covered but not verified for per-session and per-tool counter accuracy across multiple tool calls in one session.
- **No end-to-end CLI test invoking `main()` from the script entrypoint.** The `test_cli.py::test_main_doctor_outputs_json_report` test does call `main()` for the doctor subcommand, but no test exercises the full REPL or headless path through `main()` including the actual startup sequence.
- **No test for post-session false-positive fact extraction.** The test verifies that facts are found, not that they are accurate.
- **No test for concurrent session writes.** The atomicity of `SessionStore.save()` is implicitly exercised but not tested under concurrent access.

---

## Stability Assessment

### Stable Areas

- Config loading, validation, and the five-layer merge hierarchy.
- Session round-trip, retention pruning, and ephemeral mode.
- Prompt assembly under normal conditions and with unreadable knowledge files.
- Tool registration, `is_mutating` flag, schema dispatch, and workspace boundary enforcement in `WriteNoteTool`.
- Approval and clarification pause-and-resume in the single-agent loop.
- Hook execution with failure isolation.
- JSONL runtime logging with payload redaction.
- Delegation task lifecycle, permission routing, resource version conflict detection.
- MCP client connect, list, and call cycle with a cooperative server.
- Plugin loading with allow policy.
- Post-session atomic writes to workspace and profile files.

### Less Stable Areas

- **Live provider sessions with long history** due to double-compaction and tool-result orphaning risk.
- **Worker tool execution paths** due to `Path.cwd()` working directory issue.
- **Large memory stores** due to full-directory search scan.
- **High-throughput sessions** due to per-event synchronous metrics flush.
- **MCP servers with chatty notification streams** due to unbounded `_read_response()` discard loop.
- **Concurrent multi-process headless execution** sharing the same session directory.

---

## Recommended Next Fixes

Ordered by impact and difficulty:

1. **Fix worker `ToolExecutionContext` to use `config.workspace_root` instead of `Path.cwd()`.**
   Low effort, high safety impact. The delegation runtime has access to `working_directory` via the model client factory closure; it should also receive the workspace root.

2. **Fix the double-compaction in `collect_turn_events()`.**
   Remove the unconditional `compact_messages()` call that follows the conditional `compactor.compact()` call, or merge the two into a single compaction pass.

3. **Guard the compaction boundary against tool-result orphaning.**
   `ContextCompactor.compact()` should ensure the recent window starts at an assistant or user message, not at a tool result message. Walk backwards from the split point to find the nearest safe split boundary.

4. **Offload `RuntimeMetricsCollector._flush()` to `asyncio.to_thread()`.**
   The sync file write in an async path should be wrapped to avoid blocking the event loop.

5. **Add a cap on discarded lines in `MCPClient._read_response()`.**
   Track a discard counter and raise a `RuntimeError` once a reasonable limit (e.g., 1,000 lines) is exceeded, rather than running until timeout.

6. **Make `MCPToolAdapter.is_mutating` configurable per server or per tool prefix.**
   Expose an `mcp_read_only_prefixes` or `mcp_read_only_tools` config field that marks designated MCP tools as non-mutating so they bypass confirmation in DEFAULT mode.

7. **Pass tool arguments into `classify_danger()` rather than discarding them.**
   Remove `del arguments` and use the path argument for `write_note` to produce more specific danger level annotations in the audit trail.

8. **Add unit tests for `ContextCompactor` edge cases** covering the compaction boundary, tool-result orphaning, and the compactor-disabled path.

9. **Add a test for skill name collision between global and local directories** to verify and document the override behavior.

10. **Decide whether session persistence needs explicit locking for concurrent headless use.** If concurrent headless invocations are a supported use case, add an advisory lock or migrate to a process-safe store. If not, document the constraint explicitly.

---

## Final Assessment

The Nexus Agent Framework has progressed significantly from its initial scaffold state. All core subsystems are implemented, all 67 tests pass, and the architecture remains coherent as new capabilities have been added. The permission system is now argument-aware for the built-in `write_note` tool, the delegation runtime handles multi-agent coordination correctly, the observability pipeline covers audit, metrics, and event logging, and the CLI surface provides a complete operator interface.

The remaining risks are no longer about missing safety primitives — they are about correctness edge cases in subsystem composition. The most actionable items are the double-compaction issue (silent message loss), the worker working-directory mismatch (boundary enforcement against wrong root), and the MCP `is_mutating` over-classification (constant confirmation friction for read-only MCP tools). All three are narrowly scoped fixes that do not require architectural changes.

The system is not yet at a production deployment threshold. The gaps in live-provider compaction safety, metrics I/O on the event loop, and concurrent session access would need to be addressed before treating this as an infrastructure component. For continued local development and experimentation, the current codebase is stable and reliable.