# 02 — Headless CLI And One-Shot Flows

This chapter tests the one-shot CLI surface that runs with `--prompt`, `--prompt-file`, or `--stdin`.

It is intentionally focused on **terminal-visible behavior**, not on internal Python test helpers.

---

## Objective

By the end of this chapter, you should know whether headless mode:

- accepts prompts from all supported sources
- renders final output correctly in text, JSON, and JSONL modes
- returns meaningful exit codes
- behaves correctly in TTY vs non-TTY confirmation flows
- persists or skips sessions according to flags
- supports provider overrides cleanly

---

## Prerequisites

Complete [`01-workspace-bootstrap-and-health.md`](./01-workspace-bootstrap-and-health.md) first.

Use a fresh workspace:

```bash
mkdir -p /tmp/nexus-manual-tests
rm -rf /tmp/nexus-manual-tests/headless-workspace
mkdir -p /tmp/nexus-manual-tests/headless-workspace
cd /tmp/nexus-manual-tests/headless-workspace
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus init
```

---

## Scenario 1 — Basic one-shot prompt

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "what time is it" 2>&1 | tee /tmp/nexus-manual-tests/headless-basic.log
```

### Expected result

- the command runs once and exits
- output appears without entering the REPL
- exit code is `0`
- no `>` prompt should appear

### Manual checks

- does the answer look like a single one-shot flow rather than an interactive session?
- is the output concise and readable?

---

## Scenario 2 — Prompt file input

```bash
cat > prompt.txt <<'EOF'
Summarize what this workspace is for.
EOF

uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt-file prompt.txt
```

### Expected result

- the file content is read as the prompt
- Nexus exits after one run
- no REPL is started

### Gap checklist

- does the UX make it obvious the file was consumed?
- if the file is missing, is the error clear?

---

## Scenario 3 — Piped stdin input

```bash
echo "Explain this input in one short line." | uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --stdin
```

### Expected result

- stdin content is used as the task
- the command exits normally

### What to look for

- if stdin is empty, is the behavior still understandable?
- if the shell pipeline fails upstream, is Nexus output still sensible?

---

## Scenario 4 — Output to JSON and JSONL

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "say hello" --output-format json | tee /tmp/nexus-manual-tests/output.json
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "say hello" --output-format jsonl | tee /tmp/nexus-manual-tests/output.jsonl
python -m json.tool /tmp/nexus-manual-tests/output.json >/dev/null && echo "json-ok"
head -n 5 /tmp/nexus-manual-tests/output.jsonl
```

### Expected result

- JSON mode emits valid JSON
- JSONL mode emits one JSON object per line
- output remains usable when no output file is specified

### Gap checklist

- is the difference between `json` and `jsonl` obvious from the output?
- are these formats easy to pipe into other tools?

---

## Scenario 5 — Write final output to a file

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "say hello" --output final.txt
cat final.txt
```

### Expected result

- final response is written to `final.txt`
- stdout should not become confusing or duplicated
- the written file should contain only the intended final payload

---

## Scenario 6 — Quiet mode

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "what time is it" --quiet 2>&1 | tee /tmp/nexus-manual-tests/quiet.log
```

### Expected result

- extra progress or tool-call noise should be reduced
- final output should remain usable

### UX questions

- is `--quiet` actually quieter in a way the user would expect?
- does it hide too much diagnostic information?

---

## Scenario 7 — No session persistence

Run:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --prompt "hello from no-session" --no-session --quiet
find .nexus/sessions -maxdepth 1 -type f | sort
```

### Expected result

- the command succeeds
- no new session file should be persisted for this run

### Gap checklist

- does the name `--no-session` match observed behavior clearly?
- does the command still update anything unexpectedly?

---

## Scenario 8 — Confirmation flow in an interactive terminal

This is the key UX scenario for mutating tools.
Run this **directly in your terminal**, not through a non-interactive pipeline:

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --provider mistral --model mistral-medium-latest --prompt "summarize this repo"
```

### Expected result

If the model requests a mutating tool such as `write_file`, Nexus should prompt inline, for example:

```text
Allow tool 'write_file'? [y/N]:
```

It should wait for your input instead of immediately exiting.

### Manual branches

#### Branch A — approve

Type:

```text
y
```

Expected:

- the run continues
- the tool either executes or the model continues its flow
- the command exits normally if the rest succeeds

#### Branch B — reject

Type:

```text
n
```

Expected:

- the mutating action is not approved
- output should make it obvious the action was denied or the run stopped
- the process should not hang indefinitely

### Gap checklist

- does the prompt appear in a way that is easy to notice?
- does the app explain what the tool wants to do?
- is plain `Allow tool 'write_file'?` enough context, or should it show arguments/path too?

---

## Scenario 9 — Non-interactive confirmation path must exit cleanly

Now verify the non-TTY case. This is intentionally different.

```bash
printf '' | uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --provider mistral --model mistral-medium-latest --prompt "summarize this repo"; echo "exit=$?"
```

### Expected result

If confirmation is required and there is no interactive stdin:

- Nexus should not wait forever
- it should print a confirmation-required message
- exit code should be `3`

### Gap checklist

- is the message actionable enough?
- does it tell the user when to use `--auto-confirm` versus `--mode plan`?

---

## Scenario 10 — Auto-confirm path

Use with caution in a scratch workspace only.

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --provider mistral --model mistral-medium-latest --prompt "summarize this repo" --auto-confirm; echo "exit=$?"
```

### Expected result

- the run should continue without asking inline for approval
- exit code should reflect the final outcome of the run

### Risk review

- does the command make it too easy to approve mutations accidentally?
- is there enough documentation warning against using it blindly?

---

## Scenario 11 — Plan mode as a non-mutating escape hatch

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --provider mistral --model mistral-medium-latest --mode plan --prompt "summarize this repo and say what files you would write"
```

### Expected result

- Nexus should avoid actually mutating files
- the response should feel like a plan or dry-run style flow

### Gap checklist

- does the runtime make the safety difference between `default`, `auto`, and `plan` clear enough?

---

## Scenario 12 — Provider override smoke tests

### Fake provider

```bash
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --provider fake --prompt "hello"
```

### Mistral provider

```bash
export MISTRAL_API_KEY="your-token"
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --provider mistral --model mistral-small-latest --prompt "summarize this repo"
```

### OpenAI-compatible provider

```bash
export OPENAI_API_KEY="your-token"
export AGENT_API_BASE_URL="https://your-endpoint.example/v1"
uv --directory /home/rishikesh/dev/exp/build-an-ai-agent/build-an-ai-agent run nexus --provider openai-compatible --model your-model --prompt "summarize this repo"
```

### Expected result

- provider overrides should be respected
- config errors should be readable if required env vars or URLs are missing

---

## Validation checklist before moving on

- [ ] `--prompt` works as a one-shot run
- [ ] `--prompt-file` works
- [ ] `--stdin` works
- [ ] `--output-format json` emits valid JSON
- [ ] `--output-format jsonl` emits line-oriented JSON objects
- [ ] `--output` writes final output to disk
- [ ] `--no-session` prevents session persistence
- [ ] TTY confirmation waits for input
- [ ] non-TTY confirmation exits cleanly with an actionable message
- [ ] `--auto-confirm` and `--mode plan` behave distinctly

---

## Suggested notes to carry into Chapter 03

Document any UI-level issues with:

- prompt source ambiguity
- quiet mode behavior
- exit-code clarity
- provider setup friction
- confirmation wording and trustworthiness

