# Mistral Vibe AI Agent Concepts

This document explains how Mistral Vibe implements an AI coding agent from the
Python codebase. It focuses on the execution flow, agentic loop, middleware,
tools, subagents, permissions, and sandbox-like boundaries used by the CLI and
ACP entrypoints.

For deeper treatment of two large topics, see
[Subagents And Task Delegation](subagents-task-delegation.md) and
[Context Management](context-management.md).

## High-Level Runtime Map

Mistral Vibe has one core runtime and multiple front doors:

- `vibe/core`: agent runtime, tools, LLM backends, config, sessions, skills,
  agents, permissions, hooks, telemetry, rewind, and compaction.
- `vibe/cli`: interactive Textual TUI and programmatic CLI entrypoint.
- `vibe/acp`: Agent Client Protocol adapter for editor and client integrations.
- `vibe/setup`: onboarding, browser auth, and trusted-folder setup.
- `tests`: behavior coverage for the agent loop, backends, tools, ACP, CLI,
  config, sessions, skills, and e2e UI flows.

The central object is `vibe.core.agent_loop.AgentLoop`. A session creates one
`AgentLoop`, and that loop owns:

- the message history (`MessageList`)
- the active agent profile (`AgentManager`)
- available tools (`ToolManager`)
- available skills (`SkillManager`)
- LLM backend selection
- middleware pipeline
- permission callbacks
- session logging
- telemetry
- rewind checkpoints
- optional scratchpad directory

Entry points wrap the same core loop:

- Interactive CLI: `vibe.cli.entrypoint` parses args and calls
  `vibe.cli.cli.run_cli`, which creates `AgentLoop(..., enable_streaming=True)`
  and passes it to the Textual app.
- Programmatic CLI: `vibe.core.programmatic.run_programmatic` creates an
  `AgentLoop`, runs a single prompt, formats output, and exits.
- ACP: `vibe.acp.acp_agent_loop.VibeAcpAgentLoop` creates one `AgentLoop` per
  protocol session and translates core events into ACP session updates.

## Core Agentic Loop

At a concept level, one prompt flows like this:

```text
user prompt
  -> AgentLoop.act()
  -> append user message
  -> run middleware before turn
  -> call LLM with current messages and available tool schemas
  -> stream or append assistant message
  -> parse assistant tool calls
  -> validate tool args with each tool's Pydantic args model
  -> decide permissions
  -> execute approved tools concurrently
  -> append tool result messages
  -> repeat LLM turn while last message is a tool message
  -> stop when assistant returns a normal final response
```

Important implementation points:

- `AgentLoop.act()` creates a rewind checkpoint, starts a tracing span, and
  delegates to `_conversation_loop`.
- `_conversation_loop()` appends the user message, yields `UserMessageEvent`,
  runs middleware before every model turn, calls `_perform_llm_turn()`, saves
  the session repeatedly, and loops while the latest message is a tool result.
- `_perform_llm_turn()` calls the backend in streaming or non-streaming mode,
  parses tool calls via `APIToolFormatHandler`, and executes resolved tools.
- `_run_tools_concurrently()` starts one asyncio task per resolved tool call and
  yields events through a queue as tools finish.
- Tool outputs are converted back into `role="tool"` LLM messages so the model
  can continue reasoning with observed results.

The loop is "agentic" because the model is not just producing text. It observes
state, chooses tools, receives tool results, and keeps iterating until it has
enough information or the middleware stops it.

## Messages, Events, And UI Translation

The internal conversation uses `LLMMessage` objects from `vibe.core.types`:

- `system`: the composed system prompt
- `user`: human prompts and injected runtime warnings
- `assistant`: model responses, reasoning content, and tool calls
- `tool`: tool observations returned to the model

The loop yields event models instead of printing directly:

- `UserMessageEvent`
- `AssistantEvent`
- `ReasoningEvent`
- `ToolCallEvent`
- `ToolResultEvent`
- `ToolStreamEvent`
- `CompactStartEvent` and `CompactEndEvent`
- `AgentProfileChangedEvent`

This separation lets the Textual UI, programmatic formatter, and ACP adapter
render the same core behavior differently. ACP translates core events into
`agent_message_chunk`, `agent_thought_chunk`, `tool_call_update`, usage updates,
and replay events.

## System Prompt Composition

`vibe.core.system_prompt.get_universal_system_prompt()` builds the system prompt
from several sections:

- selected base prompt from config (`system_prompt_id`)
- headless-mode instruction when applicable
- commit signature instructions
- active model name
- operating system and shell details
- per-tool prompt files from builtin or custom tools
- available skills
- available subagents
- scratchpad directory instructions
- project context and git status
- user-level and project-level `AGENTS.md` instructions

The system prompt is rebuilt when remote tools finish deferred discovery, when
the agent profile changes, or when config is reloaded.

## Middleware Pipeline

Middleware runs before each model turn. The interface is
`ConversationMiddleware.before_turn(context) -> MiddlewareResult`.

The pipeline supports four actions:

- `CONTINUE`: proceed to the LLM call.
- `STOP`: emit an assistant stop event and end the turn.
- `COMPACT`: summarize the conversation and reset the session context.
- `INJECT_MESSAGE`: append a runtime user message before the LLM call.

Builtin middleware:

- `TurnLimitMiddleware`: stops programmatic or configured sessions after a max
  number of assistant turns.
- `PriceLimitMiddleware`: stops once estimated session cost exceeds a max price.
- `AutoCompactMiddleware`: triggers compaction when `context_tokens` reaches the
  active model's `auto_compact_threshold`.
- `ContextWarningMiddleware`: injects a warning when the context reaches a
  configured percentage of the compaction threshold.
- `ReadOnlyAgentMiddleware`: injects reminders and exit messages for read-only
  modes such as `plan` and `chat`.

Middleware is the runtime guardrail layer around the model loop. It does not
execute tools itself; it changes whether the next LLM turn happens and what
extra instructions are inserted before that turn.

## Agents And Modes

Agent profiles live in `vibe.core.agents.models`. A profile is a named config
overlay with a safety label and a type:

- `AgentType.AGENT`: selectable as the main session agent.
- `AgentType.SUBAGENT`: callable through the `task` tool only.

Builtin primary agents:

- `default`: normal coding agent; tool calls require configured permissions.
- `plan`: read-only exploration and planning mode; may edit only the plan file
  through allowlisted paths.
- `chat`: read-only conversational mode for explanations and discussion.
- `accept-edits`: auto-approves file edits but keeps other tools governed by
  their config.
- `auto-approve`: bypasses tool permission prompts.
- `lean`: optional specialized Lean profile, enabled only after installation.

Builtin subagent:

- `explore`: read-only codebase exploration subagent with `grep` and
  `read_file`.

`AgentManager` discovers builtin and custom TOML agent profiles, applies
enabled/disabled filters, and exposes the active profile's config overlay as
`agent_manager.config`. Switching agents invalidates the cached config and
causes `AgentLoop.reload_with_initial_messages()` to rebuild tools, skills,
backend, pricing, and system prompt.

## Tools

All tools subclass `BaseTool` from `vibe.core.tools.base`. A tool declares:

- a Pydantic args model
- a Pydantic result model
- a config model based on `BaseToolConfig`
- a state model based on `BaseToolState`
- `async def run(args, ctx)` that yields stream events and then a result

`BaseTool.invoke()` validates raw model arguments before `run()` executes.
`BaseTool.get_parameters()` turns the args model into the JSON schema sent to
the LLM.

Builtin core tools:

| Tool | Purpose | Default Permission |
| --- | --- | --- |
| `ask_user_question` | Ask structured questions in interactive mode. | `always` |
| `bash` | Run a one-off shell command with output capture. | `ask` |
| `exit_plan_mode` | Ask to leave plan mode and switch agent. | `always` |
| `grep` | Search files with ripgrep or Python fallback. | `always` |
| `read_file` | Safely read bounded text from a file. | `always` |
| `search_replace` | Apply SEARCH/REPLACE edit blocks. | `ask` |
| `skill` | Load a skill prompt by name. | `always` |
| `task` | Delegate work to a subagent. | `ask` |
| `todo` | Maintain an in-session todo list. | `always` |
| `web_fetch` | Fetch URL content. | `ask` |
| `web_search` | Search the web through configured backend support. | `ask` |
| `write_file` | Create or overwrite UTF-8 files. | `ask` |

So the repository ships 12 core builtin tools. The total runtime tool count may
be higher because `ToolManager` also discovers:

- custom Python tools from configured `tool_paths`
- project and user tool directories
- MCP server proxy tools
- connector proxy tools
- ACP-specific tool overrides when running through ACP

## Tool Discovery Flow

`ToolManager` computes search paths from:

- builtin tool directory
- `tool_paths` from config
- project tool directories from the harness file manager
- user tool directories from the harness file manager

It dynamically imports Python files, finds concrete `BaseTool` subclasses, and
registers them by `ClassName -> snake_case` unless the class overrides
`get_name()`.

Remote tools are added later:

- `MCPRegistry` discovers tools from configured MCP servers over `stdio` or
  HTTP/streamable HTTP and creates proxy `BaseTool` classes.
- `ConnectorRegistry` discovers Mistral connectors and creates connector proxy
  tool classes.
- `ToolManager.integrate_all()` discovers MCP and connector tools concurrently.
- With deferred init, the CLI starts quickly, then remote discovery happens in a
  background thread and the system prompt is refreshed.

Tool filtering happens after discovery:

- per-source disabled MCP or connector tools
- global `enabled_tools`
- global `disabled_tools`

## Tool Permission Flow

Every tool has `BaseToolConfig.permission`:

- `always`: execute without asking.
- `ask`: request approval unless granular rules already cover it.
- `never`: skip and return a tool result explaining denial.

At execution time, `AgentLoop._should_execute_tool()` applies permissions in
this order:

```text
if active config bypasses tool permissions:
  execute
else:
  ask tool.resolve_permission(args)
  if no override:
    use tool config permission
  if always:
    execute
  if never:
    skip
  if ask:
    check session-approved granular rules
    otherwise call approval_callback
```

Granular approvals use `RequiredPermission` scopes:

- `COMMAND_PATTERN`
- `OUTSIDE_DIRECTORY`
- `FILE_PATTERN`
- `URL_PATTERN`

The user can approve once, approve always for the session, or persist allowlist
patterns to config. Session approvals are stored as `ApprovedRule` entries.

## Sandbox And Safety Boundaries

This codebase does not implement a container or OS-level sandbox for tool
execution. Its sandbox model is a policy boundary enforced by tool permissions,
path checks, command checks, read-only agent modes, scratchpad auto-allow rules,
session logging, and rewind snapshots.

File tool boundary:

- Scratchpad paths are always allowed.
- Denylist glob patterns win before allowlist patterns.
- Allowlist glob patterns auto-approve.
- Sensitive patterns such as `.env` require approval.
- Paths outside the current working directory require approval, or are denied
  when the tool permission is `never`.

Bash boundary:

- Commands are parsed with tree-sitter bash on Unix.
- Default safe read-only commands are allowlisted.
- Interactive shells, editors, debuggers, standalone interpreters, and similar
  commands are denylisted.
- `find -exec`, `-execdir`, `-ok`, and `-okdir` always require approval.
- Commands referencing paths outside the workdir require granular approval.
- Processes run non-interactively with environment flags such as `CI=true`,
  `NONINTERACTIVE=1`, `NO_TTY=1`, disabled pagers, timeout, output byte cap, and
  subprocess cleanup on cancellation.

Read-only mode boundary:

- `plan` and `chat` inject strong runtime reminders through middleware.
- `chat` enables only conversational/read tools.
- `plan` keeps edit tools disabled except for the generated plan file path.

Scratchpad:

- Primary sessions create a temp scratchpad directory.
- The system prompt tells the model it can use this for temporary work.
- File tools auto-allow paths inside active scratchpads.
- Subagents receive the scratchpad path in the delegated task text.

Rewind:

- Before a user turn, `RewindManager` creates a checkpoint.
- Editing tools expose pre-write file snapshots through `get_file_snapshot()`.
- Rewind can truncate conversation history and optionally restore changed files.

## Subagents

Subagents are implemented by the `task` tool, not by a separate scheduler.

Flow:

```text
main agent calls task(task=..., agent="explore")
  -> Task.resolve_permission() checks agent allowlist/denylist
  -> Task.run() verifies requested profile has AgentType.SUBAGENT
  -> create nested AgentLoop(is_subagent=True, defer_heavy_init=True)
  -> pass approval callback and scratchpad instructions
  -> run subagent_loop.act(task_text)
  -> stream summarized tool progress back to parent as ToolStreamEvent
  -> return accumulated assistant text as TaskResult
```

Security constraints:

- Main sessions cannot select subagent profiles as `--agent`.
- The `task` tool rejects non-subagent profiles.
- The built-in `explore` subagent only has `grep` and `read_file`.
- Subagents do not create their own scratchpad; they share the parent-provided
  scratchpad path through the prompt.
- Subagent session logs are stored under the parent session directory's
  `agents` folder when session logging is available.

## Context Sharing And Isolation

Context in Vibe is intentionally split into three different categories:

- **Conversation context**: the `LLMMessage` list sent to the model.
- **Runtime context**: tool managers, active agent profile, callbacks, session
  paths, scratchpad path, telemetry metadata, and config.
- **Persistent context**: session logs, config files, custom skills, custom
  tools, custom agents, prompts, and trusted project docs.

The main `AgentLoop` owns the primary conversation context. On every model call,
the backend receives the current `MessageList`, active tool schemas, model
settings, and request metadata. Tool results are appended back into the same
message list as `role="tool"` observations, which is why the next LLM turn can
reason over previous command output, file reads, edit results, and failures.

Subagents do not receive a deep copy of the parent conversation. The `task` tool
creates a fresh nested `AgentLoop` with `is_subagent=True`, the requested
subagent profile, and a fresh system prompt. The delegated task text becomes the
subagent's first user message. The parent shares only operational context that
the child needs:

- the approval callback, so subagent tool calls use the same user approval path
- entrypoint metadata, so telemetry remains associated with the same entrypoint
- a session logging directory under the parent session, if available
- the scratchpad path, injected into the delegated prompt

The result crosses back in one direction: the parent receives the subagent's
accumulated assistant response as the `task` tool result, and it may receive
progress updates for subagent tool results as `ToolStreamEvent`s. The child's
full internal message history is not merged into the parent history. This keeps
the parent's context window smaller and prevents exploratory details from
polluting the main thread.

```text
main AgentLoop messages
  -> assistant calls task(...)
  -> Task creates fresh subagent AgentLoop
  -> subagent receives task text + scratchpad note
  -> subagent runs its own LLM/tool loop
  -> parent receives progress + final TaskResult.response
  -> parent continues with only that summarized result in context
```

## Scratchpad Isolation

Primary sessions create a session-scoped temporary scratchpad directory with
`init_scratchpad(session_id)`. Subagent loops do not create their own
scratchpad, but the parent passes the scratchpad path in the delegated task
prompt. File tools recognize scratchpad paths globally through
`is_scratchpad_path()`.

Scratchpad behavior:

- It is a temporary filesystem directory, not part of the repository.
- It is session-scoped and intended for drafts, intermediate artifacts, scripts,
  and outputs that should not become project files.
- File tools auto-approve paths inside active scratchpads.
- Path checks resolve symlinks and relative paths before deciding whether a path
  is inside a scratchpad.
- Subagents can use the same scratchpad path, which gives them a low-friction
  way to exchange intermediate files with the main agent.

Scratchpad isolation is practical rather than absolute: it separates temporary
work from project files and removes approval friction for that area, but it is
not an OS-level sandbox. Scratchpad files are not automatically persisted as
session artifacts. They may still be referenced in messages or tool results,
and those messages can be persisted by session logging.

## Session Logging And Persistent Storage

Session logging is handled by `SessionLogger`. When enabled, a session gets a
directory under `session_logging.save_dir`, using the configured
`session_prefix`, timestamp, and shortened session id. By default, the save
directory resolves under Vibe's session log directory in `VIBE_HOME`.

Each logged session stores:

- `messages.jsonl`: append-only non-system conversation messages.
- `meta.json`: metadata written atomically through a temporary file and
  `os.replace`.

`messages.jsonl` stores user, assistant, and tool messages. System messages are
not appended there. On resume, Vibe loads non-system messages from
`messages.jsonl`, then uses the current runtime to generate a fresh system
prompt. This means resumed conversations keep the user-visible conversation and
tool observations while picking up current config, tools, skills, agents, and
project instructions.

`meta.json` stores:

- session id and parent session id
- start and end timestamps
- git commit and branch at session start
- working directory
- username
- title
- token/tool stats
- total message count
- available tool schemas at save time
- base config snapshot
- active agent profile name and overrides
- current system prompt snapshot
- scheduled loop metadata

Subagent persistence is nested. When the `task` tool runs with a parent
`session_dir`, it creates a `SessionLoggingConfig` whose save directory is:

```text
<parent-session-dir>/agents
```

and whose prefix is the subagent name. That gives each subagent its own
`messages.jsonl` and `meta.json` without mixing those messages into the parent
session log.

Long-term user and project storage is handled separately through the harness
files manager:

- user config: `VIBE_HOME/config.toml`
- user skills/tools/agents/prompts: directories under `VIBE_HOME`
- user instructions: `VIBE_HOME/AGENTS.md`
- trusted project config: `<repo>/.vibe/config.toml`
- trusted project skills/tools/agents/prompts: `<repo>/.vibe/...`
- trusted project instructions: checked-in `AGENTS.md` files

Project-local files are loaded only when project sources are enabled and the
working directory is trusted.

## Plan Mode

Plan mode is a dedicated read-only workflow:

1. `AgentLoop` owns a `PlanSession`, which lazily creates a plan file path under
   the Vibe plans directory.
2. `ReadOnlyAgentMiddleware` injects plan-mode instructions before the first
   plan turn.
3. The plan profile config makes `write_file` and `search_replace` denied by
   default but allowlisted for the generated plan file path.
4. The agent writes or edits only that plan file while exploring with read-only
   tools.
5. `exit_plan_mode` reads the plan file, asks the user for confirmation, then
   switches to either `accept-edits` or `default`.
6. Agent switching reloads the runtime with updated permissions and a refreshed
   system prompt.

## Skills

Skills are prompt bundles discovered by `SkillManager`.

Discovery sources:

- builtin skills
- configured `skill_paths`
- project skill directories
- user skill directories

Each skill is a directory with `SKILL.md` frontmatter parsed into
`SkillMetadata`. The system prompt lists available skills and tells the model to
use the `skill` tool to load full instructions. User-invocable skills also
appear as slash commands; ACP exposes them as available commands.

Skills are not tools by themselves. They are reusable instructions that can
guide the agent into using existing tools in a specialized way.

## Context, Compaction, And Session Forking

The loop tracks token usage in `AgentStats`.

When `AutoCompactMiddleware` triggers:

1. The loop yields `CompactStartEvent`.
2. `AgentLoop.compact()` saves the current session.
3. It appends the compaction utility prompt silently.
4. It calls the configured compaction model, or the active model by default.
5. It resets messages to `[system_message, summary_message]`.
6. It creates a new session id with the old one as parent.
7. It counts tokens for the compacted context.
8. It resets middleware state for compaction.
9. It yields `CompactEndEvent`.

Forking creates a new `AgentLoop` with copied config and selected message
history. ACP exposes this as `fork_session`.

## Hooks

Experimental hooks are loaded from filesystem config and managed by
`HooksManager`. After the assistant appears to have completed a turn,
`AgentLoop` runs `POST_AGENT_TURN` hooks. A hook can emit normal events or
return a `HookUserMessage`, which is injected as a new user message and causes
the agent loop to continue.

This lets repository policies or automated checks ask the agent to retry or
repair work after a turn.

## ACP-Specific Flow

ACP wraps the core loop without replacing it.

For a new ACP session:

```text
initialize()
  -> report capabilities and auth methods
new_session(cwd)
  -> load config with non-interactive tools disabled
  -> append ACP tool override paths based on client capabilities
  -> create AgentLoop(streaming=True, deferred init=True)
  -> create AcpSessionLoop
  -> install approval callback if permissions are not bypassed
  -> send available commands and warm deferred init
prompt()
  -> build text prompt from ACP content blocks
  -> handle builtin slash commands or skill slash commands
  -> run AgentLoop.act()
  -> translate core events to ACP session_update messages
```

ACP uses tool overrides in `vibe/acp/tools/builtins` for client-backed file
system, terminal, web, skill, and task behaviors. Client capability flags decide
which override paths are added.

ACP sessions explicitly track background tasks and prompt tasks in
`AcpSessionLoop`, so cancellation can stop the active prompt while session close
cancels all session-scoped work.

## Coding Flow In Practice

For a coding request, the intended loop is:

1. The model receives repository context, `AGENTS.md` instructions, tools, and
   active agent profile rules in the system prompt.
2. It explores with `grep`, `read_file`, `bash` read-only commands, or an
   `explore` subagent.
3. It asks clarification questions with `ask_user_question` when interactive and
   necessary.
4. It edits with `search_replace` or `write_file`; snapshots are captured before
   writes.
5. It runs verification with `bash` when commands are approved or allowlisted.
6. It observes tool results and iterates until it can answer.
7. The UI or ACP client displays assistant text, reasoning, tool calls, tool
   results, progress, usage, and compaction events from the event stream.
8. Session logs persist the messages, stats, config, tool state, and active
   agent profile for resume, replay, and debugging.

## Compile And Test Flow

Vibe does not have a dedicated compile or test tool. Compile, test, lint, type
check, and build commands run through the `bash` tool.

Examples in this repository:

```bash
uv run pytest
uv run pyright
uv run ruff check --fix .
uv run ruff format .
uv run pre-commit run --all-files
```

The `bash` tool starts a one-off subprocess shell for each command. The shell
execution is stateless from the tool's perspective: every tool call starts a new
process, captures stdout and stderr, enforces a timeout, caps output bytes, and
kills the process on timeout or cancellation.

The execution environment is the current working directory, not a containerized
or chrooted sandbox. Isolation is implemented through command policy and
process settings:

- commands are parsed before execution on Unix
- known read-only commands can be allowlisted
- interactive editors, standalone shells, debuggers, and risky command patterns
  can be denied
- sensitive commands or command shapes require approval
- file paths outside the workdir require granular approval
- stdin is disabled
- environment flags make commands noninteractive, such as `CI=true`,
  `NONINTERACTIVE=1`, and `NO_TTY=1`
- pagers are disabled or made noninteractive
- commands have configurable timeouts and output caps

For compile and test tasks, the model normally uses dedicated read/search/edit
tools for file operations, then uses `bash` only for verification commands. The
LLM observes the command result as a tool message and decides whether to fix
errors, rerun a narrower test, or report success.

```text
assistant edits code
  -> assistant calls bash("uv run pytest ...")
  -> permission resolver checks the command
  -> approval callback runs if needed
  -> subprocess executes in current workdir
  -> stdout/stderr/returncode become a BashResult
  -> BashResult is appended as a tool message
  -> assistant uses that result for the next step
```

Programmatic runs can restrict the available verification surface with
`--enabled-tools`. For example, enabling only `bash`, `read_file`, and `grep`
means the model can inspect and run commands but cannot edit unless edit tools
are also enabled.

## Key Files To Read Next

- `vibe/core/agent_loop.py`: main loop, LLM calls, middleware handling, tool
  execution, compaction, fork, switch agent.
- `vibe/core/middleware.py`: middleware actions and builtin middleware.
- `vibe/core/tools/base.py`: tool interface, config, state, validation,
  snapshots.
- `vibe/core/tools/manager.py`: tool discovery, filtering, MCP and connector
  integration.
- `vibe/core/tools/permissions.py`: granular permission models.
- `vibe/core/tools/builtins/`: builtin tool implementations.
- `vibe/core/agents/models.py`: builtin agent and subagent profiles.
- `vibe/core/agents/manager.py`: agent discovery and config overlays.
- `vibe/core/system_prompt.py`: prompt composition.
- `vibe/core/programmatic.py`: headless one-shot agent execution.
- `vibe/cli/cli.py`: CLI construction of the core loop.
- `vibe/acp/acp_agent_loop.py`: ACP session and event translation layer.
- `vibe/core/skills/manager.py`: skill discovery and slash-command handling.
- `vibe/core/rewind/manager.py`: file snapshots and rewind.
- `vibe/core/session/session_logger.py`: session persistence, metadata, and
  message logging.
- `vibe/core/session/session_loader.py`: resume and session listing.
- `vibe/core/scratchpad.py`: scratchpad creation and path checks.
