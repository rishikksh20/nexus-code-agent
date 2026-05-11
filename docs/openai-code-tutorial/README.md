# Minimal Agent Harness — Tutorial Series

A sequential coding tutorial for building a **production-capable AI agent harness** in Python from scratch. No frameworks. Every component is explained, built, and tested step by step.

---

## Reading order

Follow the chapters in order. Each one builds directly on the previous.

## Companion review documents

- [IMPROVEMENTS.md](IMPROVEMENTS.md) — the earlier gap analysis and missing-chapter plan
- [IMPROVEMENTS-POST-AUDIT.md](IMPROVEMENTS-POST-AUDIT.md) — post-implementation audit of the full tutorial series and the harness it produces
- [COMPREHENSIVE-AUDIT-AND-ACTION-PLAN.md](COMPREHENSIVE-AUDIT-AND-ACTION-PLAN.md) — consolidated detailed review, merged findings, and prioritized improvement roadmap

### Foundation

| File | What you build |
|---|---|
| [00-agent-basics.md](00-agent-basics.md) | Mental model, REPL loop, fake model, the agent idea |
| [01-agent-loop.md](01-agent-loop.md) | `Message`, `ToolCall`, `ModelResponse`, `Agent`, async events |
| ↳ [01-1-streaming.md](01-1-streaming.md) | `stream()` on model client, live token output, streamed REPL renderer |
| [02-tools.md](02-tools.md) | `BaseTool`, `ToolRegistry`, context, validation, OpenAI adapter |
| ↳ [02-1-mcp-integration.md](02-1-mcp-integration.md) | `MCPClient`, `MCPToolAdapter`, discover tools from external servers |
| ↳ [02-2-plugins.md](02-2-plugins.md) | `PluginLoader`, entry-point discovery, local `plugins/` directory |
| [03-session-manager.md](03-session-manager.md) | `SessionSnapshot`, `SessionStore`, persist/resume/export, schema versioning |
| ↳ [03-1-context-compaction.md](03-1-context-compaction.md) | `TokenEstimator`, sliding window, tool result pruning, token budget |

### Runtime capabilities

| File | What you build |
|---|---|
| [04-hooks.md](04-hooks.md) | `HookEvent`, `HookExecutor`, async timeout, audit and logging hooks |
| [05-context-engineering.md](05-context-engineering.md) | `ContextBuilder`, layered prompt assembly, dynamic system prompt |
| [06-memory-and-storage.md](06-memory-and-storage.md) | `MemoryStore`, `MemoryEntry`, TTL pruning, save/search/delete tools |
| [07-permissions.md](07-permissions.md) | `PermissionChecker`, `PermissionPolicy`, path rules, command patterns, ephemeral grants |
| ↳ [07-1-docker-sandboxing.md](07-1-docker-sandboxing.md) | `DockerSandbox`, `SandboxedBashTool`, container isolation |
| [08-skills.md](08-skills.md) | `Skill`, `SkillRegistry`, on-demand instruction packs, `SkillTool` |
| [09-plan-mode-and-auto-mode.md](09-plan-mode-and-auto-mode.md) | `ExecutionMode`, mode-gated permissions, `ModeChangedEvent` |

### Multi-agent and safety

| File | What you build |
|---|---|
| [10-swarms-and-delegation.md](10-swarms-and-delegation.md) | `SpawnRequest`, `TaskRecord`, `SwarmBackend`, parallel worker result routing |
| [11-agent-communication.md](11-agent-communication.md) | `AgentMessage`, `MessageKind`, `InMemoryMailbox`, `FileMailbox` (durable) |
| [12-dangerous-actions-and-user-confirmation.md](12-dangerous-actions-and-user-confirmation.md) | `ConfirmationRequest`, `ConfirmationKind`, `DangerLevel`, worker approval via mailbox |
| [13-guardrails-and-safety.md](13-guardrails-and-safety.md) | Four-layer safety envelope, `GuardrailChecker`, `AuditTrail`, injection scanning |

### Production

| File | What you build |
|---|---|
| ↳ [13-1-configuration.md](13-1-configuration.md) | `agent.toml`, `AgentConfig`, env var overrides, `load_config()` |
| [14-testing-the-harness.md](14-testing-the-harness.md) | `FakeModelClient`, `RecordingHook`, full `tests/` scaffold for every layer |
| [15-advanced-context-and-storage.md](15-advanced-context-and-storage.md) | `.agent/` workspace dir, `~/.agent/` user profile, `KnowledgeUpdater`, `ProfileUpdater`, post-session learning |
| [16-advanced-logging-and-observability.md](16-advanced-logging-and-observability.md) | `UsageSnapshot+`, structured JSON logs, pricing config, token/cost tracking, optional OpenTelemetry tracing |
| [17-cli-flags-and-headless-mode.md](17-cli-flags-and-headless-mode.md) | `HeadlessConfig`, full `argparse` surface, `run_headless()`, exit codes, CI/pipe usage patterns |
| [18-config-hierarchy.md](18-config-hierarchy.md) | Two-tier config (`~/.agent/agent.toml` + `agent.toml`), `_merge()`, `UIConfig`, `init_global_config()`, `config show global\|local\|merged` |
| [19-slash-commands-and-repl-control.md](19-slash-commands-and-repl-control.md) | `SlashCommandRouter`, `ReplState`, `/mode`, `/config`, `/skills`, `/session`, `/tools`, `/memory`, `/history`, `/quit`, plugin extension |

---

## Final file structure

After completing all chapters, your project looks like this:

```
agent/
    __init__.py
    models.py          # Message, ToolCall, ModelResponse, ToolResult, SessionSnapshot
    tools.py           # BaseTool, ToolRegistry, all concrete tools
    events.py          # All runtime event dataclasses
    client.py          # ModelClient base, DemoModelClient
    agent.py           # Agent class — owns loop, history, permissions, hooks
    session.py         # SessionSnapshot, SessionStore, SESSION_ID contextvar
    hooks.py           # HookEvent, HookResult, HookExecutor, builtin hooks
    prompts.py         # ContextBuilder, DEFAULT_BASE_PROMPT
    memory.py          # MemoryEntry, MemoryStore, prune()
    permissions.py     # PermissionDecision, PermissionPolicy, PermissionChecker
    skills.py          # Skill, SkillRegistry, SkillTool
    modes.py           # ExecutionMode, mode helpers, ModeChangedEvent
    swarm.py           # SpawnRequest, TaskRecord, TaskStatus, SwarmBackend
    mailbox.py         # AgentMessage, MessageKind, InMemoryMailbox, FileMailbox
    confirmation.py    # ConfirmationRequest, ConfirmationResult, DangerLevel
    guardrails.py      # GuardrailChecker, AuditTrail
    compaction.py      # TokenEstimator, compact_messages()
    config.py          # AgentConfig, load_config(), env overrides
    mcp.py             # MCPClient, MCPToolAdapter, discover_mcp_tools()
    plugins.py         # PluginLoader, load_plugins()
    sandbox.py         # DockerSandbox, SandboxedBashTool
    agent_dir.py       # AgentDirs, resolve_agent_dirs()          ← Ch15
    workspace_knowledge.py  # WorkspaceKnowledge, KnowledgeUpdater, extract_facts_from_text()  ← Ch15
    user_profile.py    # UserProfile, ProfileUpdater              ← Ch15
    telemetry.py       # UsageSnapshot+, PricingRule, cost estimation, telemetry records  ← Ch16
    logging.py         # JSONL logger, redaction, correlation IDs, optional tracing glue  ← Ch16
    headless.py        # run_headless(), HeadlessResult, write_output(), resolve_prompt()  ← Ch17
    slash_commands.py  # SlashCommandRouter, ReplState, all command handlers  ← Ch19
    openai_client.py   # OpenAIStreamingClient (real provider)

main.py                # Entry point: full argparse, headless branch, REPL fallback  ← Ch17
agent.toml             # Project config (committed)
~/.agent/agent.toml    # Global personal config (never committed)  ← Ch18
.env                   # Secrets and local overrides (gitignored)
pyproject.toml         # [project.scripts] registers "agent" entry point  ← Ch17

.agent/                # Created at runtime — workspace-scoped context
    sessions/            # session JSON files
    memory/              # memory Markdown entries
    knowledge.md         # auto-updated workspace knowledge (committable)
    facts.json           # extracted environment facts
    audit-trail.jsonl    # append-only audit log
    logs/
        runtime.jsonl    # structured runtime logs
        errors.jsonl     # optional error-only stream

~/.agent/              # User-scoped global context
    profile.md           # user behavioral model and preferences
    workspaces.json      # registry of known workspaces
    tools.md             # user's preferred tools

skills/                # Skill SKILL.md packs
plugins/               # Local plugin .py files
mailbox/               # Durable mailbox files (FileMailbox)

tests/
    conftest.py        # FakeModelClient, RecordingHook, collect_events()
    test_tools.py
    test_agent_loop.py
    test_session.py
    test_permissions.py
    test_guardrails.py
    test_hooks.py
    test_memory.py
    test_headless.py   # headless runner, exit codes, output formats  ← Ch17
    test_config.py     # two-tier merge, env overrides, deep-merge  ← Ch18
    test_slash_commands.py  # mode switch, history clear, compound routing  ← Ch19
```

---

## What each layer does

```
User input
    │
    ▼
REPL loop (main.py)                    ← manages session, renders events
    │
    ▼
Agent.run()                            ← owns messages, drives one turn
    │
    ├─ ContextBuilder                  ← assembles layered system prompt
    │     ├─ UserProfile               ← ~/.agent/profile.md (user prefs)
    │     ├─ WorkspaceKnowledge        ← .agent/knowledge.md (project context)
    │     └─ MemoryStore               ← per-turn relevant memory entries
    ├─ compact_messages()              ← keeps messages within token budget
    ├─ HookExecutor (pre_tool_use)     ← lifecycle extension points
    ├─ GuardrailChecker                ← unconditional hard boundaries
    ├─ PermissionChecker               ← configurable policy enforcement
    ├─ confirm_action()                ← human-in-the-loop for risky actions
    ├─ tool.execute()                  ← actual work (sandboxed if Docker)
    ├─ HookExecutor (post_tool_use)    ← logging, notifications
    ├─ TelemetryRecorder               ← token usage, model config, cost estimates
    ├─ JSONL / OpenTelemetry exporter  ← structured logs and optional traces
    └─ AuditTrail                      ← durable append-only record (.agent/)

HookEvent.STOP (session end)
    ├─ KnowledgeUpdater                ← updates .agent/knowledge.md
    └─ ProfileUpdater                  ← updates ~/.agent/profile.md

SwarmBackend                           ← manages worker task lifecycle
InMemoryMailbox / FileMailbox          ← typed agent-to-agent messages
SessionStore (.agent/sessions/)        ← persist/resume across runs
MemoryStore (.agent/memory/)           ← durable long-term knowledge
```

--- 

## Run the tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

---

## Concepts borrowed from this codebase

The following concepts in this tutorial series are inspired by real patterns in the [OpenHarness](https://github.com/openharness/openharness) reference implementation:

- Event-driven runtime loop with typed event dataclasses
- Hook lifecycle system with executor and payload contracts
- Layered context engineering with `ContextBuilder`
- Session snapshot schema with `carry_over` metadata
- File-based memory with keyword retrieval
- Permission policy with path rules and command deny patterns
- Skill instruction packs loaded from Markdown files
- Mode-gated permission enforcement (PLAN / DEFAULT / AUTO)
- Coordinator + worker swarm with typed task lifecycle
- Typed mailbox with correlation IDs for agent-to-agent communication
- Four-layer safety envelope with `AuditTrail`
- MCP integration pattern (MCPClient + MCPToolAdapter)
- Plugin entry-point discovery

All code in this series is generic and not tied to the OpenHarness repository. The concepts are reusable across any Python agent project.
