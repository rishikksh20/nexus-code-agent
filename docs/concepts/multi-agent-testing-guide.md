# Cognitive Sub-Agent Testing Guide

Last updated: 2026-05-18

This guide validates the current advanced-mode model: one supervisor turn loop with optional cognitive `subagent_*` tools. Nexus no longer has a separate worker runtime, mailbox command surface, or automatic planning scheduler.

## 1. Automated Regression Tests

Run the full suite:

```bash
uv run pytest
```

Focused advanced-mode tests:

```bash
uv run pytest tests/test_orchestration.py tests/test_tools.py -k subagent
uv run pytest tests/test_slash_commands.py -k "skills or context"
```

Expected result: all tests pass. These tests cover the shared turn runner, sub-agent tool registration, skill-backed sub-agents, structured tool behavior, and `/context` visibility.

## 2. Fake Provider Smoke Test

Use fake provider mode to verify wiring without spending tokens:

```bash
uv run nexus --provider fake --no-session "Inspect the repo and summarize the approval flow"
```

To expose built-in cognitive tools, set local config:

```toml
agent_mode = "advanced"
approval_policy = "on-request"
```

Then run:

```bash
uv run nexus --provider fake --no-session "Use a read-only sub-agent to inspect the repo, then summarize the result"
```

Expected behavior:

- Execution still uses the normal approval-safe turn runner.
- Built-in `subagent_planning_analysis`, `subagent_execution`, `subagent_review`, and `subagent_verification` tools are registered when allowed by filters.
- Sub-agent results return as structured tool output to the supervisor.
- Mutating tools still ask for approval through the normal approval flow.

## 3. Interactive Visibility

Start the REPL:

```bash
uv run nexus --provider fake
```

Useful commands:

```text
/tools
/skills
/skills reload
/context agents
/context agent supervisor
/context usage supervisor
```

Expected behavior:

- `/tools` shows the active tool surface.
- Advanced mode shows `subagent_*` tools when allowed by `allowed_tools` and `denied_tools`.
- `/skills reload` registers local or global skills named `subagent-*` as additional cognitive tools.
- `/context agents`, `/context agent <id>`, and `/context usage <id>` show compact context snapshots when sub-agent state exists.

## 4. Structured Tool Checks

Run individual structured tools through the agent or focused tests:

```bash
uv run pytest tests/test_structured_tools.py
```

Manual prompts:

```text
Use git_status and summarize the working tree.
Use git_diff to review the current working changes.
Run run_python_check and summarize the result.
```

Expected behavior:

- `git_status` returns branch and changed-file metadata.
- `git_diff` returns a working/staged diff or `(no diff)`.
- `run_python_check` returns JSON with `passed`, `exit_code`, stdout tail, and stderr tail.

## 5. Real Provider Trial

After fake-provider smoke tests pass, configure a live provider and keep approvals visible:

```toml
provider = "openai-compatible"
agent_mode = "advanced"
approval_policy = "on-request"
```

Use a read-heavy prompt first:

```text
Analyze the runtime approval flow and review whether advanced mode changes the approval invariant. Do not edit files.
```

Then try a small edit:

```text
Update one docs paragraph about advanced-mode sub-agent testing, then run verification and review the diff.
```

Expected behavior:

- Mutating tools still ask for approval.
- Sub-agent tool calls do not bypass `run_agent_turn()`.
- Context summaries from sub-agent work remain compact and inspectable.

## 6. Failure Cases To Check

Advanced mode disabled:

- Set `agent_mode = "basic"`.
- Expected: the supervisor still works, and specialist sub-agent tools are not registered.

Verification failure:

- Introduce a temporary syntax error in a throwaway branch.
- Ask the agent to run `run_python_check`.
- Expected: `run_python_check` reports the failure, and the supervisor explains the blocker rather than running hidden mutation loops.

## Current Boundary

Advanced mode is a tool-registration profile, not a separate orchestration runtime. Repairs should remain user-visible and approval-safe.
