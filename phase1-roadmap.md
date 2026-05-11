# Nexus Agent Framework - Phase 1 Roadmap

## Context Alignment

- Current implementation phase: `docs/action-plan/01-foundations`
- Priority source: `docs/action-plan/*`
- Tutorial cross-check: `docs/openai-code-tutorial/*`
- Cross-cutting constraints included now because they shape phase-1 boundaries:
  - provider boundary discipline
  - local and global config hierarchy
  - workspace knowledge base creation on init
  - slash commands and CLI flags
  - interactive and headless execution

## Scope For This Build

Phase 1 will implement a minimal, testable harness under `nexus/` with these capabilities:

1. Async CLI REPL outer loop with Rich rendering.
2. Typed runtime models for messages, tool calls, tool results, runtime requests, and runtime responses.
3. Provider-neutral agent loop with a fake OpenAI-compatible model client for deterministic development.
4. Tool abstraction, registry, execution context, and two builtin tools:
   - `get_time`
   - `write_note`
5. Streaming-capable model interface and event-driven agent execution.
6. Session persistence, context assembly, and lightweight compaction.
7. File-based workspace memory plus workspace knowledge bootstrap on `init`.
8. Permission gating with `plan`, `default`, and `auto` execution modes.
9. Two-tier config resolution:
   - global: `~/.nexus/config.toml`
   - local: `{workspace}/.nexus/config.toml`
10. Slash-command router for in-session operational control.
11. Headless CLI execution with prompt/file/stdin input.
12. Minimal lightweight pytest coverage using a fake model client.

## Phase-1 Module Plan

### Package Layout

```text
nexus/
  __init__.py
  app.py
  models.py
  prompts.py
  cli/
    __init__.py
    args.py
    headless.py
    init.py
    input.py
  config/
    __init__.py
    defaults.py
    loader.py
  integrations/
    __init__.py
    fake_model.py
    openai_compatible.py
  memory/
    __init__.py
    store.py
    workspace.py
  runtime/
    __init__.py
    agent.py
    context.py
    execution.py
    hooks.py
    permissions.py
    repl.py
    repl_state.py
    sessions.py
    slash_commands.py
  tools/
    __init__.py
    base.py
    builtin.py
tests/
  conftest.py
  test_agent.py
  test_cli.py
  test_config.py
  test_sessions.py
  test_tools.py
```

### Implementation Order

1. Project scaffold with `uv`-friendly `pyproject.toml`.
2. Core typed models and provider boundary.
3. Tool registry, builtin tools, and permission enforcement.
4. Session store, context builder, compaction, and memory store.
5. Config loader, workspace/global directory bootstrap, and knowledge base init.
6. Interactive REPL, slash commands, and headless runner.
7. Lightweight tests and focused validation.

## Explicit Boundaries

- Provider wire formats stay inside `nexus.integrations`.
- Runtime loop only sees normalized request/response types.
- REPL owns user interaction; agent loop owns orchestration.
- Tool registry stores tools only; it does not enforce policy.
- Permission checks run before every tool execution.
- Session state, durable memory, and workspace knowledge remain separate stores.
- Mutating tools are denied in `plan` mode.

## Deferred To Later Phases

- Real provider SDK integration
- MCP and plugins
- multi-agent delegation and mailboxing
- sandbox execution
- advanced telemetry and cost tracking
- skills loading beyond command/config seams

## Validation Targets

- Agent loop emits typed events and executes registered tools.
- Config merge order resolves defaults, global, local, env, then CLI overrides.
- `init` creates `.nexus/` workspace structure and knowledge base files.
- Headless mode runs one prompt and exits with a stable response.
- Slash commands mutate live REPL state without invoking the model.
- Session persistence round-trips through JSON.

## Next Step

Scaffold the `nexus/` package and implement the typed runtime skeleton first, then validate it with focused pytest coverage before expanding the CLI surface.