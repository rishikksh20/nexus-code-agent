# Chapter 23: Headless Mode And CLI Flags

## Objective

Make the harness runnable from a terminal without any interactive prompts. Headless mode lets you drive the agent from shell scripts, CI pipelines, editor integrations, pre-commit hooks, or any other automated context where no human is present to type responses.

This chapter defines the full CLI surface, shows how to parse flags with `argparse`, and explains how headless execution differs from the interactive REPL at every significant step: input sourcing, confirmation handling, output routing, and session management.

---

## The Entry Point Design

The harness has two runtime personalities launched from the same binary:

```
agent                        # interactive REPL (default when no --prompt given)
agent --prompt "..."         # headless: run one task and exit
agent --prompt-file task.txt # headless: read task from a file
agent init                   # one-time setup command
agent config show            # config inspection without starting a session
agent version                # print version and exit
```

All three modes share the same `load_config()`, session management, tool registry, and agent loop. The difference is only in how input arrives and how confirmation is handled.

---

## Full CLI Flag Reference

### Positional Subcommands

| Subcommand | Description |
|---|---|
| *(none)* | Start the interactive REPL |
| `init` | Create default config files in the workspace and global directories |
| `config show [global\|local\|merged]` | Print config without starting a session |
| `config set <key> <value>` | Write a value to the local config |
| `config global set <key> <value>` | Write a value to the global config |
| `version` | Print the harness version and exit |

### Headless Flags

| Flag | Short | Type | Description |
|---|---|---|---|
| `--prompt <text>` | `-p` | `str` | Task prompt; enables headless mode |
| `--prompt-file <path>` | `-f` | `path` | Read prompt from a file; enables headless mode |
| `--stdin` | | `flag` | Read prompt from stdin (for pipes) |

### Session Flags

| Flag | Short | Type | Description |
|---|---|---|---|
| `--session <id>` | `-s` | `str` | Resume or create a named session |
| `--no-session` | | `flag` | Do not persist a session for this run |

### Config Override Flags

| Flag | Short | Type | Description |
|---|---|---|---|
| `--model <name>` | `-m` | `str` | Override `model_name` for this run |
| `--provider <name>` | | `str` | Override `provider` for this run |
| `--mode <mode>` | | `str` | Override `default_mode` (plan/default/auto) |
| `--config <path>` | `-c` | `path` | Use a specific local config file |
| `--global-config <path>` | | `path` | Use a specific global config file |
| `--max-tokens <n>` | | `int` | Override `compaction_hard_limit` |
| `--max-turns <n>` | | `int` | Stop after n agent loop iterations |

### Output Flags

| Flag | Short | Type | Description |
|---|---|---|---|
| `--output <file>` | `-o` | `path` | Write final assistant response to a file |
| `--output-format <fmt>` | | `str` | `text` (default), `json`, `jsonl` |
| `--no-stream` | | `flag` | Disable token streaming; print full response at end |
| `--quiet` | `-q` | `flag` | Suppress all output except the final response |
| `--verbose` | `-v` | `flag` | Increase log verbosity (alias for `--log-level DEBUG`) |

### Safety Flags

| Flag | Short | Type | Description |
|---|---|---|---|
| `--auto-confirm` | | `flag` | Approve all confirmation prompts automatically |
| `--deny-mutating` | | `flag` | Deny all mutating tools (equivalent to plan mode) |
| `--allowed-tools <names>` | | `csv` | Comma-separated tool allowlist for this run |
| `--denied-tools <names>` | | `csv` | Comma-separated tool denylist for this run |

### Skills And Plugins

| Flag | Short | Type | Description |
|---|---|---|---|
| `--skill <name>` | | `str` | Activate a skill for this run (repeatable) |
| `--no-plugins` | | `flag` | Skip plugin loading for this run |
| `--no-skills` | | `flag` | Skip skill loading for this run |

## Current Nexus Notes

The current Nexus runtime now implements these later-stage headless controls in the actual CLI surface:

- `--deny-mutating` now forces plan-mode behavior at runtime start
- `--skill` activates session skills for headless and interactive runs
- `--no-plugins` and `--no-skills` skip optional runtime loading paths explicitly
- `--provider openai-compatible` and `--provider openai` now select the live compatible client path
- headless runs still share the same session, tool, prompt, and post-session learning paths as the REPL

If `--provider` selects a live compatible provider without `api_base_url`, Nexus now fails during config resolution rather than waiting for runtime startup.

Provider errors during headless execution are now caught before propagating as unhandled exceptions. When the model call fails (no API key, 401/403 auth, 429 rate limit, missing `api_base_url`, or connection failure), the error is mapped to a user-readable message printed to the console and the run returns `HeadlessResult(exit_code=EXIT_ERROR)` with the raw exception string in `HeadlessResult.error`. This matches the REPL's error-handling behaviour and ensures headless callers can distinguish provider failures from other exit conditions using the exit code.

---

## Parsing CLI Arguments

```python
# cli/args.py
import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Minimal Python agent harness",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser("init", help="Create default config files")
    subparsers.add_parser("version", help="Print version and exit")

    config_cmd = subparsers.add_parser("config", help="Inspect or modify config")
    config_cmd.add_argument("action", nargs="?", choices=["show", "set", "global"], default="show")
    config_cmd.add_argument("args", nargs="*")

    # Headless input
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--prompt", "-p", metavar="TEXT", help="Task prompt (enables headless mode)")
    input_group.add_argument("--prompt-file", "-f", metavar="PATH", type=Path,
                             help="Read prompt from a file")
    input_group.add_argument("--stdin", action="store_true",
                             help="Read prompt from stdin")

    # Session
    parser.add_argument("--session", "-s", metavar="ID", help="Session ID to resume or create")
    parser.add_argument("--no-session", action="store_true", help="Do not persist a session")

    # Config overrides
    parser.add_argument("--model", "-m", metavar="NAME", help="Override model_name")
    parser.add_argument("--provider", metavar="NAME", help="Override provider")
    parser.add_argument("--mode", choices=["plan", "default", "auto"], help="Override execution mode")
    parser.add_argument("--config", "-c", metavar="PATH", type=Path, help="Local config file path")
    parser.add_argument("--global-config", metavar="PATH", type=Path, help="Global config file path")
    parser.add_argument("--max-tokens", metavar="N", type=int, help="Override compaction_hard_limit")
    parser.add_argument("--max-turns", metavar="N", type=int, help="Stop after N agent loop turns")

    # Output
    parser.add_argument("--output", "-o", metavar="FILE", type=Path, help="Write response to file")
    parser.add_argument("--output-format", choices=["text", "json", "jsonl"], default="text")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress all but the final response")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging")

    # Safety
    parser.add_argument("--auto-confirm", action="store_true",
                        help="Auto-approve all confirmation prompts")
    parser.add_argument("--deny-mutating", action="store_true",
                        help="Deny all mutating tools (forces plan mode behaviour)")
    parser.add_argument("--allowed-tools", metavar="NAMES",
                        help="Comma-separated tool allowlist")
    parser.add_argument("--denied-tools", metavar="NAMES",
                        help="Comma-separated tool denylist")

    # Skills / Plugins
    parser.add_argument("--skill", metavar="NAME", action="append", dest="skills",
                        help="Activate a skill (repeatable)")
    parser.add_argument("--no-plugins", action="store_true", help="Skip plugin loading")
    parser.add_argument("--no-skills", action="store_true", help="Skip skill loading")

    return parser


def args_to_config_overrides(args: argparse.Namespace) -> dict:
    """Convert parsed args into a dict suitable for load_config(cli_overrides=...)."""
    overrides: dict = {}

    if args.model:
        overrides["model_name"] = args.model
    if args.provider:
        overrides["provider"] = args.provider
    if args.mode:
        overrides["default_mode"] = args.mode
    if args.max_tokens:
        overrides["compaction_hard_limit"] = args.max_tokens
    if args.verbose:
        overrides["log_level"] = "DEBUG"
    if args.no_stream:
        overrides["stream_output"] = False
    if args.allowed_tools:
        overrides["allowed_tools"] = [t.strip() for t in args.allowed_tools.split(",")]
    if args.denied_tools:
        overrides["denied_tools"] = [t.strip() for t in args.denied_tools.split(",")]

    return overrides
```

---

## The Headless Runner

When a prompt is provided, skip the REPL entirely. Run the agent loop once, collect the response, and exit.

```python
# cli/headless.py
import asyncio
import json
import sys
from pathlib import Path

from config.loader import AgentConfig
from models import Message
from runtime.agent import Agent
from runtime.execution_modes import ExecutionMode
from runtime.sessions import SessionSnapshot, SessionStore, new_snapshot


async def run_headless(
    prompt: str,
    agent: Agent,
    config: AgentConfig,
    session: SessionSnapshot,
    *,
    auto_confirm: bool = False,
    deny_mutating: bool = False,
    max_turns: int | None = None,
    output_path: Path | None = None,
    output_format: str = "text",
    quiet: bool = False,
) -> int:
    """
    Run one task non-interactively.
    Returns an exit code: 0 for success, 1 for error.
    """
    from runtime.execution_modes import ExecutionMode

    mode = ExecutionMode.PLAN if deny_mutating else ExecutionMode(config.default_mode)
    history: list[Message] = list(session.messages)
    history.append(Message(role="user", content=prompt))

    final_response = ""
    exit_code = 0
    turn_count = 0

    try:
        async for event in agent.run(history, mode=mode, max_turns=max_turns):
            event_type = event.get("event")

            if event_type == "model_response":
                response = event["value"]
                history.append(response.message)
                final_response = response.message.content
                if not quiet and config.stream_output:
                    print(final_response)

            elif event_type == "tool_result":
                result = event["value"]
                history.append(Message(role="tool", content=result.output, name=result.tool_name))
                if not quiet:
                    print(f"  [tool:{result.tool_name}] {result.output[:120]}", file=sys.stderr)

            elif event_type == "confirmation_requested":
                if auto_confirm:
                    # Headless auto-confirm: log and proceed
                    req = event["value"]
                    print(
                        f"  [headless] Auto-confirming: {req.prompt}",
                        file=sys.stderr,
                    )
                    # Signal approval back to the agent (implementation-specific)
                else:
                    req = event["value"]
                    print(
                        f"Error: confirmation required but running headless without --auto-confirm.\n"
                        f"  Prompt: {req.prompt}\n"
                        f"  Re-run with --auto-confirm or --mode plan to avoid this.",
                        file=sys.stderr,
                    )
                    exit_code = 1
                    break

            elif event_type == "tool_denied":
                if not quiet:
                    print(f"  [denied] {event.get('reason', '')}", file=sys.stderr)

            elif event_type == "turn_completed":
                turn_count += 1

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        exit_code = 1

    # Write output
    _write_output(final_response, history, output_path, output_format, quiet)

    # Persist session unless disabled
    if not config.save_on_every_turn:
        session.messages.extend(history[len(session.messages):])
        store = SessionStore(config.session_dir)
        store.save(session)

    return exit_code


def _write_output(
    response: str,
    history: list[Message],
    output_path: Path | None,
    output_format: str,
    quiet: bool,
) -> None:
    if output_format == "json":
        data = json.dumps({"response": response}, indent=2)
    elif output_format == "jsonl":
        data = "\n".join(
            json.dumps({"role": m.role, "content": m.content}) for m in history
        )
    else:
        data = response

    if output_path:
        output_path.write_text(data, encoding="utf-8")
        if not quiet:
            print(f"\nOutput written to: {output_path}", file=sys.stderr)
    elif not quiet and output_format != "text":
        # text format was already printed during streaming; only print structured formats now
        print(data)
```

---

## Reading The Prompt In Headless Mode

```python
# cli/input.py
import sys
from pathlib import Path


def resolve_prompt(args) -> str | None:
    """Return the prompt string or None if this is an interactive run."""
    if args.prompt:
        return args.prompt

    if args.prompt_file:
        path = Path(args.prompt_file)
        if not path.exists():
            print(f"Prompt file not found: {path}", file=sys.stderr)
            raise SystemExit(1)
        return path.read_text(encoding="utf-8").strip()

    if args.stdin:
        return sys.stdin.read().strip()

    return None  # interactive mode
```

---

## The Main Entry Point

```python
# app.py
import asyncio
import sys
from pathlib import Path

from cli.args import build_parser, args_to_config_overrides
from cli.input import resolve_prompt
from cli.headless import run_headless
from config.loader import load_config, ensure_config_dirs, init_config
from runtime.repl_state import ReplState
from runtime.slash_commands import build_router
from runtime.execution_modes import ExecutionMode
from runtime.sessions import SessionStore, new_snapshot


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # ── Subcommands that do not start a session ──────────────────────────────

    if args.subcommand == "version":
        print("agent-harness 0.1.0")
        return

    if args.subcommand == "init":
        init_config(workspace_root=Path.cwd(), global_root=Path.home() / ".agent")
        return

    if args.subcommand == "config":
        # Minimal config inspection without a full session
        config = load_config(
            workspace_root=Path.cwd(),
            global_root=getattr(args, "global_config", None) or Path.home() / ".agent",
        )
        action = getattr(args, "action", "show")
        if action == "show":
            import dataclasses
            scope = args.args[0] if args.args else "merged"
            print(f"=== Config ({scope}) ===")
            for f in dataclasses.fields(config):
                print(f"  {f.name} = {getattr(config, f.name)!r}")
        return

    # ── Load config ──────────────────────────────────────────────────────────

    cli_overrides = args_to_config_overrides(args)
    config_path = getattr(args, "config", None)
    global_config_path = getattr(args, "global_config", None)

    config = load_config(
        workspace_root=config_path.parent if config_path else Path.cwd(),
        global_root=global_config_path.parent if global_config_path else Path.home() / ".agent",
        cli_overrides=cli_overrides,
    )
    ensure_config_dirs(config, global_root=Path.home() / ".agent")

    # ── Session ──────────────────────────────────────────────────────────────

    import uuid
    session_id = args.session or str(uuid.uuid4())
    store = SessionStore(config.session_dir)
    try:
        session = store.load(session_id)
    except FileNotFoundError:
        session = new_snapshot(session_id)

    # ── Build the agent ──────────────────────────────────────────────────────

    # (Assemble model client, tool registry, hooks, etc. — see prior chapters)
    agent = _build_agent(config, args)

    # ── Headless or interactive ───────────────────────────────────────────────

    prompt = resolve_prompt(args)

    if prompt is not None:
        exit_code = asyncio.run(
            run_headless(
                prompt=prompt,
                agent=agent,
                config=config,
                session=session,
                auto_confirm=getattr(args, "auto_confirm", False),
                deny_mutating=getattr(args, "deny_mutating", False),
                max_turns=getattr(args, "max_turns", None),
                output_path=getattr(args, "output", None),
                output_format=getattr(args, "output_format", "text"),
                quiet=getattr(args, "quiet", False),
            )
        )
        raise SystemExit(exit_code)
    else:
        # Interactive REPL
        state = ReplState(
            config=config,
            mode=ExecutionMode(config.default_mode),
            session=session,
            tool_registry=agent.tool_registry,
        )
        router = build_router()
        asyncio.run(_interactive_repl(state, agent, router))


async def _interactive_repl(state, agent, router) -> None:
    from runtime.slash_commands import repl as run_repl
    await run_repl(state, agent, router)


def _build_agent(config, args):
    """
    Assemble the agent from config and args.
    This is a stub; in practice wire in the model client, tool registry,
    permission checker, hook executor, and context builder from prior chapters.
    """
    from integrations.fake_model import FakeModelClient
    from tools.registry import ToolRegistry
    from tools.builtin import GetTimeTool, WriteNoteTool
    from runtime.agent import Agent

    registry = ToolRegistry()
    registry.register(GetTimeTool())
    if "write_note" not in (config.denied_tools or []):
        registry.register(WriteNoteTool())

    model = FakeModelClient()
    return Agent(model, registry)


if __name__ == "__main__":
    main()
```

---

## Usage Examples

### Interactive REPL (default)

```bash
agent
agent --session my-project
agent --model gpt-4o --mode default
```

### Headless: single prompt

```bash
agent --prompt "Summarise the README.md file"
agent --prompt "List all TODO comments" --mode plan --quiet
agent --prompt "Refactor utils.py" --mode auto --auto-confirm --session refactor-run-1
```

### Headless: prompt from file

```bash
agent --prompt-file tasks/daily-review.txt --output results/review.md
agent --prompt-file ci-task.txt --no-session --quiet --output-format json
```

### Headless: piped from stdin

```bash
echo "What does this error mean? $(cat error.log)" | agent --stdin --quiet
git diff HEAD~1 | agent --stdin --prompt "Review this diff and suggest improvements"
```

> When both `--stdin` and `--prompt` could conflict, `--prompt` takes priority. If reading a diff via pipe, pass the diff via stdin and set the instruction via `--prompt`.

### One-shot subcommands

```bash
agent init                                  # create config files
agent version                               # print version
agent config show                           # print merged config
agent config show global                    # print global config
agent config set model_name gpt-4o          # update local config
agent config global set stream_output false # update global config
```

### CI pipeline example

```bash
#!/bin/bash
set -e

# Run an automated code review on every PR
DIFF=$(git diff origin/main...HEAD)

agent \
  --stdin \
  --prompt "Review this PR diff for security issues and code quality. Be concise." \
  --mode plan \
  --no-session \
  --quiet \
  --output pr-review.md \
  --output-format text <<< "$DIFF"

echo "Review written to pr-review.md"
cat pr-review.md
```

---

## Handling Confirmation In Headless Mode

Headless runs will encounter confirmation requests for mutating tools. There are three strategies:

| Strategy | Flag | Behaviour |
|---|---|---|
| Fail with error | *(default)* | Print an error and exit with code 1 |
| Auto-approve all | `--auto-confirm` | Log and approve every request |
| Block all mutations | `--mode plan` or `--deny-mutating` | Deny mutating tools before they request confirmation |

The safest approach for CI is `--mode plan` or `--deny-mutating`. Use `--auto-confirm` only in a controlled local automation where you have already reviewed the task.

Never combine `--auto-confirm` with a wide `--mode auto` in an untrusted or unknown-scope task. That combination can approve dangerous side effects without any visibility.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Task completed successfully |
| `1` | Task failed: error in agent loop, tool failure, or unhandled exception |
| `2` | Bad arguments or config error at startup |
| `3` | Confirmation required but running headless without `--auto-confirm` |

Use exit codes in shell scripts:

```bash
agent --prompt "Run tests" --mode plan --quiet
if [ $? -ne 0 ]; then
    echo "Agent task failed" >&2
    exit 1
fi
```

---

## Installing The CLI Entry Point

Add to `pyproject.toml`:

```toml
[project.scripts]
agent = "agent_harness.app:main"
```

Then install in development mode:

```bash
pip install -e .
agent version
```

Or add a simple wrapper script if you are not using a package:

```bash
#!/usr/bin/env python3
# bin/agent
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent_harness.app import main
main()
```

---

## Action Plan

1. Create `cli/args.py` with `build_parser()` and `args_to_config_overrides()`.
2. Create `cli/input.py` with `resolve_prompt()` for the three input modes.
3. Create `cli/headless.py` with `run_headless()` and `_write_output()`.
4. Update `app.py` to branch on prompt presence for headless vs. interactive.
5. Register the `agent` CLI entry point in `pyproject.toml`.
6. Add dedicated tests for: `--mode plan` blocking mutations, `--auto-confirm` approving them, exit codes on errors, `--output` file writing, stdin prompt reading.
7. Document the CI pipeline pattern in the project README.

## Validation Checklist

- `agent --prompt "hello" --mode plan` exits 0 and produces a response.
- `agent --prompt "write a file" --deny-mutating` exits without writing any file.
- `agent --prompt "..." --auto-confirm` approves confirmations without hanging.
- `echo "hello" | agent --stdin` reads the prompt from stdin correctly.
- `agent --output out.txt` writes to the file and does not print to stdout.
- `agent --quiet` suppresses all output except the final response.
- `agent --no-session` leaves no session file after exit.
- Exit code `3` is returned when confirmation is required without `--auto-confirm`.

## Definition Of Done

This chapter is complete when the harness can be driven entirely from shell scripts and CI pipelines without any human present, and when the exit code and output file can be reliably used by calling scripts to detect success or failure.
