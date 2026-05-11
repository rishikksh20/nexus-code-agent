# Python Agent Harness Action Plan

This directory is a build-first roadmap for creating a full-fledged but minimal agent harness in Python. It combines the architectural lessons from `agentic-framework-tutorial` with the implementation improvements from `openai-code-tutorial`, especially around streaming, MCP integration, plugins, context compaction, execution modes, testing, configuration, sandboxing, and observability.

After a second comparison pass against the attached `openai-code-tutorial` and `openai` folders, this plan also includes an advanced continuity stage that captures the concepts that were still underrepresented in the first draft: workspace knowledge, user profiles, advanced telemetry, deeper testing patterns, modular skills, confirmation state machines, provider-boundary discipline, and rollout hardening.

The goal is not to build a toy chatbot. The goal is to build a small but serious harness with clear boundaries:

- a typed agent loop
- a tool system with runtime context
- streaming output
- sessions and resumability
- context engineering and memory
- permissions and confirmation workflows
- hooks and execution modes
- extension points through plugins and MCP
- testability, logging, and production hardening
- workspace learning and user profiles
- provider-neutral client boundaries
- advanced telemetry, cost tracking, and redaction

## Who This Is For

This plan is aimed at developers who want to build their own local or semi-production-ready coding agent harness in Python without hiding the important details behind a framework.

You should already be comfortable with:

- Python 3.11+
- async and `asyncio`
- dataclasses and typing
- JSON and file I/O
- basic API client patterns

## Design Principles

The chapter flow follows a few rules extracted from both tutorial sets:

1. Start from the control loop, not the tooling ecosystem.
2. Keep the runtime typed so behavior remains inspectable and testable.
3. Treat permissions and safety as executable boundaries, not prompt wording.
4. Add extensibility only after the core loop is stable.
5. Introduce advanced features only when the previous layer has a clear reason to exist.

## Build Outcome

If you work through these chapters in order, you should end with a Python harness that can:

- run a REPL and execute a structured agent loop
- stream responses while keeping tool execution explicit
- call local tools with permission checks
- persist and resume sessions
- manage long context safely
- store durable memory in readable files
- load skills and plugins
- integrate external tools through MCP
- switch between plan, default, and auto execution modes
- support delegation patterns for multi-agent workflows
- produce logs, traces, and tests suitable for real iteration

## Directory Structure

The material is split into four stages so the difficulty rises slowly.

### 01 Foundations

- `00-sync-to-async-primer.md` ← **start here if async/await is new to you**
- `01-agent-mental-model-and-scope.md`
- `02-typed-loop-and-runtime-models.md`
- `03-tools-streaming-and-first-io.md`

### 02 Runtime And Safety

- `04-session-state-context-and-memory.md`
- `05-permissions-hooks-and-execution-modes.md`
- `06-configuration-testing-and-observability.md`

### 03 Extensions And Scale

- `07-mcp-plugins-and-sandboxing.md`
- `08-delegation-mailboxes-and-coordination.md`

### 04 Production Blueprint

- `09-production-rollout-blueprint.md`

### 05 Advanced Continuity

These chapters intentionally start at `17` to preserve continuity with the source OpenAI tutorial numbering and to make it obvious that they are gap-closure additions rather than replacements for the core sequence above.

- `17-workspace-learning-context-compaction-and-user-profiles.md`
- `18-advanced-observability-telemetry-and-cost-control.md`
- `19-skills-dangerous-actions-and-runtime-hardening.md`
- `20-provider-boundaries-capstone-and-operational-readiness.md`
- `21-config-system-local-and-global.md` — two-tier TOML config, all parameters, merge order, env overrides
- `22-slash-commands-and-repl-control.md` — REPL slash commands for config, skills, sessions, tools, memory
- `23-headless-mode-and-cli-flags.md` — full CLI flags, headless runner, stdin/file prompts, CI usage, exit codes

## Recommended Chapter Rhythm

Each chapter is written as an action plan instead of a theory note. Follow the same rhythm in every chapter:

1. Read the objective and architecture notes.
2. Build only the code for that chapter.
3. Run the validation checklist before moving forward.
4. Write down what changed in your harness design.
5. Only then continue to the next chapter.

## Suggested Final Project Layout

By the end of the series, your Python project can look like this:

```text
agent_harness/
  __init__.py
  app.py            ← main entry point; branches interactive vs headless
  models.py
  prompts.py
  cli/
    args.py         ← argparse definitions and args_to_config_overrides()
    headless.py     ← run_headless() runner and output writer
    input.py        ← resolve_prompt() for --prompt, --prompt-file, --stdin
  config/
    loader.py       ← AgentConfig, load_config(), ensure_config_dirs()
  runtime/
    agent.py
    context_builder.py
    execution_modes.py
    hooks.py
    permissions.py
    repl_state.py   ← ReplState (live harness state passed to slash commands)
    sessions.py
    slash_commands.py  ← SlashCommandRouter and all command handlers
    streaming.py
  memory/
    store.py
    workspace.py
    profiles.py
  tools/
    base.py
    builtin.py
    registry.py
    sandbox.py
  skills/
    registry.py     ← SkillRegistry and load_skills()
  integrations/
    fake_model.py
    openai_client.py
    mcp.py
    plugins.py
  multi_agent/
    coordinator.py
    mailbox.py
    worker.py
  observability/
    logging.py
    metrics.py
  tests/
    test_agent_loop.py
    test_config.py
    test_headless.py
    test_permissions.py
    test_sessions.py
    test_slash_commands.py
    test_tools.py
pyproject.toml      ← registers [project.scripts] agent = "agent_harness.app:main"
```

## Implementation Strategy

This action plan deliberately adopts the best improvements highlighted in `openai-code-tutorial`:

- build with typed dataclasses from the start
- support streaming as a first-class interface
- separate approval from clarification
- add explicit execution modes
- keep file-based memory readable and debuggable
- prefer dynamic context construction over giant static prompts
- include testing and observability before calling the harness complete
- design for MCP and plugin extension rather than bolting them on later
- preserve a provider-neutral runtime boundary and keep vendor wire formats inside adapters
- treat workspace knowledge and user profile learning as post-session updates, not per-turn side effects
- add explicit telemetry and cost visibility before claiming the system is operationally ready

At the same time, it keeps the architectural rigor emphasized in `agentic-framework-tutorial`:

- treat the system as a control loop, not a chat wrapper
- keep tool scheduling and permissions explicit
- think in layers: UI, intelligence, and tools
- preserve inspectability as the system grows

## How To Use This Folder

Read the chapters in order. Do not skip ahead to delegation, plugins, or sandboxing before the basic runtime is working. Most harnesses become messy because advanced features are introduced before the state model, permission boundaries, and testing discipline are stable.

If you want the smallest possible milestone, stop after Chapter 5. That gives you a useful minimal harness. Chapters 6 through 9 turn it into a maintainable and extensible system.

If you want broader parity with the attached OpenAI tutorial materials, continue through Chapters 17 through 20. Those chapters cover the most important advanced concepts that were still not represented strongly enough in the first action-plan pass.