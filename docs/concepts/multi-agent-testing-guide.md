# Multi-Agent Testing Guide

Last updated: 2026-05-15

This guide splits multi-agent validation into safe layers. Start with the automated tests, then run the fake-provider smoke test, then try a real provider only after the first two pass.

## 1. Automated Regression Tests

Run the full suite:

```bash
uv run pytest
```

Focused multi-agent tests:

```bash
uv run pytest tests/test_orchestration.py tests/test_structured_tools.py
uv run pytest tests/test_slash_commands.py -k multi_agent
```

Expected result: all tests pass. These tests cover DAG validation, post-execution checks, repair decisions, structured tools, and `/multi-agent` visibility.

## 2. Fake Provider Smoke Test

Use fake provider mode to verify wiring without spending tokens:

```bash
uv run nexus --provider fake --no-session "Implement this plan: update docs and review the diff"
```

To force the supervisor/planner path, set local config:

```toml
multi_agent_mode = "always"
multi_agent_show_plan = true
delegation_enabled = true
```

Then run:

```bash
uv run nexus --provider fake --no-session "Implement this plan: inspect the repo, update docs, verify, and review"
```

Expected behavior:

- Nexus prints a multi-agent plan.
- Execution still uses the normal approval-safe turn runner.
- Session metadata records `multi_agent.shared_state`.
- No mutating repair loop runs invisibly.

## 3. Interactive Visibility

Start the REPL:

```bash
uv run nexus --provider fake
```

Useful commands:

```text
/multi-agent status
/multi-agent plan
/multi-agent state
/context agents
/context agent supervisor
/context usage supervisor
/delegate status
/delegate workers
/tools
```

Expected behavior:

- `/multi-agent status` shows mode, threshold, delegation state, latest complexity, and repair decision.
- `/multi-agent plan` shows the latest task DAG if one was recorded.
- `/multi-agent state` prints the raw shared-state JSON.
- `/context agents` lists isolated and shared context snapshots for supervisor/planner/execution/test-review/workers.
- `/context agent <id>` shows one agent's context snapshot, including scope and handoff inputs.
- `/context usage <id>` shows estimated token usage for one agent.
- `/delegate` remains the low-level worker/mailbox inspection surface.

## 3.1 Context Isolation And Handoff Check

Use an explicit delegated worker with shared handoff context:

```text
/delegate spawn "Verification slice" "Inspect the changed files and summarize risk." --tool git_status --context "Execution changed nexus/runtime/orchestration.py"
```

Then inspect:

```text
/delegate tasks
/context agents
/context agent worker-worker-1-<task_id>
```

Expected behavior:

- The worker context snapshot has `scope = "isolated"`.
- `shared_inputs` contains only the handoff text, not the full supervisor or execution history.
- `allowed_tools` contains only the worker allowlist.
- Tool calls execute with the worker's restricted registry.

## 4. Structured Tool Checks

Run individual structured tools through the agent or focused tests:

```bash
uv run pytest tests/test_structured_tools.py
```

Manual prompts:

```text
Use git_status and summarize the working tree.
Use git_diff to review the current working changes.
Run run_typecheck and summarize the result.
```

Expected behavior:

- `git_status` returns branch and changed-file metadata.
- `git_diff` returns a working/staged diff or `(no diff)`.
- `run_typecheck` returns JSON with `passed`, `exit_code`, stdout tail, and stderr tail.

## 5. Real Provider Trial

After fake-provider smoke tests pass, configure a live provider and keep approvals visible:

```toml
provider = "openai-compatible"
multi_agent_mode = "auto"
multi_agent_show_plan = true
delegation_enabled = true
approval_policy = "on-request"
```

Use a read-heavy prompt first:

```text
Analyze the runtime approval flow, produce a plan, and review whether multi-agent mode changes the approval invariant. Do not edit files.
```

Then try a small edit:

```text
Update one docs paragraph about multi-agent testing, then run verification and review the diff.
```

Expected behavior:

- Mutating tools still ask for approval.
- Post-execution checks run after the normal turn.
- Review findings and repair decisions are stored in `/multi-agent state`.
- Latest repair/review carry-over appears in later turns through the normal context builder.

## 6. Failure Cases To Check

Planner JSON failure:

- Use a fake/scripted planner test or `tests/test_orchestration.py::test_parse_task_dag_rejects_invalid_json`.
- Expected: validation fails before execution.

Verification failure:

- Introduce a temporary syntax error in a throwaway branch.
- Run multi-agent mode.
- Expected: `run_typecheck` reports failure and `/multi-agent status` shows repair needed.

Delegation disabled:

- Set `delegation_enabled = false`.
- Expected: supervisor still works, specialist subagent tools are simply unavailable, and `/delegate status` explains how to enable delegation.

## Current Boundary

The current implementation records repair decisions but does not automatically run hidden mutating repair turns. That is intentional. Repairs should remain user-visible and approval-safe until the retry loop has more production history.
