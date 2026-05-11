# 05 — Extensions, Delegation, And Production-Flows

This chapter covers the higher-variability features that are powerful but environment-dependent: MCP, skills, plugins, delegation, sandboxing, and production-style operational checks.

Treat each section as either:

- **required**, if your environment is configured for it
- **optional**, if the dependency is not available locally

---

## Objective

By the end of this chapter, you should know whether advanced runtime features:

- are discoverable from the CLI and REPL
- degrade gracefully when unavailable
- expose actionable status to the user
- preserve trust and inspectability instead of feeling magical

---

## Prerequisites

Complete Chapters 01 through 04 first.

Use a fresh workspace:

```bash
mkdir -p /tmp/nexus-manual-tests
rm -rf /tmp/nexus-manual-tests/advanced-workspace
mkdir -p /tmp/nexus-manual-tests/advanced-workspace
cd /tmp/nexus-manual-tests/advanced-workspace
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus init
```

---

## Scenario 1 — MCP status without any configured servers

Start REPL:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus
```

Inside:

```text
/mcp status
/mcp tools
/quit
```

### Expected result

- if no MCP servers are configured, the message should say so clearly
- the REPL should stay usable

### Gap checklist

- does the runtime explain how to configure MCP next?
- is “no MCP servers configured” enough guidance?

---

## Scenario 2 — Skill visibility and activation

Start REPL again:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus
```

Inside:

```text
/skills list
```

If skills exist in the workspace or global skills directory, continue with:

```text
/skills show <skill-name>
/skills add <skill-name>
/skills show <skill-name>
/skills remove <skill-name>
/skills reload
/quit
```

### Expected result

- skills should be listable
- adding/removing a skill should clearly update session state
- reload should not crash the REPL

### If no skills exist

Mark this scenario as **not applicable** and note whether the empty-state UX is still understandable.

---

## Scenario 3 — Delegation disabled path

Start the REPL:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus
```

Inside:

```text
/delegate status
/delegate workers
/delegate tasks
/delegate tasks active
/quit
```

### Expected result

If delegation is not enabled:

- each command should fail gracefully with a clear message
- the message should tell you how to enable delegation

---

## Scenario 4 — Delegation enabled path

Only run this if you want to validate worker coordination.

Create a local config override:

```bash
python - <<'PY'
from pathlib import Path
path = Path('.nexus/config.toml')
existing = path.read_text(encoding='utf-8') if path.exists() else ''
path.write_text(existing + '\ndelegation_enabled = true\n', encoding='utf-8')
PY
```

Start REPL:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus
```

Inside:

```text
/delegate status
/delegate workers
/delegate tasks
/delegate tasks active
/delegate spawn "Review docs" "Summarize the workspace purpose." --worker worker-1
/delegate spawn "Review docs with controls" "Summarize the workspace purpose and note required follow-ups." --worker worker-1 --tool write_note --resource docs-readme --permission-action write_note --permission-reason "Need approval before writing a summary note"
/delegate tasks
/delegate messages
/delegate messages worker-1 10
/delegate approvals
```

### Expected result

- status should indicate delegation is enabled
- workers should be listed
- spawned work should produce visible task state
- approvals/messages should be inspectable

### Optional approval branch

If a worker requests permission:

```text
/delegate approvals
/delegate approve <decision-id>
```

or

```text
/delegate reject <decision-id>
```

### Gap checklist

- is worker activity understandable from the terminal alone?
- can a user reason about who asked for permission and why?

---

## Scenario 5 — MCP refresh flow with configured servers

Only run this if you have MCP configured.

Inside REPL:

```text
/mcp status
/mcp refresh
/mcp tools
/mcp refresh <server-name>
```

### Expected result

- refresh should not crash the session
- status should reflect availability or error state clearly
- if tools are discovered, they should be listed in a usable format

---

## Scenario 6 — Plugin loading behavior

This is best treated as an observation test.
Run:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "hello" --quiet
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "hello" --quiet --no-plugins
```

### Expected result

- both commands should remain stable
- disabling plugins should not break core built-in behavior

### Manual follow-up

If you have local plugins, compare:

- `/tools` output with plugins enabled
- `/tools` output with `--no-plugins`

---

## Scenario 7 — No-skills path

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "hello" --no-skills
```

### Expected result

- command should still work
- disabling skills should not break the core experience

### Gap checklist

- does the runtime communicate clearly what was disabled?

---

## Scenario 8 — Sandboxed command tool availability

This is environment-dependent.
First, run doctor and observe Docker-related warnings:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus doctor
```

If Docker is installed and the sandbox image is built, also inspect tool availability from REPL:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus
```

Inside:

```text
/tools
/quit
```

### Expected result

- if Docker is unavailable, the system should warn clearly rather than crash
- if sandbox tooling is available, it should appear explicitly in tool listings

### Gap checklist

- are sandbox prerequisites documented clearly enough from runtime messages alone?

---

## Scenario 9 — Production-style logging and observability sanity check

If JSON logging is enabled in config, run a headless prompt and inspect log outputs.

Example local override:

```bash
python - <<'PY'
from pathlib import Path
path = Path('.nexus/config.toml')
existing = path.read_text(encoding='utf-8') if path.exists() else ''
if 'log_format = "json"' not in existing:
    path.write_text(existing + '\nlog_format = "json"\n', encoding='utf-8')
PY
```

Then run:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "hello from observability" --quiet
find .nexus -maxdepth 3 -type f | sort
find ~/.nexus -maxdepth 3 -type f 2>/dev/null | sort | head
```

### Expected result

- expected runtime artifacts such as audit trail or logs should be created if configured
- files should be human-inspectable enough for debugging

### Manual checks

- do logs contain enough correlation details to troubleshoot a run?
- are sensitive values accidentally echoed?

---

## Scenario 10 — Full smoke pass matrix

Run this short matrix after all advanced setup work:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus version
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus doctor --output-format json | python -m json.tool >/dev/null
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "hello" --quiet
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "hello" --quiet --no-plugins
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "hello" --quiet --no-skills
```

### Expected result

- all commands should complete cleanly
- no obvious regression should remain in the core CLI experience

---

## Final validation checklist

- [ ] empty-state UX for MCP is readable
- [ ] skills can be inspected and optionally activated
- [ ] delegation disabled path is clear
- [ ] delegation enabled path is inspectable from the terminal
- [ ] plugin/no-plugin behavior is stable
- [ ] skill/no-skill behavior is stable
- [ ] sandbox availability degrades gracefully
- [ ] observability artifacts are usable when enabled
- [ ] final smoke matrix passes

---

## Recommended output for a manual test report

When you finish all five chapters, summarize findings under these headings:

1. **critical runtime bugs**
2. **safety or confirmation concerns**
3. **documentation mismatches**
4. **UX friction and confusing wording**
5. **environment-dependent setup issues**
6. **feature gaps or missing commands**

That report will be much more useful than a raw terminal transcript because it distinguishes implementation bugs from documentation or onboarding gaps.



