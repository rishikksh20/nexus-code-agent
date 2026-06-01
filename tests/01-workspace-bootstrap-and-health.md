# 01 — Workspace Bootstrap And Health

Start here. This chapter verifies that Nexus installs, initializes its workspace state, resolves config, and reports health correctly from the terminal. It also covers sending your first query to the agent (headless and interactive) and working with the `/provider` session command.

---

## Objective

By the end of this chapter, you should know whether:

- the CLI is callable from `uv run nexus`
- workspace initialization is creating the expected `.nexus/` structure
- config loading works from the terminal
- `doctor` gives usable output in both text and JSON modes
- startup failures are understandable when configuration is invalid
- a normal query can be sent to the agent in both headless and interactive modes
- the `/provider` slash command shows the active model provider and allows live parameter updates

---

## Prerequisites

- `uv` installed
- project dependencies installed
- terminal opened in the project root

From the repo root:

```bash
cd /Users/rishikeshrishikesh/dev/exp/build-an-ai-agent
uv sync --group dev
```

---

## Test workspace setup

Create a clean scratch workspace so the results are easy to inspect.

```bash
mkdir -p /tmp/nexus-manual-tests
rm -rf /tmp/nexus-manual-tests/bootstrap-workspace
mkdir -p /tmp/nexus-manual-tests/bootstrap-workspace
cd /tmp/nexus-manual-tests/bootstrap-workspace
```

Define a shorthand for the project directory to keep commands readable:

```bash
NEXUS_DIR=/Users/rishikeshrishikesh/dev/exp/build-an-ai-agent
```

Verify the CLI is reachable:

```bash
uv --directory "$NEXUS_DIR" run nexus version
```

### Expected result

- the command prints a version string
- exit code is `0`

---

## Scenario 1 — Initialize a fresh workspace

```bash
uv --directory "$NEXUS_DIR" run nexus init
find . -maxdepth 3 | sort
```

### Expected result

- `.nexus/` is created in the current workspace
- `.agents/skills/` contains workspace-readable copies of packaged skills
- you should see files and folders such as:
  - `.nexus/config.toml`
  - `.nexus/knowledge.md`
  - `.nexus/memory/`
  - `.nexus/sessions/`
- the command should either print `Created:` items or report that the workspace is already initialized

### Failure signals

- missing `.nexus/config.toml`
- missing `.nexus/knowledge.md`
- stack trace instead of a clear message

---

## Scenario 2 — Re-run initialization for idempotency

```bash
uv --directory "$NEXUS_DIR" run nexus init
```

### Expected result

- no duplicate errors
- user-facing output should clearly indicate the workspace already exists or that nothing harmful happened

### What to note

- does the message feel safe and obvious?
- does it imply destructive overwrite even though none occurred?

---

## Scenario 3 — Inspect merged config from the CLI

```bash
uv --directory "$NEXUS_DIR" run nexus config
```

### Expected result

- valid JSON printed to stdout
- includes important fields such as:
  - `provider`
  - `model_name`
  - `workspace_root`
  - `local_root`
  - `global_root`

### Manual checks

- confirm `workspace_root` points to `/tmp/nexus-manual-tests/bootstrap-workspace`
- confirm `.nexus`-scoped paths live under that workspace

---

## Scenario 4 — Inspect local and global config separately

```bash
uv --directory "$NEXUS_DIR" run nexus config show local
uv --directory "$NEXUS_DIR" run nexus config show global
```

### Expected result

- local config should be readable even if minimal
- global config should either print a file or a clear “not initialized” message

### Gap checklist

- is the difference between local, global, and merged clear enough?
- would a new user understand where to edit provider settings?

---

## Scenario 5 — Run doctor in text mode

```bash
uv --directory "$NEXUS_DIR" run nexus doctor
```

### Expected result

- a readable health report
- explicit pass/warn style sections
- no Python traceback on a healthy local setup

### What to observe

- are warnings actionable?
- if Docker or MCP is unavailable, is the wording understandable?
- does the report explain what is optional versus required?

---

## Scenario 6 — Run doctor in JSON mode

```bash
uv --directory "$NEXUS_DIR" run nexus doctor --output-format json | tee /tmp/nexus-manual-tests/doctor.json
python3 -m json.tool /tmp/nexus-manual-tests/doctor.json >/dev/null && echo "json-ok"
```

### Expected result

- valid JSON is emitted
- the JSON can be parsed without editing
- useful top-level fields should exist, such as overall status and gates

### Gap checklist

- would this JSON be easy to consume from CI?
- are field names obvious?

---

## Scenario 7 — Validate error handling for bad provider config

Create an intentionally broken local config:

```bash
cat > .nexus/config.toml <<'EOF'
provider = "invalid-provider"
EOF
```

Now run:

```bash
uv --directory "$NEXUS_DIR" run nexus --prompt "hello"; echo "exit=$?"
```

### Expected result

- Nexus should stop early
- it should print a clear configuration error
- exit code should be non-zero
- it should not produce a traceback for a simple user config mistake

### Cleanup

Restore a sane config by reinitializing the workspace:

```bash
rm -rf .nexus
uv --directory "$NEXUS_DIR" run nexus init
```

---

## Scenario 8 — Check CLI help discoverability

```bash
uv --directory "$NEXUS_DIR" run nexus --help
uv --directory "$NEXUS_DIR" run nexus doctor --help
uv --directory "$NEXUS_DIR" run nexus config --help
```

### Expected result

- help text should mention major flags and subcommands
- mutually exclusive prompt inputs should be understandable
- advanced flags should not hide the basic workflow

### UX questions

- can a first-time user infer the difference between REPL and headless mode?
- does `--auto-confirm` feel sufficiently risky in wording?

---

## Scenario 9 — Send a normal query to the agent (headless mode)

The `fake` provider is the default. It executes queries locally without any API key.

```bash
uv --directory "$NEXUS_DIR" run nexus --prompt "What time is it?" --auto-confirm
echo "exit=$?"
```

### Expected result

- the agent runs one turn
- it invokes `get_time` and returns the current UTC time
- exit code is `0`
- no traceback

### Headless JSON output

Capture the structured response for further inspection:

```bash
uv --directory "$NEXUS_DIR" run nexus \
  --prompt "What time is it?" \
  --output-format json \
  --auto-confirm \
  --output-file /tmp/nexus-manual-tests/response.json
python3 -m json.tool /tmp/nexus-manual-tests/response.json >/dev/null && echo "json-ok"
```

### Expected result

- `/tmp/nexus-manual-tests/response.json` contains valid JSON
- the response includes a turn with tool usage and a natural language answer

### What to observe

- does the agent maintain coherent conversation history within one headless invocation?
- if the prompt produces a `confirmation_required` exit (code 3), re-run with `--auto-confirm`

---

## Scenario 10 — Use the /provider command in an interactive session

Start an interactive REPL session:

```bash
uv --directory "$NEXUS_DIR" run nexus
```

Inside the REPL, type the following commands one at a time and observe the output.

### 10a — View the current provider status

```
/provider
```

**Expected output:** a table showing `provider`, `model_name`, `api_base_url`, `temperature`, `max_output_tokens`, `max_loop_iterations`, `stream_output`, `show_tool_calls` with their current values.

**Checks:**
- `provider` should be `fake` on a fresh workspace
- `model_name` should be `fake-model`
- `temperature` should be `0.0`

### 10b — List all available providers

```
/provider list
```

**Expected output:** a table listing `fake`, `openai`, and `openai-compatible`, each with a description and whether it is currently active.

**Checks:**
- `fake` row should show `yes` in the Active column
- `openai` and `openai-compatible` rows should show `no`

### 10c — Update the model name for this session

```
/provider set model_name gpt-4o
```

**Expected output:**

```
Updated model_name = 'gpt-4o'
```

Verify it persisted:

```
/provider
```

The `model_name` row should now show `gpt-4o`.

### 10d — Update temperature

```
/provider set temperature 0.7
```

**Expected output:**

```
Updated temperature = 0.7
```

Run `/provider` again to confirm.

### 10e — Attempt to set a restricted parameter

```
/provider set sandbox_image evil:latest
```

**Expected output:** an error message stating the parameter is unknown or restricted. The `sandbox_image` setting must not change.

### 10f — Send a normal query inside the REPL

```
What time is it?
```

**Expected:**
- the agent thinks and then calls `get_time`
- you see the tool result and a natural language response with the current UTC time
- if a confirmation prompt appears, type `y` to approve

### 10g — Exit cleanly

```
/quit
```

**Expected:** session is saved and the REPL exits without a traceback.

---

## Scenario 11 — Reload provider config without restarting

If you edit `.nexus/config.toml` manually while inside the REPL, reload it with:

```
/config reload
```

Or use `/provider set` to make targeted changes directly. Changes written via `/provider set` go to `.nexus/config.toml` atomically and are immediately reloaded into the running session.

**Settable parameters via `/provider set`:**

| Parameter | Purpose |
|---|---|
| `provider` | Switch between `fake`, `openai`, `openai-compatible` |
| `model_name` | The model identifier sent to the provider |
| `api_base_url` | Endpoint URL for live providers |
| `temperature` | Sampling temperature (0.0–2.0) |
| `max_output_tokens` | Maximum tokens per response |
| `max_loop_iterations` | Maximum agent turns per query |
| `stream_output` | Enable/disable streaming output |
| `show_tool_calls` | Show or hide tool call output |

Parameters outside this list must be edited in `.nexus/config.toml` directly.

---

## Validation checklist before moving on

Mark this chapter complete only if all are true:

- [ ] `nexus version` works
- [ ] `nexus init` creates expected local state
- [ ] `nexus config` is readable and accurate
- [ ] `nexus doctor` works in text mode
- [ ] `nexus doctor --output-format json` emits valid JSON
- [ ] bad config fails early with a user-readable error
- [ ] CLI help is understandable enough for a new user
- [ ] headless `--prompt` query completes and exits cleanly
- [ ] headless `--output-format json` produces valid JSON output
- [ ] `/provider` shows the current provider configuration in a table
- [ ] `/provider list` lists all three providers with active flag
- [ ] `/provider set model_name` updates the in-session config
- [ ] `/provider set` rejects non-allowlisted parameters
- [ ] a normal REPL query reaches the agent and produces a response

---

## Suggested notes to carry into Chapter 02

Document anything unclear about:

- config precedence
- workspace root handling
- doctor report wording
- install/setup friction
- missing onboarding guidance
- whether the `/provider` allowlist covers the parameters you commonly need to change
