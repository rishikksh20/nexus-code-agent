# Nexus Codebase Context

This document is a working context guide for future development on Nexus. It captures the current implementation shape, code style, runtime flow, extension surfaces, and design constraints that matter when changing the codebase.

## Project Shape

Nexus is a CLI-first Python coding agent framework. The package is intentionally small-dependency and terminal-focused: `click` owns the CLI, `rich` owns terminal rendering, `httpx` is available but live provider calls currently use stdlib `urllib`, and tests use `pytest` plus `pytest-asyncio`.

The package entry point is:

- `pyproject.toml`: publishes the `nexus` console script as `nexus.app:main`.
- `nexus/app.py`: top-level runtime orchestrator. It loads config, initializes state directories, builds tools/resources, creates the `Agent`, dispatches headless or interactive mode, and tears down long-lived resources.

Major package areas:

- `nexus/models.py`: shared runtime contracts: messages, tool calls/results, stream events, confirmation requests, usage, telemetry, and tool execution context.
- `nexus/runtime/`: session state, REPL/headless execution, slash commands, delegation, sandbox runtime wiring, and post-session updates.
- `nexus/tools/`: tool protocol/registry and first-party built-in tools.
- `nexus/security/`: execution modes, approval policies, permission checks, command classification, and approval scoping.
- `nexus/context/` and `nexus/prompts/`: system prompt construction, context compaction, loop detection, and tool-output pruning.
- `nexus/config/`: config dataclass, defaults, TOML/env/CLI merge, validation, model limits.
- `nexus/integrations/`: fake provider, Ollama, OpenAI-compatible provider, retry helper, and MCP stdio client.
- `nexus/memory/`, `nexus/skills/`, `nexus/extensions/`, `nexus/hooks/`, `nexus/observability/`, `nexus/sandbox/`, `nexus/ui/`: persistence, skills, plugins, hooks, logs/metrics/audit, Docker sandboxing, and terminal UI.

## Coding Style

Use modern Python 3.11 style:

- `from __future__ import annotations` is used throughout.
- Prefer dataclasses with `slots=True` for runtime state and value objects.
- Prefer explicit type hints and small helper functions over dynamic dictionaries at module boundaries.
- Keep implementation dependency-light. Most core modules use only the standard library plus the small declared project dependencies.
- Use async for runtime/model/tool paths. Tools implement `async def execute(...) -> ToolResult`.
- Favor narrow, explicit modules. New built-in tools should usually live in `nexus/tools/builtin/<name>.py` and be registered in `nexus/tools/registry.py`.
- Preserve backward compatibility shims where existing tests or public imports depend on them, such as `nexus/tools/filesystem.py` re-exporting built-ins.
- Keep comments useful and sparse. Existing comments mostly explain architectural decisions, provider quirks, safety boundaries, or non-obvious state handling.
- Prefer exact, deterministic behavior in tests. Use `FakeModelClient`, `tmp_path`, and direct runtime objects instead of live providers.

String/path conventions:

- Files are read and written as UTF-8 unless a tool explicitly supports replacement/error handling.
- Workspace-relative paths are resolved against `ToolExecutionContext.working_directory`.
- `.nexus/` state is treated as managed runtime state. User-facing tools generally refuse to read or write it unless explicitly allowed by config and policy.
- Hidden/private paths are hidden from discovery/read tools unless `allow_hidden_paths` is enabled. `.nexus` remains special.

## Runtime Flow

CLI flow:

1. `nexus.app:main` invokes the Click CLI defined in `nexus/cli/args.py`.
2. `_dispatch_runtime()` loads config with CLI overrides, creates a `TerminalUI`, and enters `_run_app()`.
3. `_run_app()` ensures config/state directories exist and runs `init_workspace()` for workspace scaffolding.
4. `NexusApp.initialize()` applies model context limits, sets up hooks, builds the tool registry/resources, creates a model client, and constructs the `Agent`.
5. If a prompt source is present, `NexusApp.run_single()` calls `run_headless()`. Otherwise `NexusApp.run_interactive()` calls `run_repl()`.
6. `NexusApp.close()` shuts down delegation and MCP server connections.

Registry/resource build order in `NexusApp._build_registry()` is important:

1. Start `DelegationRuntime` if enabled.
2. Register core tools.
3. Load user plugins from `plugins_dir` unless disabled.
4. Connect MCP servers and register MCP tool adapters.
5. Register sandbox and sub-agent tools.

User turn and agent turn:

- Interactive input is handled by `run_repl()` in `nexus/runtime/repl.py`.
- Headless input is handled by `run_headless()` in `nexus/cli/headless.py`.
- Both paths call `run_agent_turn()`, which is the shared turn runner.
- `run_agent_turn()` prepares context, streams model events through `Agent.run()`, handles approval/clarification loops, records telemetry, and returns events.
- `ReplState.apply_events()` is the only normal place where completed runtime events are committed back into durable session history.

Agent loop:

- `Agent.run()` emits high-level start/stop events and delegates to `_agentic_loop()`.
- `_agentic_loop()` sends a `RuntimeRequest` to the model client, accumulates text deltas and streamed tool calls, appends assistant messages, evaluates tool permissions, executes tools, appends tool result messages, prunes older tool outputs, and checks loop patterns.
- It emits both newer reference-style events (`TEXT_DELTA`, `TOOL_CALL_START`, `AGENT_STOP`, etc.) and legacy Nexus event names (`model_response`, `tool_result`, `turn_completed`) for compatibility.
- Tool batches are committed after the whole batch completes. This helps preserve provider wire-format correctness for assistant tool calls and matching tool results.
- `max_loop_iterations` controls model/tool rounds per user turn. `max_tool_calls_per_turn` stops runaway tool use and saves a paused prompt so the user can type `continue`.

History/session handling:

- Sessions are JSON snapshots under `.nexus/sessions/`.
- `SessionStore.save()` writes atomically via a temp file then updates `latest_session.txt`.
- `sanitize_session_messages()` drops empty assistant messages and orphaned tool messages.
- `prepare_messages_for_model()` strips trailing assistant messages that strict providers may reject, especially after interrupted approval loops.
- `EphemeralSessionStore` is used for `--no-session`.

## Context And Prompt Design

System prompt construction starts in `ReplState.build_system_prompt()`:

1. `nexus.prompts.build_context_sections()` creates a `ContextSections` object.
2. `ContextBuilder().build()` renders sections into Markdown.
3. The result is stored on `state.current_system_prompt`.

Prompt sections include:

- Base identity/security/tool guidance from `nexus/prompts/system.py`.
- Environment, current task, tool list, active skills, project notes, persistent memory, and carry-over context.
- `developer_instructions` and `user_instructions` from config when present.

Context management:

- `TokenEstimator` uses the rough `len(text) // 4` heuristic.
- `ContextCompactor` summarizes older history into `CarryOverState.summarized_history` when soft limits are exceeded, then trims recent messages to hard limits.
- `prune_tool_outputs()` replaces old tool-result bodies with a placeholder once enough old tool output can be reclaimed.
- `_safe_recent_start()` avoids starting retained history with an orphaned tool message.
- `NexusApp._apply_model_context_limits()` derives default compaction thresholds from `config/model_limits.py`, unless the user overrode the defaults.

Design rule: preserve provider-valid message ordering before optimizing context. Assistant messages with tool calls need matching tool result messages by `tool_call_id`.

## Tools

Tool contracts live in `nexus/tools/base.py`:

- Subclass `Tool` when adding first-party tools.
- Set `name`, `description`, `kind`, `input_schema`, and `is_mutating`.
- Implement `execute(call_id, arguments, context)`.
- Override `get_confirmation()` when the UI should show a command, affected paths, or file diff before execution.
- Return `ToolResult` for both success and failure. Do not raise for expected tool errors.

Core tools are assembled in `nexus/tools/registry.py`:

- `get_time`
- `read_file`, `write_file`, `edit`, `insert_edit`, `apply_patch`, `modify_file`
- `glob`, `grep`, `ls`
- `bash`
- `memory`
- `todo`
- `web_fetch`, `web_search`

Tool design conventions:

- Read tools should be non-mutating and should honor hidden/private path policy.
- Write tools should validate workspace boundaries and refuse `.nexus/` managed state.
- Prefer surgical editing tools (`edit`, `modify_file`, `apply_patch`) over full rewrites.
- Tools should produce compact, useful output because tool results become model context.
- Use `metadata` on `ToolResult` for structured details used by tests/UI/observability.
- `write_file` is intentionally high risk and always requires confirmation outside plan denial.
- `bash` is mutating by default; the permission layer classifies command risk.

`nexus/tools/filesystem.py` is mostly a compatibility shim. New generic file tools belong under `nexus/tools/builtin/`; only Nexus-specific helpers/tools should stay in the shim.

## Safety And Approvals

Execution modes are defined by `ExecutionMode`:

- `plan`: mutating tools are denied.
- `default`: read-only tools are allowed, mutating tools require confirmation.
- `auto`: most mutating tools are allowed, but dangerous/high-risk bash still requires confirmation.

`PermissionChecker.evaluate()` is the central policy gate:

- It performs path hard-denials before mode/policy decisions.
- It special-cases `bash`, `write_file`, and `memory`.
- It allows read-only tools by default unless `auto_confirm_read_only` is disabled.
- It denies writes outside the workspace and direct writes into `.nexus/`, including `.nexus/memory`.

Approval state:

- `ApprovalManager` tracks per-turn/session/once approvals and refusals.
- `run_agent_turn()` handles approval callback loops for both interactive and headless modes.
- In headless non-TTY mode, missing approval produces exit code `3` (`EXIT_NEEDS_CONFIRM`).
- Clarification requests are used when required tool arguments are missing.

Shell safety:

- `CommandClassifier` and `ShellTool` classify commands as low/medium/high/dangerous.
- Some destructive shell fragments are blocked outright by `ShellTool`.
- Timed-out shell commands are killed by process group on Unix.
- Shell output is capped to protect context.

## Config

`AgentConfig` in `nexus/config/defaults.py` is the single config schema. Add new config keys there first, then update:

- `nexus/config/loader.py` for coercion/validation if needed.
- `nexus/cli/args.py` if the value needs a CLI override.
- `nexus/cli/init.py` if defaults should appear in generated config files.
- tests for default, TOML, env, and CLI behavior.

Config precedence in `load_config()`:

1. Defaults from `build_default_config()`.
2. Global TOML.
3. Local workspace TOML.
4. Environment variables and `.env`.
5. CLI overrides.
6. Provider-specific default adjustments.

Environment notes:

- `.env` in the workspace is injected into `os.environ` before config reads.
- Generic aliases are supported: `PROVIDER`, `MODEL`, `API_KEY`, `BASE_URL`.
- `AGENT_<FIELD_NAME>` overrides config fields.
- `AGENT_MAX_TOKENS` maps to `compaction_hard_limit`.

Validation is strict for providers, modes, approval policies, integer bounds, MCP server definitions, delegation workers/subagents, and allowed/denied tool overlap.

## Providers And MCP

Model clients satisfy the `ModelClient` protocol in `nexus/runtime/agent.py`:

- Primary method: `chat_completion(request, stream=True)` yielding `StreamEvent`.
- Legacy method: `complete()` is retained by some tests/adapters.

Provider implementations:

- `FakeModelClient`: deterministic local CI/test client.
- `OpenAICompatibleModelClient`: OpenAI-style `/chat/completions`, streaming SSE support, tool-call accumulation, retry on transient provider errors.
- `OllamaModelClient`: local Ollama integration.

OpenAI-compatible adapter responsibilities:

- Convert internal `Message` and `ToolCall` objects to provider wire format.
- Preserve assistant `tool_calls` and tool `tool_call_id`.
- Parse provider tool-call JSON arguments into `ToolCall.arguments`.
- Emit normalized `RuntimeResponse`/`StreamEvent` values.

MCP:

- `MCPClient` speaks JSON-RPC over subprocess stdio.
- `MCPServerRuntime.refresh()` discovers tools and reconnects if needed.
- `MCPToolAdapter` wraps remote tools as Nexus tools and marks them mutating by default.
- MCP tools can be prefixed per server to avoid name collisions.
- Failed MCP servers are logged/skipped but still tracked in runtime state for status commands.

## Skills, Plugins, Delegation, Sandbox

Skills:

- Skill roots come from builtin, global, and workspace locations.
- `RuntimeSession.create()` loads the skill registry unless `--no-skills` is set.
- `nexus-agent` is auto-activated when present.
- Active skill content is injected into the system prompt.
- Skill sub-agent tools are registered through `nexus/tools/subagents.py`.

Plugins:

- `PluginLoader` loads `*.py` files from `plugins_dir`.
- A plugin module must expose `register(registry_view, hooks)`.
- Plugin tools are registered with source `plugin` and origin equal to the plugin filename stem.
- `allowed_tools`/`denied_tools` policy applies during plugin registration.
- Plugin load/register failures are warnings, not fatal startup errors.

Delegation:

- `DelegationRuntime` owns an in-memory mailbox, task records, worker state, pending approvals, and optimistic resource versioning.
- Workers run their own `Agent` loop using a filtered tool registry.
- Coordinator and worker messages use typed `AgentMessage` values.
- Slash commands under `/delegate` expose status, task spawning, mailbox history, and approval decisions.
- Delegated work should stay narrow; workers report concise summaries to the coordinator.

Sandbox:

- Docker-backed sandbox tools are optional and gated by config.
- Sandbox settings include image, timeout, memory limit, network mode, read-only workspace, and tmp size.
- Sandbox command registration happens after core/plugin/MCP wiring.

## Hooks And Observability

Hooks are emitted for key lifecycle events:

- User prompt submission.
- Model usage notification.
- Confirmation requested.
- Tool denied.
- Pre/post tool use.
- Delegation mailbox events.
- Stop/session completion.

`setup_hooks()` wires runtime logging, metrics, and audit trails based on config. Mutating actions should continue to flow through tool execution and hooks so audit behavior remains centralized.

Correlation IDs:

- `run_repl()` and `run_headless()` create `turn_id` and `trace_id`.
- `ToolExecutionContext.metadata` carries turn/trace/worker/approval metadata.
- Agent hook payloads include correlation data via `_correlation_payload()`.

## CLI And Slash Commands

CLI options are defined in `nexus/cli/args.py`. Keep source selection mutually exclusive:

- `--prompt`
- `--prompt-file`
- `--stdin`

Headless output formats:

- `text`: final response.
- `json`: `{"response": ...}`.
- `jsonl`: complete role/content history.

Interactive slash commands are in `nexus/runtime/slash_commands.py`:

- `/help`
- `/mode`
- `/provider`
- `/skills`
- `/config`
- `/session`
- `/tools`
- `/memory`
- `/context`
- `/history`
- `/mcp`
- `/delegate`
- `/quit` and `/exit`

Unknown slash commands are intentionally forwarded to the agent as natural-language prompts.

## Testing Guidance

Run the automated suite with:

```bash
uv run --group dev python -m pytest -q
```

Test layout:

- `tests/test_agent.py`: agent loop and event behavior.
- `tests/test_repl.py`, `tests/test_cli.py`, `tests/test_terminal_ui.py`: user-facing runtime and UI behavior.
- `tests/test_tools.py`, `tests/test_filesystem_tools.py`: tool contracts and path behavior.
- `tests/test_security_manager.py`, `tests/test_sandbox.py`: approval/security/sandbox behavior.
- `tests/test_context.py`, `tests/test_sessions.py`, `tests/test_memory*`-related tests: context and persistence behavior.
- `tests/test_mcp.py`, `tests/test_plugins.py`, `tests/test_skills.py`, `tests/test_delegation.py`: extension systems.
- Markdown files in `tests/` are manual CLI scenario guides.

When changing a feature:

- Add focused unit tests near the module behavior.
- Add integration-style tests for CLI/session/approval flows when user-visible behavior changes.
- Use `tmp_path` for filesystem state and avoid touching real `~/.nexus`.
- Use `FakeModelClient` or small fake clients for provider behavior.
- Test provider wire-format changes with assistant tool calls plus matching tool result IDs.
- Test both confirmation-required and approval-denied paths for mutating tool changes.

## Change Checklist

Before editing:

- Read the module and the tests around it.
- Check whether there is a compatibility shim or legacy event name that tests depend on.
- Preserve session/message wire-format correctness.
- Keep new behavior scoped to the relevant package area.

When adding a tool:

- Implement `Tool`.
- Provide a precise JSON schema with `required` fields.
- Set `kind` and `is_mutating` correctly.
- Add confirmation previews/diffs for mutating file or shell behavior.
- Register in `get_core_tools()` if first-party.
- Add tests for success, validation failure, permission behavior, and workspace boundary behavior.

When adding config:

- Update `AgentConfig`.
- Update init/config docs if user-facing.
- Validate values in `loader.py`.
- Add CLI override only when needed.
- Test defaults, TOML, env, and override precedence.

When changing the agent loop:

- Preserve event compatibility.
- Ensure partial approval loops do not commit unresolved assistant tool calls.
- Keep tool results correlated by `call_id`.
- Watch compaction/pruning behavior on long histories.
- Verify headless and interactive paths still share behavior through `run_agent_turn()`.

## Current Design North Star

Nexus is designed as a conservative, inspectable coding-agent harness:

- CLI-first and REPL-friendly.
- Typed runtime boundaries.
- Provider adapters isolated from the agent loop.
- Tool execution centralized through registry, permissions, confirmations, hooks, and results.
- Workspace state stored under `.nexus/`, with direct access restricted.
- Long-session continuity via sessions, memory, skills, compaction, and post-session learning.
- Extension support through plugins, MCP, sub-agents, and sandboxing without making the core loop depend on them.

