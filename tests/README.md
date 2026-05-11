# Nexus Manual CLI Test Suite

This folder now contains a **terminal-first, UI-level manual test plan** for validating the Nexus agent outside of `pytest`.

The goal is different from the existing Python tests:

- the `test_*.py` files verify internal behavior at code level
- the markdown files below verify **observable user behavior** from the CLI and REPL
- each chapter is written so you can follow it step by step and record gaps, regressions, or UX issues

---

## Reading order

Follow these documents in order:

1. [`01-workspace-bootstrap-and-health.md`](./01-workspace-bootstrap-and-health.md)
2. [`02-headless-cli-and-one-shot-flows.md`](./02-headless-cli-and-one-shot-flows.md)
3. [`03-interactive-repl-and-slash-commands.md`](./03-interactive-repl-and-slash-commands.md)
4. [`04-safety-sessions-memory-and-state.md`](./04-safety-sessions-memory-and-state.md)
5. [`05-extensions-delegation-and-production-flows.md`](./05-extensions-delegation-and-production-flows.md)
6. [`06-full-interactive-command-matrix.md`](./06-full-interactive-command-matrix.md)

Each chapter builds on the previous one. If you skip the setup and health checks, later failures become harder to interpret.

---

## What these docs cover

These guides focus on the user-visible Nexus surface:

- install and bootstrap behavior
- config resolution and doctor output
- one-shot headless execution
- inline confirmation behavior in interactive terminals
- structured output modes (`text`, `json`, `jsonl`)
- session persistence and resumability
- REPL slash commands
- safety boundaries around mutating tools
- memory and context inspection
- optional extension paths such as MCP, delegation, skills, and sandboxing

They are intended to help you find:

- CLI regressions
- confusing prompts or missing UX affordances
- provider-specific surprises
- broken assumptions around TTY vs non-TTY behavior
- file-system side effects that differ from the user’s expectations
- gaps between the documentation and the actual runtime

---

## Conventions used in the manual tests

### 1. Run from a dedicated scratch workspace

Many scenarios intentionally write files, create sessions, or update `.nexus/` state.
Use a throwaway directory whenever possible.

### 2. Capture both stdout and stderr

A lot of Nexus behavior is visible only through terminal output.
When testing a new scenario, prefer using `tee` so you can inspect what happened later.

Example:

```bash
uv run nexus --prompt "what time is it" 2>&1 | tee /tmp/nexus-run.log
```

### 3. Record the exit code explicitly

Some important flows are differentiated by exit code rather than text alone.

```bash
uv run nexus --prompt "write a note"; echo "exit=$?"
```

### 4. Separate required vs optional scenarios

Some chapters include optional sections for:

- Mistral or OpenAI-compatible providers
- Docker sandboxing
- MCP servers
- delegation

If your environment is missing the dependency, mark the scenario as **not applicable** instead of failed.

### 5. Log observed gaps

For each scenario, record:

- what you ran
- what you expected
- what actually happened
- whether the issue is a runtime bug, doc mismatch, or UX gap

A simple template:

```markdown
### Observation
- Scenario:
- Command:
- Expected:
- Actual:
- Severity: low | medium | high
- Notes:
```

---

## Suggested execution rhythm

For each chapter:

1. complete the prerequisites
2. run the commands exactly as written
3. compare the observed behavior with the expected behavior
4. note any mismatch before continuing
5. only then move to the next chapter

---

## Fastest useful path

If you only want the minimum high-value manual pass, run:

- Chapter 01 through Chapter 04

If you want the broadest runtime validation, run all six chapters.

The final chapter is a compact command-surface audit that is especially useful before release or after changing slash-command behavior.

---

## Relationship to the automated test suite

Use both layers together:

- run `pytest` when changing implementation details
- run these markdown scenarios when validating real CLI behavior and end-to-end UX

Recommended pairing:

```bash
uv run --group dev python -m pytest -q
```

Then run the manual docs in order.

