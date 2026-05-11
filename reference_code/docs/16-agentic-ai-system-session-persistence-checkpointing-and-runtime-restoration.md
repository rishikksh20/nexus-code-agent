# 16. Agentic AI System Session Persistence, Checkpointing, and Runtime Restoration: Durable Conversation State, Save/Resume Workflows, and Snapshot-Based Recovery

This document is a continuation of:

- `docs/01-agentic-ai-system-basics.md`
- `docs/02-agentic-ai-system-runtime-and-error-paths.md`
- `docs/03-agentic-ai-system-context-management-and-prompt-construction.md`
- `docs/04-agentic-ai-system-tool-calling-and-execution-runtime.md`
- `docs/05-agentic-ai-system-configuration-and-environmental-context-management.md`
- `docs/06-agentic-ai-system-session-runtime-ownership-and-state-boundaries.md`
- `docs/07-agentic-ai-system-builtin-tool-expansion-and-interactive-tool-observability.md`
- `docs/08-agentic-ai-system-search-and-discovery-tool-expansion.md`
- `docs/09-agentic-ai-system-web-discovery-and-tool-result-specialization.md`
- `docs/10-agentic-ai-system-subagent-delegation-and-prompt-surface-specialization.md`
- `docs/11-agentic-ai-system-mcp-tool-integration-and-runtime-management.md`
- `docs/12-agentic-ai-system-context-compaction-summary-restoration-and-tool-output-pruning.md`
- `docs/13-agentic-ai-system-safety-and-approval-mechanisms.md`
- `docs/14-agentic-ai-system-hook-system.md`
- `docs/15-agentic-ai-system-loop-detection-and-loop-breaking.md`

`01` established the client and event foundations.
`02` introduced the CLI/agent/TUI runtime shell.
`03` added context management and prompt construction.
`04` introduced structured tool calling.
`05` expanded configuration and environment-aware runtime setup.
`06` moved key runtime ownership under `Session`.
`07` expanded builtin tools and interactive observability.
`08` added stronger local discovery tools.
`09` expanded discovery outward through web tooling.
`10` added local custom tool discovery.
`11` added MCP-backed external tool integration.
`12` introduced context compaction, summary restoration, and tool-output pruning.
`13` added approval and safety control paths.
`14` added hooks as event-driven runtime extension points.
`15` activated loop detection and prompt-based loop breaking inside the live agentic loop.
`16` explains the next practical runtime boundary: how a live session can be saved, checkpointed, listed, resumed, and restored so work can survive beyond one in-memory interactive process.

In this stage, the code adds:

- a new persistence module in `core/agent/persistence.py`,
- a serializable `SessionSnapshot` format,
- a `PersistenceManager` for session and checkpoint files,
- interactive commands in `main.py` for `/save`, `/sessions`, `/resume`, `/checkpoint`, and `/restore`,
- session statistics exposure through `Session.get_stats()`,
- and a small `TUI` typing adjustment related to nullable exit codes.

This document serves two purposes:

1. explain what sessions and checkpoints are, why they matter in agent systems, and how they differ conceptually, and
2. show exactly how this repository implements persistence and restoration today, including the current operational nuances and edge cases.

---

## 1. High-level change in this iteration

The project shifts from a **rich but mostly in-memory interactive session runtime** to a **runtime that can persist session state to disk and later reconstruct a working session from saved snapshots**.

Previous effective flow (from `15`):

`main.py (CLI)` -> interactive `Agent` session runs in memory -> conversation, turn counters, and token usage live inside the process -> when the process exits, the live session state is gone unless the user manually replays context

Current flow (this `16` step):

`main.py (CLI)` -> interactive `Agent` session runs in memory -> user can save the current session or create a checkpoint snapshot -> snapshots are written to disk under the agent data directory -> later, the user can list sessions, resume a named session, or restore from a checkpoint -> a fresh `Session` is initialized and then repopulated from the saved snapshot

That is the main conceptual shift.

The project is no longer only about how the agent behaves *during* a single live runtime.
It is also beginning to care about how that runtime state can persist *across* runs.

This is a major usability step because long-running agent work often needs:

- interruption tolerance,
- manual save points,
- the ability to return to prior work,
- and a way to preserve conversational and operational context without redoing everything.

---

## 2. What a session is and why it matters

In this repository, a **session** is the runtime-scoped bundle of state that defines one agent conversation and its execution environment.

Earlier docs, especially `06`, already established that a `Session` owns key runtime resources such as:

- the model client,
- the tool registry,
- the context manager,
- MCP manager,
- approval manager,
- hook system,
- loop detector,
- compaction support,
- session timestamps,
- and turn counters.

### 2.1 Why sessions matter conceptually

Sessions matter because they define the boundary of continuity.

A session answers questions like:

- which conversation are we in,
- what has already been said,
- which tools were available,
- how many turns have happened,
- how much usage has accumulated,
- and which runtime managers are currently active.

Without a session boundary, persistence becomes ambiguous.
You can save text, but not the larger working state that makes the conversation operationally meaningful.

### 2.2 Why persistence matters for sessions

A purely in-memory session is fragile.
It disappears when:

- the process exits,
- the terminal closes,
- the system reboots,
- or the user simply wants to pause and continue later.

So session persistence is about durability of runtime context, not only convenience.

---

## 3. What a checkpoint is and how it differs from a saved session

This iteration introduces two related but distinct persistence concepts:

- **saved sessions**
- **checkpoints**

### 3.1 Saved session

A saved session is the durable file representation of the current logical session.

In this repo, it is keyed by:

- `session_id`

and stored as:

- `sessions/<session_id>.json`

Conceptually, this is like saving “the current canonical state of this session.”

### 3.2 Checkpoint

A checkpoint is a time-stamped snapshot of session state.

In this repo, it is keyed by:

- `<session_id>_<timestamp>`

and stored as:

- `checkpoints/<checkpoint_id>.json`

Conceptually, this is like creating a restore point rather than updating the main saved session file.

### 3.3 Why both are useful

These two concepts solve different user needs.

- **Save session** is for ongoing continuity.
- **Checkpoint** is for branching, experimentation, or rollback.

Examples:

- save the session before closing your laptop,
- checkpoint before trying a risky refactor,
- resume the last saved session tomorrow,
- restore an older checkpoint if the recent conversation drifted or went wrong.

That distinction gives the runtime a more practical workflow model.

---

## 4. Change scope since the previous commit

### New module introduced

- `core/agent/persistence.py`

### Existing files updated

- `core/agent/session.py`
- `main.py`
- `core/ui/tui.py`
- `core/hooks/__init__.py` (empty package marker added)

### Center of gravity in this change

The center of gravity is the introduction of a **snapshot format** and a **CLI-driven persistence surface**.

If `15` was about *making the live runtime more self-correcting*, then `16` is about *making the live runtime more durable and resumable*.

---

## 5. New persistence module: `core/agent/persistence.py`

This new module is the foundation of the feature.

It introduces:

- `SessionSnapshot`
- `PersistenceManager`

These two pieces separate:

- what is persisted,
- from how it is stored and retrieved.

That is a good design split.

---

## 6. `SessionSnapshot`: the persisted shape of runtime state

`SessionSnapshot` is a `dataclass` representing the saved form of a session.

### 6.1 Fields captured

It stores:

- `session_id`
- `created_at`
- `updated_at`
- `turn_count`
- `messages`
- `total_usage`

This is a meaningful selection.

It preserves:

- session identity,
- temporal metadata,
- progression metadata,
- the conversational transcript shape,
- and cumulative token accounting.

### 6.2 Why these fields matter

These are the fields most needed to resume work with continuity.

They answer:

- which session this is,
- when it started and last changed,
- how many turns it has seen,
- what messages exist,
- and how much total model usage has accumulated.

This is enough to reconstruct a workable `ContextManager` state even though it is not a perfect capture of every live runtime object.

### 6.3 Serialization format

`SessionSnapshot.to_dict()` converts the dataclass into a JSON-serializable form.

Important conversions include:

- `datetime -> isoformat()` strings
- `TokenUsage -> __dict__`

And `SessionSnapshot.from_dict(...)` reconstructs those fields on load.

That is straightforward and effective.

### 6.4 Why a custom snapshot dataclass is useful

The repository could have dumped ad-hoc dicts directly from `main.py`.
That would work initially, but it would mix UI command code with persistence schema logic.

Instead, the snapshot dataclass creates a cleaner contract between:

- runtime state,
- persistence layer,
- and command/UI layer.

---

## 7. `PersistenceManager`: filesystem-backed session and checkpoint storage

`PersistenceManager` is the operational layer that writes and reads snapshots.

### 7.1 Directory structure

On construction it resolves the agent data directory via:

- `get_data_dir()` from `core.config.loader`

and then creates:

- `sessions/`
- `checkpoints/`

under that data directory.

So persistence is kept inside the same broader application-data root that the repo already uses for runtime data such as memory.

### 7.2 File permissions

The manager applies restrictive permissions:

- directories: `0o700`
- files: `0o600`

This is an important implementation detail.

Saved sessions may contain:

- user messages,
- tool outputs,
- internal summaries,
- possibly sensitive context.

So private-by-default permissions are a good choice.

### 7.3 Session save/load

For normal session persistence:

- `save_session(...)` writes `sessions/<session_id>.json`
- `load_session(session_id)` reads it back if it exists

This makes saved sessions stable and directly addressable by session id.

### 7.4 Session listing

`list_sessions()` scans `sessions/*.json` and returns lightweight metadata:

- `session_id`
- `created_at`
- `updated_at`
- `turn_count`

The list is sorted by `updated_at` descending, so the most recently updated sessions appear first.

This supports a simple “what saved sessions do I have?” user workflow.

### 7.5 Checkpoint save/load

For checkpoints:

- `save_checkpoint(...)` creates a timestamp string using `%Y%m%d_%H%M%S`
- builds `checkpoint_id = <session_id>_<timestamp>`
- and writes `checkpoints/<checkpoint_id>.json`

This is a simple and usable naming scheme.

It preserves:

- which session the checkpoint came from,
- and when it was created.

`load_checkpoint(checkpoint_id)` then reads that exact file back.

### 7.6 Why sessions and checkpoints are stored separately

This is a strong design detail.

It prevents the conceptual confusion of:

- “the latest saved form of a session”

from being mixed together with:

- “many historical snapshots of that session.”

That separation makes user intent clearer and simplifies file semantics.

---

## 8. Session statistics now expose persistence-relevant metadata

`core/agent/session.py` now adds:

- `get_stats()`

This method returns a dict containing:

- `session_id`
- `created_at`
- `turn_count`
- `message_count`
- `token_usage`
- `tools_count`
- `mcp_servers`

### 8.1 Why this matters

Although `get_stats()` is not itself persistence, it provides the kind of runtime observability that makes persistence more understandable.

When a user saves, resumes, or restores a session, it is helpful to be able to inspect:

- how large that session is,
- how many turns it includes,
- and what general runtime surface is active.

So this is a small but relevant part of the same usability story.

---

## 9. CLI integration: persistence becomes an operator-facing workflow

The most user-visible changes are in `main.py`, specifically in `CLI._handle_command(...)`.

This is where persistence stops being an internal utility and becomes part of the interactive console experience.

### 9.1 `/save`

`/save` creates a `SessionSnapshot` from the current live session and writes it via `PersistenceManager.save_session(...)`.

The snapshot currently includes:

- session id
- timestamps
- turn count
- `context_manager.get_messages()`
- cumulative usage

This is the basic durable-save operation.

### 9.2 `/sessions`

`/sessions` lists saved sessions using `PersistenceManager.list_sessions()`.

That gives the user a quick overview of resumable sessions.

### 9.3 `/resume <session_id>`

`/resume` loads a saved session snapshot, creates a fresh `Session`, initializes it, then repopulates the context manager from the stored messages.

This is the main “continue previous work” workflow.

### 9.4 `/checkpoint`

`/checkpoint` creates a `SessionSnapshot` and stores it as a timestamped checkpoint rather than overwriting the session save slot.

This is the explicit restore-point workflow.

### 9.5 `/restore <checkpoint_id>`

`/restore` loads a checkpoint snapshot, creates a fresh session, initializes it, and then reconstructs the message history from the checkpoint.

This is the rollback/recovery workflow.

### 9.6 Related supporting commands

This same CLI change set also expands related interactive commands such as:

- `/help`
- `/clear`
- `/stats`
- `/approval`
- `/mcp`

Those are not all persistence-specific, but they contribute to the broader trend: the terminal is becoming more like an operator console and less like a thin one-shot prompt loop.

---

## 10. How restoration is actually performed in this repo

The restore path in `main.py` is worth tracing carefully because this is where the implementation details really matter.

### 10.1 Fresh session first, then replay snapshot

Both `/resume` and `/restore` follow the same broad pattern:

1. load a `SessionSnapshot`
2. create a fresh `Session(config=self.config)`
3. `await session.initialize()`
4. copy snapshot metadata onto the new session
5. replay saved messages into the new `ContextManager`
6. close the old session client + MCP manager
7. replace `self.agent.session` with the new session

This is a good architectural choice.

The repo is not trying to deserialize the full object graph of a live session directly.
Instead it:

- rebuilds the runtime managers normally,
- then restores the durable state into that fresh runtime shell.

That is much safer and easier to reason about.

### 10.2 Message replay logic

For each message in `snapshot.messages`:

- `system` messages are skipped
- `user` messages are re-added with `add_user_message(...)`
- `assistant` messages are re-added with `add_assistant_message(...)`
- `tool` messages are re-added with `add_tool_result(...)`

This is important.

The restore path does not trust raw message dicts as the final in-memory form.
It rehydrates them through the normal `ContextManager` APIs, which recompute token counts and maintain the expected internal message structure.

### 10.3 Why system messages are skipped

The saved message list comes from `ContextManager.get_messages()`, which includes the system prompt as the first message.

On restore, system messages are skipped because the fresh `ContextManager` already rebuilt the system prompt from the current config and runtime context.

That is a subtle but important design choice.

It means restoration preserves:

- conversation history,

while allowing the system prompt to be regenerated by the current runtime rather than frozen as a copied literal.

---

## 11. Example workflow: save and resume a session

A typical user flow might look like this conceptually:

```text
/start interactive session
... several turns of work happen ...
/save
/sessions
/resume <session_id>
```

Operationally, this means:

1. the current live session is snapshotted,
2. the snapshot is written to `sessions/<session_id>.json`,
3. later the user chooses that session id,
4. a new `Session` is initialized,
5. messages are replayed into the new context manager,
6. and the interactive agent continues using that reconstructed session.

This is the persistence story for continuity.

---

## 12. Example workflow: checkpoint before risky work, then restore

Another useful workflow is:

```text
/checkpoint
... try a risky path or exploratory refactor ...
/restore <checkpoint_id>
```

Conceptually, this means:

1. create a restore point,
2. let the live session continue evolving,
3. if the conversation goes in the wrong direction, restore from the earlier snapshot.

This is the persistence story for rollback.

---

## 13. Important implementation nuances and edge cases

This section is especially important because the feature is useful, but the current implementation still has several practical nuances.

### 13.1 Persistence only saves a snapshot of selected state, not the full live object graph

The repo persists:

- identity/timestamps
- turn count
- message history
- cumulative usage

It does **not** persist every runtime manager’s internal state, such as:

- loop detector history
- hook execution history
- approval transient state
- live MCP client connection objects
- cached latest usage details

Those are rebuilt fresh on resume/restore.

That is a reasonable and common design choice, but it is important to understand.

### 13.2 Restore creates a fresh runtime shell first

Because restore starts with `Session(config)` + `initialize()`, all managers are recreated from current runtime configuration.

That means restore is really:

- **state replay into a new session shell**

not:

- **binary resurrection of the old session object**.

### 13.3 System prompt is regenerated, not restored verbatim

Saved messages include the system prompt because `get_messages()` returns the full model-facing list.
But the restore logic intentionally skips saved `system` messages.

So the system prompt after restore is:

- whatever the current runtime regenerates from config and current tool surface,

not necessarily byte-for-byte identical to the original saved prompt.

This is an important nuance.

### 13.4 `turn_count` currently has a method/property mismatch in command code

`Session` exposes:

- `turn_count(self) -> int`

as a method, not a property.

But `main.py` currently snapshots and restores it using attribute-style access:

- `turn_count=self.agent.session.turn_count`
- `session.turn_count = snapshot.turn_count`

This means the current command code is treating `turn_count` like a plain integer field, while `Session` currently defines it as a method over `_turn_count`.

So the intended semantics are clear, but the current implementation has a mismatch at this boundary.

### 13.5 `get_stats()` returns `turn_count` using the same method-style symbol

`Session.get_stats()` currently includes:

- `"turn_count": self.turn_count`

which again refers to the method object rather than calling it.

So the statistics surface currently reflects the same turn-count API mismatch.

### 13.6 `total_usage` is restored, but `_latest_usage` is not

The restore path sets:

- `session.context_manager.total_usage = snapshot.total_usage`

But it does not restore the context manager’s `_latest_usage` field.

That means cumulative usage is preserved, while latest request usage is not explicitly replayed.

That is usually acceptable, but it can matter for behaviors that depend on latest usage rather than total usage.

### 13.7 Token counts are recomputed during replay

Because messages are replayed through `add_user_message(...)`, `add_assistant_message(...)`, and `add_tool_result(...)`, token counts are recalculated during restore.

That is good for consistency, but it means the snapshot does not preserve original internal token_count fields directly.

### 13.8 File permissions are security-aware

The `0o700` directory and `0o600` file permissions are a meaningful privacy choice.

This matters because persisted sessions can contain:

- user prompts
- code snippets
- command outputs
- fetched content
- possibly secrets or personal notes

So persistence is being introduced with at least a basic private-storage posture.

### 13.9 There is currently no implemented `/checkpoints` listing command even though help text mentions it

`TUI.show_help()` includes:

- `/checkpoints` - List available checkpoints

But `main.py` does not currently implement a `/checkpoints` command handler.

That means the help surface is slightly ahead of the implemented command surface in this area.

### 13.10 Restore/resume close the old live resources before swapping sessions

Both `/resume` and `/restore` call:

- `await self.agent.session.client.close()`
- `await self.agent.session.mcp_manager.shutdown()`

before replacing `self.agent.session`.

That is a good lifecycle hygiene detail.

It prevents the old live session’s client and MCP resources from hanging around after the swap.

---

## 14. Why this change matters in the series

This change matters because many earlier improvements made the runtime more capable *inside one live execution*.
This one makes the runtime more usable *across time*.

That is a major step in practical agent UX.

The project is moving from:

- a sophisticated in-memory agent console,

toward:

- a runtime where work can be paused, preserved, inspected, resumed, and rolled back.

That changes the feel of the system significantly.

It is no longer only a live chat loop.
It is beginning to behave more like a persistent working environment.

---

## 15. Delta summary table (`15` -> current uncommitted state)

| Area | `15` baseline | Current uncommitted delta (`16`) |
|---|---|---|
| Session durability | Session lives mostly in memory | Adds disk-backed session snapshot persistence |
| Checkpoint support | None | Adds timestamped checkpoint snapshots |
| Persistence schema | None | Adds `SessionSnapshot` with identity, timestamps, turns, messages, usage |
| Persistence manager | None | Adds `PersistenceManager` for save/load/list operations |
| Interactive commands | General runtime/operator commands | Adds `/save`, `/sessions`, `/resume`, `/checkpoint`, `/restore` |
| Restore behavior | No restore workflow | Rebuild fresh session shell, then replay saved messages |
| Stats surface | Basic runtime stats path evolving | Adds `Session.get_stats()` including session/runtime counts |
| Storage location | Data dir used for memory and runtime data | Adds `sessions/` and `checkpoints/` under agent data dir |
| Security posture | No persisted session files | Uses private directory/file permissions for saved state |
| Main implementation nuance | Live runtime only | Turn-count API mismatch and partial command/help mismatch remain |

---

## 16. Natural continuation points for a future `17`

Natural next steps after this iteration would be:

- fixing the `turn_count` method/property mismatch in snapshot/save/restore paths,
- adding `/checkpoints` listing support to match the help text,
- restoring more session-scoped state such as loop history or latest usage if desired,
- adding checkpoint metadata or names beyond timestamp-based ids,
- exposing save/restore events in the UI more richly,
- and adding pruning/retention policies for old session and checkpoint files.

That would continue the transition from:

- **basic persistence and restoration workflows**

into:

- **fully managed, inspectable, and policy-aware session lifecycle management**.

---

## 17. Key takeaways

1. The main delta since `docs/15-...` is the addition of persistent session snapshots and checkpoint-based restoration workflows.
2. `SessionSnapshot` defines the saved state contract, while `PersistenceManager` owns filesystem-backed save/load/list behavior.
3. The CLI now exposes persistence directly through `/save`, `/sessions`, `/resume`, `/checkpoint`, and `/restore`, making persistence part of the interactive operator workflow.
4. Restoration in this repo is implemented as fresh-session initialization plus message replay, not full object resurrection.
5. Saved sessions and checkpoints serve different purposes: one is the latest durable session state, the other is a historical restore point.
6. The implementation already includes thoughtful details like private filesystem permissions and skipping saved system messages so prompts are regenerated from current runtime config.
7. The feature is useful now, but still early: turn-count handling currently has an API mismatch, latest usage is not fully restored, and the help surface mentions `/checkpoints` before that command exists in `main.py`.

