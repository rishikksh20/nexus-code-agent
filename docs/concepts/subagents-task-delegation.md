# Subagents And Task Delegation

This document explains how subagents work in Mistral Vibe, based on the Python
implementation in `vibe/core/agents` and `vibe/core/tools/builtins/task.py`.

## Concept

A subagent is an agent profile that cannot be selected as the primary CLI agent.
It is used only through the `task` tool. The main agent delegates a bounded
piece of work to the subagent, the subagent runs its own in-memory agent loop,
and the final subagent response comes back to the main agent as a normal tool
result.

This gives Vibe a way to offload exploration or background investigation
without copying every low-level observation into the main conversation context.

```text
main agent
  -> calls task(task="inspect auth flow", agent="explore")
  -> task tool creates a fresh subagent AgentLoop
  -> subagent runs its own LLM/tool loop
  -> task tool streams coarse progress to the main UI
  -> task tool returns TaskResult(response=..., turns_used=..., completed=...)
  -> main agent continues with the summarized result
```

## Agent Types

Agent profiles are represented by `AgentProfile` in
`vibe/core/agents/models.py`. Each profile has an `agent_type`:

- `agent`: selectable as the primary session agent.
- `subagent`: usable only by the `task` tool.

The builtin subagent is `explore`:

```python
EXPLORE = AgentProfile(
    name=BuiltinAgentName.EXPLORE,
    display_name="Explore",
    description="Read-only subagent for codebase exploration",
    safety=AgentSafety.SAFE,
    agent_type=AgentType.SUBAGENT,
    overrides={"enabled_tools": ["grep", "read_file"], "system_prompt_id": "explore"},
)
```

That profile is deliberately small: it can search and read, but it cannot edit
files or run arbitrary commands unless a custom profile changes that behavior.

Primary session creation rejects subagent profiles unless the `AgentManager` is
constructed with `allow_subagent=True`. The main CLI path does not allow this.
The `task` tool creates its nested `AgentLoop` with `is_subagent=True`, which
allows the requested profile to be a subagent.

## Discovery

`AgentManager` discovers agent profiles from:

- builtin profiles in `vibe/core/agents/models.py`
- configured `agent_paths`
- project agent directories from the harness file manager
- user agent directories from the harness file manager

Custom agents are TOML files. A custom subagent is created by setting:

```toml
agent_type = "subagent"
display_name = "Research"
description = "Read-only research assistant"
safety = "safe"
enabled_tools = ["grep", "read_file", "web_search"]
```

Agents can be filtered by `enabled_agents`, `disabled_agents`, and
`installed_agents`. The `lean` builtin is an example of an install-required
agent, while `explore` is available by default as a subagent.

## The Task Tool Contract

The `task` tool is the bridge between the main agent and a subagent.

Arguments:

```python
class TaskArgs(BaseModel):
    task: str
    agent: str = "explore"
```

Result:

```python
class TaskResult(BaseModel):
    response: str
    turns_used: int
    completed: bool
```

Configuration:

```python
class TaskToolConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    allowlist: list[str] = Field(default=[BuiltinAgentName.EXPLORE])
```

So by default, delegating to `explore` is allowlisted, while other subagents can
fall back to the normal approval flow depending on config.

## Permission Flow

`Task.resolve_permission()` checks the requested subagent name before execution:

1. If it matches the task tool denylist, return `ToolPermission.NEVER`.
2. If it matches the task tool allowlist, return `ToolPermission.ALWAYS`.
3. Otherwise return `None`, so the main agent loop uses the task tool's default
   permission from config.

The default task permission is `ask`, but the builtin `explore` subagent is in
the task allowlist. Other custom subagents can be allowed or denied with normal
tool config:

```toml
[tools.task]
permission = "ask"
allowlist = ["explore", "research-*"]
denylist = ["dangerous-*"]
```

Once the subagent starts, its own tool calls still go through the normal
permission system. The parent approval callback is passed into the nested
`AgentLoop`, so subagent tools can still ask the same UI or ACP client for
permission.

## Execution Flow

The `Task.run()` flow is:

1. Require an `InvokeContext` with `agent_manager`.
2. Resolve the requested agent profile.
3. Reject it unless `agent_profile.agent_type == AgentType.SUBAGENT`.
4. Build a subagent-specific `SessionLoggingConfig`.
5. Load a base config with that session logging override.
6. Create a nested `AgentLoop` with:
   - `agent_name=args.agent`
   - `entrypoint_metadata=ctx.entrypoint_metadata`
   - `is_subagent=True`
   - `defer_heavy_init=True`
7. Copy the approval callback into the subagent loop if one exists.
8. Prefix the delegated task with scratchpad instructions if the parent has a
   scratchpad.
9. Run `subagent_loop.act(task_text)`.
10. Accumulate assistant text into the final `TaskResult.response`.
11. Convert subagent tool results into parent-visible `ToolStreamEvent`s.
12. Return `TaskResult`.

The nested loop is a real `AgentLoop`, so it has its own messages, stats, tool
manager, skill manager, middleware, backend, and session logger.

## Context Boundaries

The subagent does not receive the parent conversation history. It receives a new
conversation:

```text
subagent system prompt
subagent user message = delegated task text
```

The main agent shares only operational context:

- the selected subagent name
- the task string
- approval callback
- entrypoint metadata
- optional scratchpad path
- optional parent session directory for subagent logs

The parent receives only:

- progress messages derived from subagent tool results
- the final accumulated assistant response
- whether the subagent completed normally
- the number of assistant turns used

This design avoids context explosion. The subagent can read many files or search
many paths, but the parent context only gets the useful summary returned by the
task tool.

## Scratchpad Sharing

The main `AgentLoop` creates a session-scoped scratchpad directory. Subagents do
not create a separate scratchpad. Instead, the task tool injects this prefix into
the subagent task:

```text
Scratchpad directory: <path>
You can read and write files here without permission prompts.

<original task>
```

That gives the subagent a shared working area for temporary artifacts,
intermediate notes, generated scripts, or summaries. File tools auto-allow paths
inside active scratchpads, but project file reads and writes still follow normal
permission rules.

## Session Logging

When the parent session has a `session_dir`, the task tool creates subagent logs
under:

```text
<parent-session-dir>/agents
```

The subagent's session prefix is the subagent name, so an `explore` task creates
session folders similar to:

```text
<parent-session-dir>/agents/explore_<timestamp>_<short-session-id>/
  messages.jsonl
  meta.json
```

Those logs are separate from the parent session. The parent receives a compact
`TaskResult`, while the detailed subagent trace remains available for debugging
or replay.

## UI And ACP Behavior

The core loop emits `ToolCallEvent`, `ToolStreamEvent`, and `ToolResultEvent`.
The `task` tool uses `ToolUIData` adapters to summarize subagent tool results as
progress messages. In the CLI this appears as subagent progress. In ACP, those
core events are translated into ACP tool call updates and progress updates.

The main agent does not see all subagent events as conversation messages. It
sees the final task result text that `AgentLoop._handle_tool_response()` appends
as the tool observation for the original `task` call.

## Error And Completion Semantics

`TaskResult.completed` is `False` when:

- the subagent assistant event was stopped by middleware
- a subagent tool result was skipped
- an exception occurred during subagent execution

If an exception occurs, the error text is appended to the accumulated response:

```text
[Subagent error: ...]
```

The result still returns to the main agent as a tool result, allowing the main
agent to recover, explain the issue, or choose another path.

## Security Properties

The implementation enforces several boundaries:

- Primary sessions cannot select subagent profiles directly.
- The `task` tool rejects primary agents and only accepts `AgentType.SUBAGENT`.
- The builtin `explore` subagent is read-only through `enabled_tools`.
- Subagent tool calls still pass through the approval system.
- Subagent logs are separated from the parent session logs.
- Subagent context does not automatically merge back into the parent context.

This is not process isolation. The subagent is an in-memory nested agent loop in
the same Python process. Its safety comes from tool selection, permission
checks, agent profile config overlays, and context boundaries.

## Files To Read

- `vibe/core/tools/builtins/task.py`: task tool implementation.
- `vibe/core/agents/models.py`: builtin agent and subagent definitions.
- `vibe/core/agents/manager.py`: discovery, filtering, and profile switching.
- `vibe/core/agent_loop.py`: nested loop behavior and tool execution.
- `vibe/core/system_prompt.py`: available subagents section in the system prompt.
- `vibe/core/scratchpad.py`: shared scratchpad path checks.
