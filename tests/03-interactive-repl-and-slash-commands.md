# 03 — Interactive REPL And Slash Commands

This chapter validates the interactive Nexus user experience: entering the REPL, sending prompts, switching modes, and using slash commands as a human operator would.

---

## Objective

By the end of this chapter, you should know whether the REPL:

- starts cleanly
- accepts normal prompts and slash commands
- displays command help clearly
- supports session management from inside the UI
- exposes config, tools, memory, context, and history in usable ways
- exits safely and predictably

---

## Prerequisites

Complete Chapters 01 and 02 first.

Use a fresh workspace:

```bash
mkdir -p /tmp/nexus-manual-tests
rm -rf /tmp/nexus-manual-tests/repl-workspace
mkdir -p /tmp/nexus-manual-tests/repl-workspace
cd /tmp/nexus-manual-tests/repl-workspace
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus init
```

Start the REPL:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus
```

From here onward, commands shown in `text` blocks are typed **inside** the REPL.

---

## Scenario 1 — REPL startup experience

Immediately after startup, observe the banner and prompt.

### Expected result

- a visible startup banner appears
- you see a `>` prompt
- the app does not immediately exit

### UX checklist

- is it obvious that slash commands exist?
- is the startup text too noisy or too minimal?

---

## Scenario 2 — Basic prompt round-trip

Inside the REPL:

```text
what time is it?
```

### Expected result

- Nexus responds without leaving the REPL
- after completion, the `>` prompt returns

### Gap checklist

- does the response render cleanly?
- if tools are shown, is the rendering understandable?

---

## Scenario 3 — Help command discoverability

```text
/help
```

### Expected result

- a readable table or list of slash commands appears
- commands such as `/mode`, `/config`, `/session`, `/tools`, `/memory`, `/history`, `/context`, `/quit`, `/mcp`, `/skills`, and `/provider` should be visible

### UX questions

- is the help output compact enough to scan?
- are subcommands discoverable without opening the code?

---

## Scenario 4 — Mode inspection and switching

```text
/mode
/mode plan
/mode
/mode auto
/mode
/mode default
/mode
```

### Expected result

- the current mode is shown
- switching mode should visibly confirm the new value
- invalid values should fail clearly if you try one

Optional invalid-value check:

```text
/mode nonsense
```

### Expected result

- clear user-facing error
- REPL remains alive

---

## Scenario 5 — Tool registry visibility

```text
/tools
```

### Expected result

- registered tools are listed
- built-in tools such as `get_time` and `write_file` should be visible if enabled

### Gap checklist

- can a user tell which tools are safe vs mutating?
- does the output show source/origin clearly enough?

---

## Scenario 6 — Config inspection from the REPL

```text
/config show merged
/config show local
/config show global
```

### Expected result

- merged config shows the active runtime view
- local/global views show raw config content or a clear not-initialized message

### Optional mutation scenario

```text
/config set default_mode plan
/config show merged
/config reset default_mode
/config reload
```

### Expected result

- local config updates are reflected
- reload is explicit
- reset removes the override cleanly

### UX questions

- does editing config from the REPL feel safe?
- is the distinction between raw TOML and merged config obvious?

---

## Scenario 7 — Session lifecycle from the REPL

```text
/session
/session save
/session list
/session new
/session
/session list
```

If a session id is shown in the list, try:

```text
/session resume <session-id>
```

Then export the active session:

```text
/session export exported-session.json
```

### Expected result

- sessions can be listed and resumed
- starting a new session clears the active conversational context
- save creates a persisted snapshot
- `/session` after `/session new` should show a different session id than before
- export should create a readable JSON file in the current workspace

### Gap checklist

- is the session output readable enough for real use?
- does the user get enough confidence before switching sessions?

---

## Scenario 8 — History inspection

Send a few prompts, then:

```text
/history
/history 5
```

### Expected result

- recent message history is visible
- limiting the number of items should work

### UX questions

- is history readable when tool outputs are involved?
- does it expose too much or too little context?

---

## Scenario 9 — Context inspection

```text
/context
```

### Expected result

- the current assembled system prompt is printed
- it should include the current mode and relevant contextual sections

### Gap checklist

- does the output help debug behavior?
- is it too large or hard to read in the terminal?

---

## Scenario 10 — Memory commands

Run these in order:

```text
/memory list
/memory save "manual-note" "Remember that this workspace is for manual REPL validation."
/memory list
/memory search manual
```

Then inspect the saved key directly:

```text
/memory show manual-note
/memory show missing-note
```

### Expected result

- memory entries can be saved and listed
- searches return relevant entries
- showing a specific entry should display its content
- asking for a missing key should fail cleanly with a user-readable message

### Gap checklist

- is the key handling user-friendly?
- can a new user infer the storage model?

---

## Scenario 11 — Unknown command behavior

```text
/not-a-real-command
```

### Expected result

- a clear “unknown command” style message
- REPL remains alive
- no traceback

---

## Scenario 12 — Broken command syntax handling

Use an unbalanced quote to trigger parse failure:

```text
/config set project_name "broken
```

### Expected result

- syntax error should be reported clearly
- REPL should remain usable

---

## Scenario 13 — Quit flow

This should be tested in two separate REPL runs.

### Run A

```text
/quit
```

### Expected result

- REPL exits cleanly
- session state is saved if persistence is enabled
- terminal returns to the shell without hanging

### Run B — alias test

Start the REPL again, then run:

```text
/exit
```

Expected:

- same behavior as `/quit`

---

## Validation checklist before moving on

- [ ] REPL startup is clear and stable
- [ ] normal prompts round-trip correctly
- [ ] `/help` exposes the command surface
- [ ] `/mode` works for show and switch
- [ ] `/mode auto` also works
- [ ] `/tools` lists registered tools
- [ ] `/config` show/set/reset/reload flows are understandable
- [ ] `/session` show/save/list/new/resume/export commands are usable
- [ ] `/history` and `/context` help debug runtime behavior
- [ ] `/memory` commands work from the terminal
- [ ] unknown/bad commands fail cleanly without crashing the REPL
- [ ] `/quit` and `/exit` terminate cleanly

---

## Suggested notes to carry into Chapter 04

Document anything unclear around:

- command discoverability
- session IDs and memory IDs
- command output readability
- the balance between debug detail and terminal noise
