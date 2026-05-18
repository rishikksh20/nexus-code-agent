# Conflict And File Freshness Management

This document explains how Mistral Vibe handles changing files during an
agentic coding session. It focuses on the tricky case where the main agent has
old file content in context, but a later tool call, subagent, or outside process
changes the file on disk.

## Core Rule

Vibe treats tool observations as historical facts, not live bindings.

If the main agent reads a file, the resulting `read_file` tool message means:

```text
At this point in the session, this was the content returned by read_file.
```

It does not mean:

```text
This file still currently has this content.
```

When the file changes later, Vibe does not rewrite the old `read_file` message
in the conversation. The active context remains append-only. Freshness is
managed by adding newer observations, usually by rereading the file or checking
`git diff`.

## Why Old Context Is Not Mutated

Vibe's message history is both working memory and an audit trail. Automatically
editing old messages would make the transcript misleading.

For example:

```text
1. read_file("app.py") returns version A
2. search_replace("app.py") changes it to version B
3. old read_file result is still in context as version A
4. new edit result is appended after it
```

The model should understand that newer observations supersede older ones. The
old observation is still useful because it explains why the agent made a
decision at that time.

Rewriting old messages would create several problems:

- The transcript would no longer show what the model actually saw.
- Tool-call protocol expects assistant tool calls and matching tool responses to
  remain stable.
- Session replay would become confusing.
- Session logs would stop being a reliable audit trail.
- Compaction would summarize a rewritten history rather than the actual one.
- Concurrent tool calls could make automatic ordering ambiguous.

So Vibe uses an append-only context model:

```text
old read result
new write/edit result
new reread or diff result
```

## Source Of Truth

The filesystem is the source of truth for current file state.

The model context contains observations about the filesystem. Some observations
may be stale. When freshness matters, the agent must query the filesystem again.

Current-state tools include:

- `read_file`: reads the current file contents at call time.
- `grep`: searches the current filesystem at call time.
- `bash("git diff")`: shows current tracked and untracked file mutations.
- `bash("git status")`: shows current repository state.
- `bash("uv run pytest ...")`: verifies current code behavior.
- `bash("uv run pyright")`: type-checks current code.
- `bash("uv run ruff check --fix .")`: lints current code and may mutate files.

## Main Agent File Flow

A typical safe coding flow for the main agent is:

```text
read_file("target.py")
  -> model reasons over current snapshot
  -> search_replace("target.py", ...)
  -> tool returns edit outcome
  -> read_file("target.py") or bash("git diff -- target.py")
  -> model reasons over refreshed state
  -> run tests/lint/typecheck
```

The important part is the explicit refresh after mutation.

Vibe does not automatically do:

```text
old read_file("target.py") message gets replaced with new content
```

Instead, the correct pattern is:

```text
new read_file("target.py") message is appended
```

That gives the model a chronological record:

```text
read_file: old content
search_replace: applied 1 block
read_file: new content
```

## Tool Result Behavior

After any tool call, `AgentLoop` converts the final tool result model into text
and appends it as a `role="tool"` message.

For file tools:

- `read_file` returns the file content read at that moment.
- `write_file` returns the path, byte count, whether the file existed, and the
  written content.
- `search_replace` returns the file path, number of applied blocks, line delta,
  original edit block content, and warnings.
- `bash` returns stdout, stderr, return code, and command.

These results help the model infer file changes, but they are not a global file
state index. Vibe does not maintain a live table like:

```text
app.py -> latest known content
config.py -> latest known content
```

The conversation itself is the memory.

## Subagent File Flow

Subagents are context-isolated nested `AgentLoop`s. The parent does not receive
the subagent's full message history.

When the main agent calls:

```text
task(task="Inspect and update parser tests", agent="custom-subagent")
```

the `task` tool creates a fresh subagent loop. The child sees its own task
prompt and its own tool results. The parent sees:

- progress summaries from subagent tool results
- final `TaskResult.response`
- `TaskResult.completed`
- `TaskResult.turns_used`

The parent does not automatically receive every subagent `read_file`,
`search_replace`, or `bash` observation as parent context.

Therefore, if a subagent may have changed files, the parent should refresh
affected files before making dependent edits:

```text
task(...) returns "Updated parser.py and tests/test_parser.py"
  -> main agent runs bash("git diff -- parser.py tests/test_parser.py")
  -> or read_file("parser.py") and read_file("tests/test_parser.py")
  -> main agent continues from refreshed state
```

For the builtin `explore` subagent this is less risky because it is read-only:

```text
enabled_tools = ["grep", "read_file"]
```

But custom subagents can be configured with editing tools, so the parent should
not assume previously read file content is still current after a delegated task.

## Scratchpad Sharing

The scratchpad is a shared temporary workspace. The main agent gets a
session-scoped scratchpad path, and the `task` tool injects that path into
subagent prompts.

This means a main agent and subagent can coordinate through scratchpad files:

```text
subagent writes scratchpad/analysis.md
main agent reads scratchpad/analysis.md
```

Scratchpad files are auto-allowed for file tools, but they follow the same
freshness rule. If a scratchpad file changes, old reads remain old reads. The
agent must reread the scratchpad file to observe its latest contents.

## External Mutations

Files can also change outside Vibe:

- the user edits a file in an IDE
- a formatter modifies files
- a test generator writes snapshots
- `ruff --fix` changes files
- a build step generates files
- another process updates the repository

Vibe does not watch every file and update context automatically. To detect
external mutations, the agent should use current-state checks:

```bash
git status --short
git diff
git diff -- path/to/file.py
```

or reread specific files:

```text
read_file(path="path/to/file.py")
```

## Concurrent Tool Calls

`AgentLoop` can execute multiple resolved tool calls concurrently. This is
useful for independent reads or searches, but it is dangerous for dependent
file mutations.

Safe concurrent work:

```text
grep("class User")
grep("def authenticate")
read_file("README.md")
```

Risky concurrent work:

```text
search_replace("app.py", block A)
search_replace("app.py", block B)
write_file("app.py", full replacement)
```

There is no repo-wide file lock or per-file serialization layer in the agent
loop. Dependent file mutations should be sequenced:

```text
read
  -> edit
  -> observe result
  -> reread/diff
  -> next edit
```

## Search/Replace As Conflict Detection

`search_replace` helps with stale context because it applies explicit
SEARCH/REPLACE blocks against the current file. If the expected search text no
longer matches, the tool can fail or warn instead of silently editing the wrong
location.

This is a practical conflict-detection mechanism:

```text
old context says function has body A
file now has body B
search_replace tries to replace body A
  -> exact match fails
  -> fuzzy matching may warn or fail
  -> agent must reread file and regenerate patch
```

So the usual recovery path is:

```text
search_replace failed
  -> read_file current file
  -> rebuild SEARCH/REPLACE block
  -> apply again
```

## Rewind And File Snapshots

Editing tools can provide pre-write snapshots through `get_file_snapshot()`.
The main loop records those snapshots before running the tool. `RewindManager`
can later restore files when rewinding a conversation.

This is different from freshness tracking:

- Freshness tracking asks: "What is the file now?"
- Rewind asks: "Can I restore this file to an earlier checkpoint?"

Snapshots help undo changes, but they do not update old `read_file` messages
when files change.

## Session Logs

Session logs preserve the detailed historical record:

```text
messages.jsonl
meta.json
```

If the agent read an old file, then edited it, then reread it, all those
observations are saved as separate messages. The log remains chronological.

Subagent logs are stored separately under:

```text
<parent-session-dir>/agents/
```

The parent session log contains the `task` result. The subagent session log
contains the child's detailed reads, edits, and tool results.

## Compaction

Compaction summarizes old conversation context. It does not preserve every file
observation verbatim in active context.

Before compaction, the detailed session is saved. After compaction, the active
message list becomes:

```text
system message
summary message
```

If exact file contents matter after compaction, the agent should reread the
file or inspect the diff. The compacted summary might say:

```text
Updated parser.py to handle quoted strings.
```

but that is not a substitute for current file content.

## Recommended Agent Policy

For reliable coding, the main agent should follow these rules:

1. Treat previous `read_file` output as a snapshot, not a live cache.
2. After any edit tool changes a file, reread the file or inspect `git diff`
   before making dependent edits.
3. After a subagent reports file changes, inspect the affected files or `git
   diff` in the parent context.
4. After formatters, tests, build tools, or `ruff --fix` run, check `git diff`
   before finalizing.
5. Use `search_replace` for targeted edits so stale context causes a failed or
   warned patch rather than an uncontrolled overwrite.
6. Avoid concurrent writes to the same file.
7. Use session logs for audit, not as the live source of current file state.
8. Use the filesystem as the source of truth.

## Example: Main Agent Edit

```text
User: Update parser to support escaped quotes.

main agent:
  read_file("parser.py")
  read_file("tests/test_parser.py")
  search_replace("parser.py", escaped quote logic)
  search_replace("tests/test_parser.py", new tests)
  bash("uv run pytest tests/test_parser.py")

pytest fails because formatter changed line layout

main agent:
  read_file("parser.py")
  bash("git diff -- parser.py tests/test_parser.py")
  search_replace("parser.py", corrected patch)
  bash("uv run pytest tests/test_parser.py")
```

The second `read_file` and `git diff` are what refresh the model's context.

## Example: Subagent Change

```text
main agent reads "router.py" version A
main agent delegates:
  task("Add route discovery and tests", agent="implementation-subagent")

subagent edits "router.py" to version B
subagent returns:
  "Updated router.py and tests/test_router.py"

main agent must not rely on version A
main agent refreshes:
  bash("git diff -- router.py tests/test_router.py")
  read_file("router.py")
```

Only after that refresh should the main agent make further changes to
`router.py`.

## Mental Model

Think of Vibe context as a notebook:

```text
Page 1: I read app.py and saw version A.
Page 2: I patched app.py.
Page 3: I read app.py again and saw version B.
```

Vibe does not erase page 1. It appends page 3.

The agent must use the newest relevant observation, and when in doubt, reread
the file.

