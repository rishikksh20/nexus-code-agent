**Short Answer**
Vibe does **not** automatically rewrite or invalidate old `read_file` context when a file changes later. The model context is an append-only observation log. If the main agent read old file content, and then a tool, subagent, or external process changes that file, the old content remains in the main agent’s message history until the agent explicitly reads the file again or observes a newer tool result.

So the source of truth is always the filesystem, not the old LLM context.

**What Happens Today**
When `read_file` runs, it returns a `ReadFileResult` containing the file path and the file content at that moment: [read_file.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/tools/builtins/read_file.py:48). The agent loop then converts that result into a `role="tool"` message and appends it to `MessageList`: [agent_loop.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/agent_loop.py:1043).

That message is historical. It means:

```text
At time T, read_file returned this content.
```

It does not mean:

```text
This file still currently has this content.
```

If `write_file` later overwrites the file, its result is appended as a newer tool message: [write_file.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/tools/builtins/write_file.py:34). If `search_replace` patches the file, its patch result is appended too: [search_replace.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/tools/builtins/search_replace.py:62). But the old `read_file` message is not edited or removed.

**Subagent Case**
For the builtin `explore` subagent, this problem is limited because `explore` is read-only. It only has `grep` and `read_file`.

For a custom subagent that can edit files, the parent still does not get the child’s full message history. The `task` tool creates a separate nested `AgentLoop`: [task.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/tools/builtins/task.py:132). The parent receives only progress summaries and the final `TaskResult.response`, not every file read/edit observation from the subagent: [task.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/tools/builtins/task.py:155).

So if a subagent mutates `foo.py`, the main agent will know only if:

- the subagent reports it in its final response
- a streamed progress message made it visible to the UI
- the main agent later checks the file, git diff, or test output
- session logs are inspected separately

The parent’s old `read_file(foo.py)` content remains stale until refreshed.

**Why Vibe Does Not Auto-Update Old Context**
Automatically updating old context is genuinely complicated here because the message list is both working memory and audit trail. Mutating old messages would cause problems:

- The transcript would no longer represent what the model actually saw at that time.
- Tool-call protocol expects assistant tool calls and matching tool responses to remain stable.
- Session replay would become confusing because old observations would silently change.
- Compaction and resume rely on persisted message history.
- Multiple tools can run concurrently, so ordering can be nontrivial: [agent_loop.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/agent_loop.py:1108).

Instead, Vibe uses an append-only event/observation model. Newer observations supersede older ones semantically, but the old ones stay in history.

**How Agentic Coding Manages This**
The practical pattern is: reread before relying on file content.

A careful Vibe agent should do this:

```text
read file
  -> decide edit
  -> apply edit
  -> observe edit result
  -> reread changed file or inspect git diff
  -> run tests/typecheck/lint
  -> continue from current filesystem state
```

For subagents:

```text
main agent delegates task
  -> subagent may inspect or modify files
  -> subagent returns summary
  -> main agent rereads affected files or runs git diff
  -> main agent continues from current state
```

The repo’s tools support that style:

- `read_file` reads current filesystem content at call time.
- `grep` searches current filesystem content.
- `bash("git diff")` shows current mutations.
- `search_replace` reads the current file before applying blocks, so stale search text can fail instead of blindly overwriting.
- `write_file` and `search_replace` create file snapshots for rewind before mutation: [agent_loop.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/agent_loop.py:1012).

**What Rewind Does And Does Not Do**
`RewindManager` tracks file snapshots for files modified by tools. It can restore files when rewinding: [rewind/manager.py](/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent/workspace/mistral-vibe/vibe/core/rewind/manager.py:57).

But rewind is not a live context invalidation system. It helps undo file changes and message history. It does not continuously update old `read_file` messages when files change.

**Compaction**
Compaction also does not preserve a live file-state model. It summarizes the conversation and replaces active context with:

```text
system message
summary message
```

Detailed historical tool messages remain in session logs, but active context becomes a summary. If exact file state matters after compaction, the agent should reread files or inspect diffs.

**Important Edge Case: Concurrent Tool Calls**
The agent loop can execute multiple tool calls concurrently. That is good for independent reads/searches, but risky for dependent file mutations. If the model emits two writes to the same file in one assistant message, there is no per-file serialization layer shown here. The safer coding pattern is to sequence dependent file operations across turns:

```text
read -> edit -> observe -> reread/diff -> next edit
```

**Best Rule For This Repo**
Treat tool observations as timestamped facts, not live bindings.

A good operating rule for Vibe would be:

> After any tool or subagent may have changed files, the main agent should refresh affected file context with `read_file`, `grep`, or `git diff` before making further dependent edits.

That is the correct mental model for this implementation: append-only context, live filesystem source of truth, explicit refresh when freshness matters.
