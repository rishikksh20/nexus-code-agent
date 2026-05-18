# 06 — Full Interactive Command Matrix

This final chapter is a **compact command-by-command audit** of the interactive Nexus REPL surface.

Unlike Chapters 03 through 05, which walk through realistic user flows, this chapter is meant to answer a simpler question:

> Can I manually exercise every interactive command and key subcommand from the terminal, and does each one behave in a user-readable way?

Use it as the final pass after the earlier narrative chapters are complete.

---

## Objective

By the end of this chapter, you should know whether the complete REPL command surface:

- is reachable from the terminal
- behaves consistently with its visible help text
- handles empty-state and success-state cases correctly
- fails clearly when a dependency is unavailable
- has any doc/runtime mismatch that should be fixed separately

---

## Source of truth

This matrix is based on the actual slash-command router in `nexus/runtime/slash_commands.py`.

That means this chapter audits the commands the runtime really accepts, even if some help text or README snippets are slightly out of sync.

---

## Prerequisites

Complete Chapters 01 through 05 first.

Use a fresh workspace:

```bash
mkdir -p /tmp/nexus-manual-tests
rm -rf /tmp/nexus-manual-tests/interactive-matrix-workspace
mkdir -p /tmp/nexus-manual-tests/interactive-matrix-workspace
cd /tmp/nexus-manual-tests/interactive-matrix-workspace
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus init
```

Start the REPL:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus
```

Commands shown below in `text` blocks should be typed inside the REPL.

---

## How to use this matrix

For each command row below:

1. run the example exactly as shown
2. verify the expected visible behavior
3. mark one of:
   - pass
   - fail
   - not applicable
4. record any mismatch between:
   - runtime behavior
   - slash-command help text
   - `README.md`
   - your user expectation

---

## A. Always-available core commands

### `/help`

```text
/help
```

Expected:

- prints a readable slash-command help table
- should not exit the REPL

Notes to record:

- does the table include enough subcommand guidance?
- does it omit any real command aliases such as `/exit`?

---

### `/mode`

```text
/mode
/mode plan
/mode
/mode default
/mode auto
/mode
```

Expected:

- show current mode
- switch modes with explicit confirmation text
- keep REPL alive

Negative check:

```text
/mode nonsense
```

Expected:

- clear error
- no crash

---

### `/tools`

```text
/tools
```

Expected:

- prints registered tools
- shows mutating vs non-mutating status
- remains readable in a narrow terminal

---

### `/context`

```text
/context
```

Expected:

- prints the current assembled system prompt
- if no turn has happened yet, output should still be understandable

Audit note:

- if the help text says `/context show` but the runtime accepts `/context`, record that mismatch

---

### `/history`

```text
/history
/history 5
```

Expected:

- prints recent messages
- numeric limit works
- no crash on short histories

---

### `/quit`

```text
/quit
```

Expected:

- exits cleanly
- saves session if persistence is enabled

Because this exits the REPL, restart it before testing `/exit`.

### `/exit`

```text
/exit
```

Expected:

- same effective behavior as `/quit`

Audit note:

- if `/exit` works but is absent from `/help`, record that as a doc/help mismatch

---

## B. Config commands

Restart REPL if needed, then test:

```text
/config show merged
/config show local
/config show global
/config set default_mode plan
/config show merged
/config reset default_mode
/config reload
```

Expected:

- merged config prints JSON-like merged runtime state
- local/global config print raw file contents or a clear empty-state message
- set/reset/reload succeed without leaving the REPL

Negative checks:

```text
/config nonsense
/config set
/config reset
```

Expected:

- clear fallback behavior or readable error
- REPL remains alive

---

## C. Session commands

Run these in one REPL session:

```text
/session
/session save
/session list
/session new
/session
/session save
/session list
```

Expected:

- `/session` reports active session id and message count
- `/session save` persists the active session
- `/session new` changes the active session id
- `/session list` shows multiple sessions after repeated saves

Then resume an existing session from the list:

```text
/session resume <session-id>
/session
```

Then export it:

```text
/session export exported-session.json
```

Expected:

- resume loads the chosen session
- export writes a readable JSON file in the current workspace

Shell-side verification after quitting:

```bash
ls -1 .nexus/sessions
cat .nexus/sessions/latest_session.txt
test -f exported-session.json && echo "export-present"
```

Expected:

- `.nexus/sessions/*.json` files exist
- `latest_session.txt` points to the last saved session
- exported session file exists

---

## D. Skill commands

Inside REPL:

```text
/skills list
```

If skills exist, continue:

```text
/skills show <skill-name>
/skills add <skill-name>
/skills show <skill-name>
/skills remove <skill-name>
/skills reload
```

Expected:

- list works in both empty and non-empty states
- add/remove/reload are readable and stable

If no skills are present:

- mark as `not applicable`
- still record whether the empty-state message is clear enough

Negative checks:

```text
/skills show missing-skill
/skills add missing-skill
```

Expected:

- readable “not found” message
- no crash

---

## E. Memory commands

Inside REPL:

```text
/memory list
/memory save manual-key "Manual matrix validation note"
/memory list
/memory search manual
/memory show manual-key
/memory show missing-key
```

Expected:

- list works in empty and non-empty states
- save persists a memory record
- search returns relevant content
- show works for existing keys
- missing-key path fails clearly

Shell-side verification after quitting:

```bash
find .nexus/memory -maxdepth 2 -type f | sort
grep -R "Manual matrix validation note" .nexus/memory || true
```

Expected:

- memory persists to local files
- content is inspectable without Python internals

---

## F. MCP commands

Inside REPL:

```text
/mcp status
/mcp tools
/mcp refresh
```

If you know a configured server name:

```text
/mcp refresh <server-name>
```

Expected:

- if MCP is not configured, empty-state messaging is clear
- if configured, status/tools/refresh remain stable and readable

Mark as `not applicable` if MCP is intentionally not configured.

---

## G. Advanced Sub-Agent Visibility

### Basic-mode audit

Inside REPL:

```text
/tools
/context agents
```

Expected in basic mode:

- normal single-agent tools are visible
- `subagent_*` tools are absent unless advanced mode is enabled
- context commands show a readable empty or current-state view

### Advanced-mode audit

Only run this if `agent_mode = "advanced"` is enabled in local config.

```text
/tools
/skills
/context agents
/context usage
```

Expected:

- built-in `subagent_*` tools are visible when allowed by tool filters
- skill-backed `subagent-*` skills appear after `/skills reload`
- approvals requested by sub-agent tool calls use the normal approval UI

---

## H. Unknown command and syntax robustness

Unknown command:

```text
/not-a-real-command
```

Expected:

- clear unknown-command message
- REPL remains alive

Broken quoting example:

```text
/config set project_name "unterminated
```

Expected:

- readable syntax error
- REPL remains alive

Empty slash input:

```text
/
```

Expected:

- no crash
- behavior remains controlled and understandable

---

## Final matrix checklist

Mark each item when manually verified:

### Core
- [ ] `/help`
- [ ] `/mode`
- [ ] `/mode plan`
- [ ] `/mode default`
- [ ] `/mode auto`
- [ ] `/tools`
- [ ] `/context`
- [ ] `/history`
- [ ] `/quit`
- [ ] `/exit`

### Config
- [ ] `/config show merged`
- [ ] `/config show local`
- [ ] `/config show global`
- [ ] `/config set <key> <value>`
- [ ] `/config reset <key>`
- [ ] `/config reload`

### Session
- [ ] `/session`
- [ ] `/session save`
- [ ] `/session list`
- [ ] `/session new`
- [ ] `/session resume <id>`
- [ ] `/session export <path>`

### Skills
- [ ] `/skills list`
- [ ] `/skills show <name>`
- [ ] `/skills add <name>`
- [ ] `/skills remove <name>`
- [ ] `/skills reload`

### Memory
- [ ] `/memory list`
- [ ] `/memory search <query>`
- [ ] `/memory save <key> <content>`
- [ ] `/memory show <key>`

### MCP
- [ ] `/mcp status`
- [ ] `/mcp tools`
- [ ] `/mcp refresh`
- [ ] `/mcp refresh <server>`

### Advanced Sub-Agents
- [ ] `agent_mode = "advanced"` exposes built-in `subagent_*` tools
- [ ] `/skills reload` registers skill-backed `subagent-*` tools
- [ ] `/context agents`
- [ ] `/context agent <agent-id>`
- [ ] `/context usage <agent-id>`

### Robustness
- [ ] unknown slash command fails cleanly
- [ ] malformed quoted command fails cleanly
- [ ] empty slash input does not crash the REPL

---

## Recommended final report format

When this final matrix is done, summarize the results using:

```markdown
## Interactive command audit summary

### Passed
- 

### Failed
- 

### Not applicable
- 

### Doc/runtime mismatches
- 

### UX improvements needed
- 
```

That gives you a compact end-state report after running the richer scenario chapters before it.
