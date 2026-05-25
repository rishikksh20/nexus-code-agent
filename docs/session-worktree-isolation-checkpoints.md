# Session Worktrees, Sub-Agent Isolation, Checkpoints, And Rewind

Last updated: 2026-05-23

This document describes how Nexus can add session-wise worktree isolation,
isolated sub-agent edit branches, user-approved merge of sub-agent changes,
checkpoint creation, and rewind. It is written for the live Nexus codebase and
intentionally does not depend on `reference_code/`.

The short version:

```text
canonical workspace
  -> Nexus session worktree
       -> isolated sub-agent worktrees
            -> merge proposals
                 -> user approval
                      -> checkpoint target
                      -> merge edits
                      -> optional rewind
```

The design must preserve current Nexus invariants:

- Approval UX stays centralized in `run_agent_turn()`.
- `Agent.run()` remains event-driven and resumes approved calls through
  `resume_tool_calls`.
- Provider-safe message ordering is preserved.
- Sub-agent tool, skill, and MCP scope continues to flow through
  `nexus/runtime/agent_scope.py`.
- Config fields are added in `nexus/config/defaults.py`, validation and merge
  behavior in `nexus/config/loader.py`, and schema upgrades in
  `nexus/config/upgrade.py`.
- Slash-command handlers live in `nexus/runtime/slash_commands.py`, and every
  new command group has a `help` subcommand.
- `.nexus/` remains runtime-managed state and is written only through storage
  APIs such as `SessionStore` or new checkpoint/worktree stores.

## External Tools Checked

The useful external patterns are:

| Project | What to borrow | What Nexus should own itself |
| --- | --- | --- |
| [`raine/workmux`](https://github.com/raine/workmux) | Single-command creation of git worktrees plus terminal multiplexer windows, pane layouts, file copy/symlink setup, lifecycle hooks, and merge cleanup. Workmux is a strong backend when the operator wants tmux, Kitty, WezTerm, or Zellij based agent terminals. | Checkpoint metadata, session history trimming, provider-safe approval resume, and Nexus-specific sub-agent scope rules. |
| [`coplane/par`](https://github.com/coplane/par) | Global labels for worktree sessions, `tmux` sessions, global `ls/open/rm/send`, and multi-repo workspace support under a global data directory. Par is a good backend when a user wants to coordinate many repos from anywhere. | Nexus session/checkpoint schema, approval flow, and per-agent resource visibility. |
| [`Priivacy-ai/spec-kitty`](https://github.com/Priivacy-ai/spec-kitty) | Spec-driven task lanes, work-package worktrees, review/accept/merge states, and an external orchestrator contract. It is most useful as a process model for "planned -> in progress -> review -> accepted -> merged". | Nexus should not adopt a full spec system just to get worktree isolation. The checkpoint/rewind feature should remain useful for ordinary REPL and headless sessions. |

Recommended approach: implement a small Nexus-native worktree/checkpoint core,
then add optional `workmux` and `par` adapters. The core keeps tests
deterministic and keeps rewind coupled to Nexus sessions. The adapters make
terminal orchestration pleasant for real users.

## Goals

1. Run each sub-agent that may edit files in its own branch and worktree.
2. Optionally run an entire Nexus user session in a worktree instead of the
   canonical workspace.
3. Allow parallel terminal sub-agents without file conflicts in the same working
   directory.
4. Return sub-agent changes as merge proposals, not silent writes into the
   parent workspace.
5. Require explicit user approval before merging isolated edits into the target
   workspace or target branch.
6. Create checkpoints before risky mutations, especially before merges and
   rewinds.
7. Support rewind of both filesystem state and session state while preserving an
   audit trail.
8. Keep parent and sub-agent conversation histories isolated. Share only focused
   packets, summaries, artifacts, and merge metadata.

## Non-Goals

- Do not replace the existing cognitive `subagent_*` tools with a scheduler.
  Advanced mode is still expressed by registered sub-agent tools.
- Do not add user-facing approval callbacks back to `Agent.run()`.
- Do not make `workmux` or `par` mandatory for CI or core tests.
- Do not turn old tool observations into live file bindings. File context stays
  append-only and freshness is refreshed by new reads, diffs, or test output.
- Do not auto-merge sub-agent edits just because they are in isolated
  worktrees.

## Operating Modes

Start with conservative modes so this can ship incrementally.

```toml
# .nexus/config.toml
worktree_isolation = "subagents"  # off | subagents | session
worktree_backend = "native"       # native | workmux | par
worktree_root = ""                # default: sibling <project>__nexus_worktrees
terminal_backend = "none"         # none | tmux | workmux | par
checkpoint_retention = 50
merge_strategy = "patch"          # patch | merge | squash | rebase
```

| Mode | Behavior |
| --- | --- |
| `off` | Existing Nexus behavior. Supervisor and in-process sub-agents use the active workspace. Manual checkpoints can still exist. |
| `subagents` | The supervisor uses the active workspace. Editing sub-agents get isolated worktrees. This should be the first production default. |
| `session` | The entire Nexus session runs in a session worktree. Edits reach the canonical workspace only through an approved merge proposal. |

## Directory Layout

Use a sibling worktree root by default. This avoids nesting git worktrees inside
the repository and keeps the main project tree easier to scan.

```text
<parent>/
  build-an-ai-agent/
  build-an-ai-agent__nexus_worktrees/
    sessions/
      sess-abc123/
    subagents/
      sess-abc123/
        call-001-planning-analysis/
        call-002-execution/
    integration/
      proposal-0007/
```

Runtime metadata remains under the existing Nexus state APIs:

```text
<repo>/.nexus/
  sessions/
    <session-id>.json
    latest_session.txt
  checkpoints/
    <checkpoint-id>.json
  merge-proposals/
    <proposal-id>.json
```

Implementation detail: do not write these files ad hoc from tools. Add stores:

- `CheckpointStore`
- `WorktreeSessionStore`
- `MergeProposalStore`

These stores should mirror `SessionStore` behavior: JSON payloads, atomic
temporary-file writes, and retention pruning.

## Core Data Model

Add metadata to `SessionSnapshot.metadata` instead of creating a separate
session system.

```json
{
  "worktree": {
    "mode": "subagents",
    "backend": "native",
    "session_worktree_path": "",
    "base_branch": "main",
    "base_sha": "abc123",
    "target_workspace": "/repo/path"
  },
  "checkpoints": {
    "latest": "ckpt-0009",
    "created": ["ckpt-0001", "ckpt-0009"]
  },
  "merge_proposals": {
    "pending": ["merge-0004"],
    "applied": ["merge-0001", "merge-0002"]
  }
}
```

### Worktree Record

```python
@dataclass(slots=True)
class WorktreeRecord:
    worktree_id: str
    session_id: str
    owner: str                 # supervisor | subagent_execution | terminal
    task_id: str
    path: Path
    branch: str
    base_branch: str
    base_sha: str
    head_sha: str
    backend: str               # native | workmux | par
    status: str                # active | completed | merged | abandoned | failed
    created_at: str
    updated_at: str
```

### Checkpoint Record

```python
@dataclass(slots=True)
class CheckpointRecord:
    checkpoint_id: str
    session_id: str
    label: str
    workspace_path: Path
    branch: str
    head_sha: str
    git_ref: str | None
    patch_path: Path | None
    file_snapshot_path: Path | None
    message_count: int
    metadata_snapshot: dict[str, Any]
    created_by: str            # manual | before_merge | before_rewind | before_tool
    created_at: str
```

For a clean git worktree, a checkpoint can be a lightweight ref:

```text
refs/nexus/checkpoints/<session-id>/<checkpoint-id>
```

For dirty tracked changes, store a binary patch. For untracked files, store file
snapshots in `CheckpointStore`. Rewind should restore all three pieces:

1. HEAD/ref state.
2. tracked dirty patch state.
3. untracked file snapshots.

### Merge Proposal Record

```python
@dataclass(slots=True)
class MergeProposal:
    proposal_id: str
    session_id: str
    source_worktree_id: str
    source_branch: str
    source_path: Path
    target_path: Path
    target_branch: str
    base_sha: str
    source_head_sha: str
    target_head_sha: str
    changed_files: list[str]
    diff_summary: str
    diff_path: Path
    tests: list[dict[str, str]]
    status: str                # pending | approved | applied | conflicted | rejected
    checkpoint_id: str | None
    created_at: str
```

The proposal is the unit the user approves. It should include enough
information for a confirmation preview without recomputing state in the
approval UI.

## Runtime Architecture

Add small runtime modules:

```text
nexus/runtime/worktrees.py
nexus/runtime/checkpoints.py
nexus/runtime/merge_proposals.py
nexus/runtime/terminal_subagents.py
```

### `worktrees.py`

Owns worktree lifecycle and backend adapters.

```python
class WorktreeBackend(Protocol):
    async def create(self, spec: WorktreeSpec) -> WorktreeRecord: ...
    async def open_terminal(self, record: WorktreeRecord, command: str) -> None: ...
    async def remove(self, record: WorktreeRecord, *, force: bool = False) -> None: ...
    async def status(self, record: WorktreeRecord) -> WorktreeStatus: ...
```

Backends:

- `NativeGitWorktreeBackend`: uses `git worktree add/remove/list`, creates
  branches, and is the default for tests.
- `WorkmuxBackend`: shells out to `workmux add`, `workmux open`, `workmux merge`
  or `workmux remove` when terminal orchestration is desired.
- `ParBackend`: shells out to `par start`, `par open`, `par send`, and `par rm`
  for globally labeled sessions and multi-repo workflows.

Use external CLIs only behind this interface. Do not let `workmux` or `par`
decide Nexus checkpoint semantics.

### `checkpoints.py`

Owns checkpoint creation, listing, preview, restore, and retention.

Important APIs:

```python
create_checkpoint(state, label, workspace_path, reason) -> CheckpointRecord
restore_checkpoint(state, checkpoint_id, *, create_safety_checkpoint=True) -> RewindResult
list_checkpoints(session_id) -> list[CheckpointRecord]
```

Restoring a checkpoint should never silently erase newer work. Before rewind,
create a safety checkpoint unless the user explicitly disables it.

### `merge_proposals.py`

Owns diff collection, conflict checks, approval payloads, and apply behavior.

Important APIs:

```python
build_merge_proposal(source_worktree, target_path) -> MergeProposal
preflight_merge(proposal) -> MergePreflight
apply_approved_merge(proposal, strategy) -> MergeApplyResult
```

The proposal must be stable between confirmation and resume. If the source
branch or target branch changes after the preview, invalidate the proposal and
ask for a fresh approval.

### `terminal_subagents.py`

Owns optional long-running terminal sub-agent sessions.

A terminal sub-agent is not an in-memory nested `Agent.run()` call. It is a
separate Nexus process in a separate worktree and terminal session:

```text
parent Nexus process
  -> creates worktree + prompt file
  -> starts terminal/backend session
  -> child Nexus process runs in isolated worktree
  -> child writes normal session metadata
  -> parent polls or receives completion
  -> parent builds merge proposal
```

Each child process still uses its own `run_agent_turn()` for local approvals.
The parent never passes a user-facing approval callback into `Agent.run()`.

## Supervisor And Sub-Agent Flow

### In-Process Sub-Agent Flow

Current `SubAgentTool` already creates a scoped registry and isolated local
conversation. Extend it only at the worktree boundary.

1. Supervisor calls `subagent_execution` with a bounded task.
2. `SubAgentTool.execute()` asks `WorktreeManager` for a worktree if the
   sub-agent has mutating tools.
3. Build `ToolExecutionContext` with:
   - `session_id = <parent>-subagent-<call-id>`
   - `working_directory = <subagent-worktree-path>`
   - `metadata.parent_session_id`
   - `metadata.worktree_id`
   - `metadata.context_scope = "isolated"`
4. Compute effective tools with `subagent_tool_names()` from
   `nexus/runtime/agent_scope.py`.
5. Run the inner agent loop.
6. Collect changed files from tool result metadata plus `git diff --name-only`.
7. Return a JSON envelope containing:
   - `status`
   - `agent`
   - `task_id`
   - `summary`
   - `modified_files`
   - `worktree_id`
   - `branch`
   - `head_sha`
   - `merge_proposal_id` if one was created

The parent sees the summary and proposal metadata, not the child history.

### Terminal Sub-Agent Flow

1. Supervisor or user starts a terminal sub-agent:

   ```text
   /sub-agent terminal start execution --task-file docs/task.md
   ```

2. Nexus creates a worktree and writes a prompt artifact through a runtime
   store.
3. Backend starts the terminal process.

   Native tmux:

   ```bash
   tmux new-session -d -s nexus-<session>-<agent> \
     "cd <worktree> && uv run nexus --session <child-session> --prompt-file <task-file>"
   ```

   Workmux:

   ```bash
   workmux add <branch> --prompt-file <task-file> --background
   ```

   Par:

   ```bash
   par start <label> --path <repo>
   par send <label> "uv run nexus --session <child-session> --prompt-file <task-file>"
   ```

4. Parent records the terminal run as a `WorktreeRecord`.
5. Parent can inspect:
   - child session file through `SessionStore`
   - git status in the child worktree
   - test results and logs
6. Completion creates or updates a `MergeProposal`.

Terminal sub-agents are useful for parallel long-running work, but they should
not be the only path. The in-process path is faster and easier to test.

## User-Approved Merge

Create a mutating tool or slash command that applies a proposal only after
confirmation:

```text
/worktree merge <proposal-id>
```

Optional model-callable tool:

```text
merge_worktree_edits
```

The tool is mutating and high-risk. It must expose a `ToolConfirmation` preview
with:

- source agent and task title
- source branch and head SHA
- target branch and target path
- changed file list
- diff summary and full diff path
- conflict preflight result
- tests already run
- checkpoint that will be created before apply

Do not apply anything in `get_confirmation()`. Preview only.

### Merge Apply Algorithm

1. Load the proposal.
2. Verify proposal is still current:
   - source branch head equals `source_head_sha`
   - target branch head equals `target_head_sha`, or re-run preflight
   - changed files still match the proposal diff
3. Check target dirty state.
4. If target has uncommitted changes overlapping proposal files, block and ask
   the user to resolve or checkpoint first.
5. Create a `before_merge` checkpoint in the target workspace.
6. Apply strategy:
   - `patch`: apply the proposal diff to the target working tree. Best for
     preserving an active user's branch and uncommitted non-overlapping edits.
   - `merge`: run `git merge --no-ff <source-branch>`.
   - `squash`: run `git merge --squash <source-branch>`.
   - `rebase`: rebase source onto target first, then merge.
7. Run configured validation commands.
8. Mark proposal `applied`, `conflicted`, or `failed`.
9. Save session metadata.
10. Optionally remove or keep the source worktree.

`run_agent_turn()` already handles the provider-safe approval loop: it commits
only safe prefixes, asks the user, records approval/refusal, and resumes exact
pending calls through `resume_tool_calls`. The merge tool should rely on that
behavior rather than inventing its own prompt path.

## Checkpoint Design

Checkpoints should be created in four places:

| Trigger | Required? | Reason |
| --- | --- | --- |
| Manual `/checkpoint create <label>` | yes | User wants an explicit save point. |
| Before approved merge | yes | User can rewind after a bad merge. |
| Before rewind | yes by default | Safety net for newer work. |
| Before high-risk mutating tool in session worktree | optional | Useful for destructive shell or broad edits. |

### Checkpoint Contents

A checkpoint captures both file state and conversation state.

File state:

- workspace path
- branch
- HEAD SHA
- optional git ref
- tracked binary diff
- untracked file snapshots
- dirty file list

Session state:

- `session_id`
- `message_count`
- `summary`
- `metadata` snapshot
- current turn and trace identifiers if applicable
- carry-over state if stored separately in future

This is what makes rewind valuable. A file-only undo leaves the model with
future context that no longer matches disk. A message-only rewind leaves files
dirty. Nexus needs both.

## Rewind Design

User-facing commands:

```text
/checkpoint list
/checkpoint show <checkpoint-id>
/rewind <checkpoint-id>
```

or under one group:

```text
/checkpoint rewind <checkpoint-id>
```

Recommended behavior:

1. Stop or pause active terminal sub-agents for the same session.
2. Create a safety checkpoint named `before-rewind-<timestamp>`.
3. Restore files from the target checkpoint.
4. Create a new child session snapshot instead of overwriting history:

   ```text
   old session: sess-abc123
   new session: sess-abc123-rw1
   parent_session_id: sess-abc123
   rewound_from_checkpoint: ckpt-0007
   ```

5. Copy messages up to `checkpoint.message_count`.
6. Restore metadata from `checkpoint.metadata_snapshot`, then append rewind
   audit metadata.
7. Update `latest_session.txt` through `SessionStore.save()`.
8. Print the safety checkpoint id and the active rewound session id.

This preserves auditability and provider-safe message ordering. Old session
files remain inspectable.

## Conflict Policy

Conflicts must be detected before applying changes to the user's target
workspace.

Block merge when:

- target has dirty changes to a file also changed by the proposal
- source branch is no longer at the proposal head SHA
- target branch moved and preflight now conflicts
- proposal includes generated or ignored files that are not in the manifest
- validation command is configured as required and fails

Allow merge with warning when:

- target has dirty changes in non-overlapping files
- source changed only docs or tests and validation was skipped by config
- worktree cleanup fails after the merge but apply succeeded

Always keep the worktree on conflict. It is the safest place to inspect and
repair the proposal.

## Slash Commands

Add these groups to `nexus/runtime/slash_commands.py`.

```text
/worktree help
/worktree list
/worktree show <id>
/worktree open <id>
/worktree diff <id>
/worktree proposal <id>
/worktree merge <proposal-id>
/worktree remove <id>
/worktree repair

/checkpoint help
/checkpoint create [label]
/checkpoint list
/checkpoint show <id>
/checkpoint rewind <id>

/rewind help
/rewind <checkpoint-id>

/sub-agent terminal help
/sub-agent terminal start <name> --task-file <path>
/sub-agent terminal list
/sub-agent terminal status <id>
/sub-agent terminal stop <id>
```

`/rewind` can be a thin alias for `/checkpoint rewind`, but if it exists as a
group it still needs help.

## Config Changes

Add fields to `AgentConfig`:

```python
worktree_isolation: str = "off"
worktree_backend: str = "native"
worktree_root: Path = field(default_factory=Path)
terminal_backend: str = "none"
checkpoint_retention: int = 50
checkpoint_before_merge: bool = True
checkpoint_before_rewind: bool = True
merge_strategy: str = "patch"
merge_remove_worktree: bool = False
merge_validation_commands: list[str] = field(default_factory=list)
```

Validation:

- `worktree_isolation` in `off`, `subagents`, `session`
- `worktree_backend` in `native`, `workmux`, `par`
- `terminal_backend` in `none`, `tmux`, `workmux`, `par`
- `merge_strategy` in `patch`, `merge`, `squash`, `rebase`
- `checkpoint_retention >= 1`
- external backend validation should be a runtime doctor/check, not import-time
  failure

Upgrade:

- Backfill defaults in `nexus/config/upgrade.py`.
- Do not migrate older delegation keys into these fields automatically. They
  represent a different feature.

## Tool Surface

Keep the default user-facing tool names clear and avoid legacy compatibility
names. Potential new tools:

| Tool | Mutating | Purpose |
| --- | --- | --- |
| `create_checkpoint` | yes | Create a checkpoint for current session/workspace. |
| `rewind_checkpoint` | yes | Restore files and create a rewound child session. |
| `merge_worktree_edits` | yes | Apply a pending merge proposal after user approval. |
| `inspect_worktree_diff` | no | Return diff/status for an isolated worktree. |

If these become model-callable tools, register them in the normal tool registry
only when the feature is enabled. Slash commands should remain available for
manual control.

## Hooks And Observability

Emit hook payloads for:

- worktree created
- terminal sub-agent started/stopped
- checkpoint created
- rewind started/completed/failed
- merge proposal created
- merge proposal approved/rejected/applied/conflicted

Audit records for merge and rewind should include:

- session id
- checkpoint id
- proposal id
- source and target paths
- changed files
- strategy
- approval scope
- result status

Metrics should count active worktrees, checkpoint count, merge conflicts, and
rewind operations.

## Test Plan

Use `uv run pytest`.

Core tests:

- `CheckpointStore` round-trips records and prunes retention.
- `create_checkpoint()` captures clean HEAD, dirty tracked diffs, and untracked
  files.
- `restore_checkpoint()` restores files and creates a child session.
- `SessionStore` still writes provider-safe message history after rewind.
- `WorktreeManager` creates and removes native git worktrees under `tmp_path`.
- `SubAgentTool` receives `ToolExecutionContext.working_directory` pointing to
  its isolated worktree when isolation is enabled.
- Sub-agent resource scopes still call `subagent_tool_names()`,
  `subagent_skill_names()`, and MCP helpers from `agent_scope.py`.
- `merge_worktree_edits` asks for confirmation and applies only through
  `run_agent_turn()` approval resume.
- Denied merge leaves target workspace unchanged.
- Approved merge creates a `before_merge` checkpoint.
- Conflict preflight blocks overlapping dirty target files.
- Provider-safe history ordering remains valid after merge confirmation.
- `Agent.run()` signature does not regain a user-facing approval callback.
- `workmux` and `par` adapters are tested with command construction fakes, not
  by requiring those CLIs in CI.

End-to-end tests:

1. Start a parent session.
2. Spawn an execution sub-agent in a worktree.
3. Sub-agent edits a file.
4. Parent receives a merge proposal.
5. User denies merge.
6. Assert target file unchanged.
7. User approves merge.
8. Assert checkpoint exists and file changed.
9. Rewind to checkpoint.
10. Assert file and session message count return to checkpoint state.

## Rollout Plan

### Phase 1: Native Checkpoints

- Add `CheckpointStore`.
- Add `/checkpoint create/list/show/rewind`.
- Support current workspace only.
- No workmux/par integration yet.

### Phase 2: Sub-Agent Worktree Isolation

- Add native git `WorktreeManager`.
- Extend `SubAgentTool` to allocate worktrees for mutating sub-agents.
- Return worktree metadata and changed files in the sub-agent result envelope.
- Add merge proposals and user-approved merge.

### Phase 3: Terminal Sub-Agents

- Add terminal sub-agent runtime.
- Add `tmux` native backend.
- Add optional `workmux` and `par` adapters.
- Add `/sub-agent terminal ...` commands.

### Phase 4: Session Worktree Mode

- Add `worktree_isolation = "session"`.
- Make `RuntimeSession.create()` select or create a session worktree.
- Set `ReplState.config.workspace_root` or `ToolExecutionContext.working_directory`
  to the session worktree.
- Add final session merge proposal on demand.

### Phase 5: Polish And Hardening

- Add conflict repair helpers.
- Add dashboard/status output.
- Add stale worktree repair.
- Add retention cleanup.
- Add docs for user workflows and backend setup.

## Example User Workflow

Sub-agent isolation:

```text
> /mode advanced
> Implement the Cohere retry fix. Use sub-agents if helpful.

Nexus delegates implementation to subagent_execution.
subagent_execution edits files in:
  ../build-an-ai-agent__nexus_worktrees/subagents/sess-123/call-456-execution

Nexus returns:
  Merge proposal merge-0004
  Changed files:
    nexus/integrations/cohere.py
    tests/test_openai_compatible.py

> /worktree diff merge-0004
> /worktree merge merge-0004

Nexus asks for approval with diff and checkpoint preview.
User approves.
Nexus creates checkpoint ckpt-0012 and applies the merge.
```

Rewind:

```text
> /checkpoint list
ckpt-0012 before_merge 2026-05-23T...

> /checkpoint rewind ckpt-0012

Nexus creates safety checkpoint ckpt-0013.
Nexus restores files and opens child session sess-123-rw1.
```

Terminal sub-agents:

```text
> /sub-agent terminal start execution --task-file docs/tasks/retry-fix.md
> /sub-agent terminal list
> /worktree proposal <terminal-worktree-id>
> /worktree merge merge-0008
```

## Design Notes

### Why Not Just Use Git Stash?

`git stash` is useful but not enough. Nexus rewind needs to couple file state
with session message state. A stash can restore files, but it cannot trim or
fork the conversation history safely.

### Why Not Let Workmux Merge Directly?

Workmux merge is convenient for human terminal workflows. Nexus still needs its
own merge proposal because the approval preview, checkpoint id, session
metadata, and provider-safe resumed tool result all belong to Nexus.

### Why Not Make Every Sub-Agent A Terminal Process?

Terminal processes are excellent for long tasks and parallel monitoring, but
they are slower and harder to test. In-process sub-agents should remain the
default for bounded tasks. Terminal sub-agents should be opt-in for work that
benefits from separate panes, dev servers, or long-running observation.

### Why Child Sessions On Rewind?

Overwriting the old session would hide what happened. A child session keeps the
rewound experience clean while preserving the original transcript for audit,
debugging, and learning.

## Open Questions

- Should `worktree_isolation = "subagents"` become the default once stable?
- Should low-risk mutating tools auto-confirm inside isolated sub-agent
  worktrees, or should they keep the same approval policy as the parent?
- Should merge proposals support partial file or hunk approval in the first
  release?
- Should terminal sub-agents be allowed to run with live user approval in their
  own pane, or should they be restricted to non-interactive/headless operation?
- Should checkpoint restore be allowed when the target branch no longer exists?

## References

- Workmux repository and README: https://github.com/raine/workmux
- Par repository and README: https://github.com/coplane/par
- Spec Kitty repository and README: https://github.com/Priivacy-ai/spec-kitty
- Spec Kitty worktree explanation: https://github.com/Priivacy-ai/spec-kitty/blob/main/docs/explanation/git-worktrees.md
- Spec Kitty CLI reference: https://github.com/Priivacy-ai/spec-kitty/blob/main/docs/reference/cli-commands.md
