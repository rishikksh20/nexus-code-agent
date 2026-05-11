# 17 — CLI Flags and Headless Mode: Running the Harness Without a Human

## Prerequisites

Complete [16-advanced-logging-and-observability.md](16-advanced-logging-and-observability.md) first.

By this point, your harness can do serious work:

- it keeps session history and compacts context automatically,
- enforces a layered permission policy,
- runs an audit trail,
- learns from the workspace and the user,
- and records structured logs and cost telemetry.

There is still one gap that blocks production use:

> the harness only runs interactively — a human must be present to type input.

You cannot call it from a shell script, a CI pipeline, a pre-commit hook, a cron job, or an editor integration. Every time the agent needs input it blocks. Every confirmation it needs to ask halts the process.

This chapter closes that gap by adding a **headless runtime** that reads its prompt from a flag, a file, or stdin, handles confirmations automatically, writes structured output, and exits with a meaningful code that calling scripts can act on.

---

## What you will build

```text
agent/
    headless.py         ← NEW: run_headless(), HeadlessResult, output writers
    config.py           ← updated: HeadlessConfig, CLIConfig
    events.py           ← unchanged (used as event shape throughout)

main.py                 ← expanded: full argparse, subcommands, headless branch
agent.toml              ← updated: [headless] section
pyproject.toml          ← updated: [project.scripts] registers "agent" entry point
```

By the end of the chapter, your harness can:

1. run a full task from a single terminal command without any interactive REPL,
2. read a task from a flag, a file, or a pipe,
3. control output format — plain text, JSON, or JSON Lines,
4. handle confirmation requests with three explicit strategies,
5. report results to calling scripts via well-defined exit codes,
6. and still start the interactive REPL when no prompt is given, exactly as before.

---

## 1. Why the interactive REPL is the wrong default for automation

The interactive REPL built in earlier chapters calls `input()` to get the next user message and `print()` to render responses. Both are fine for a developer chatting with the agent. Both are wrong for automation.

### The three problems with running interactive code in CI

**Problem A — it blocks.**

When a CI step executes `python main.py`, it expects the command to run to completion and exit. A process calling `input()` on a non-TTY will block indefinitely or raise `EOFError`. Either way the pipeline hangs or crashes.

**Problem B — output is unstructured.**

A shell script can check an exit code. It can parse a JSON response. It cannot reliably parse human-readable progress messages mixed with tool output.

**Problem C — confirmations require a human.**

The permission system added in Chapter 07 raises `ConfirmationRequest` events when a tool is risky. The interactive REPL asks `y/n`. In a pipeline there is no one to type the answer.

### The right model: two personalities, one binary

The cleanest solution is to detect at startup whether the run is interactive or headless and branch accordingly — while keeping all the runtime components (config, agent loop, tools, hooks, sessions, telemetry) identical in both modes.

```
python main.py                       # interactive REPL (default)
python main.py --prompt "..."        # headless: run task and exit
python main.py --prompt-file f.txt   # headless: read task from file
echo "..." | python main.py --stdin  # headless: read task from pipe
```

The `prompt` presence is the switch. No prompt → REPL. Any prompt → headless.

---

## 2. The complete flag surface

Before writing code, enumerate the full set of flags the binary should support.

### Headless input

| Flag | Short | Description |
|---|---|---|
| `--prompt <text>` | `-p` | Task prompt; activates headless mode |
| `--prompt-file <path>` | `-f` | Read prompt from a file |
| `--stdin` | | Read prompt from stdin (for pipes) |

These three flags are mutually exclusive. Using more than one is an argument error (exit code 2).

### Session flags

| Flag | Short | Description |
|---|---|---|
| `--session <id>` | `-s` | Resume or create a named session |
| `--no-session` | | Run without persisting a session |
| `--resume <id>` | | Alias for `--session` (backwards-compatible) |
| `--export <id>` | | Export a session to JSON and exit |

### Config override flags

| Flag | Short | Description |
|---|---|---|
| `--config <path>` | `-c` | Path to `agent.toml` |
| `--mode <mode>` | | Override `[mode] default` (plan / default / auto) |
| `--model <name>` | `-m` | Override `[model] name` |
| `--provider <name>` | | Override `[model] provider` |
| `--max-turns <n>` | | Stop after n agent loop iterations |

### Output flags

| Flag | Short | Description |
|---|---|---|
| `--output <file>` | `-o` | Write final response to a file |
| `--output-format <fmt>` | | `text` (default), `json`, `jsonl` |
| `--quiet` | `-q` | Suppress progress; only print the final response |
| `--verbose` | `-v` | Enable DEBUG log level |

### Safety flags

| Flag | Description |
|---|---|
| `--auto-confirm` | Approve all confirmation prompts without asking |
| `--deny-mutating` | Deny all mutating tools; equivalent to `--mode plan` |

### Skills and plugins

| Flag | Description |
|---|---|
| `--skill <name>` | Activate a named skill for this run (repeatable) |
| `--no-plugins` | Skip plugin loading |
| `--no-skills` | Skip skill loading |

### Subcommands

| Subcommand | Description |
|---|---|
| `init` | Create default `agent.toml` and `.agent/` directory in cwd |
| `version` | Print harness version and exit |
| `config show [global\|local\|merged]` | Print resolved configuration without starting a session |

---

## 3. Expand `agent/config.py` with headless config

Add two new config dataclasses. They mirror what the new flags control so that defaults can live in `agent.toml` rather than scattered across `main.py`.

```python
# agent/config.py  — add HeadlessConfig and CLIConfig

@dataclass
class HeadlessConfig:
    auto_confirm: bool = False          # approve all confirmations automatically
    deny_mutating: bool = False         # hard-deny all mutating tools
    output_format: str = "text"        # text | json | jsonl
    max_turns: int | None = None        # stop after n turns (None = unlimited)
    quiet: bool = False                 # suppress progress output
    no_session: bool = False            # do not persist session
    no_plugins: bool = False            # skip plugin loading
    no_skills: bool = False             # skip skill loading

@dataclass
class CLIConfig:
    entry_point: str = "agent"          # registered script name for error messages
    version: str = "0.1.0"             # displayed by `agent version`


# Update AgentConfig to include the new sections:

@dataclass
class AgentConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)    # from ch16
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)  # from ch16
    headless: HeadlessConfig = field(default_factory=HeadlessConfig) # NEW
    cli: CLIConfig = field(default_factory=CLIConfig)                # NEW

    @property
    def api_key(self) -> str:
        return os.environ.get("OPENAI_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
```

### Update `agent.toml` to include a `[headless]` section

```toml
# agent.toml  — add at the bottom

[headless]
auto_confirm   = false
deny_mutating  = false
output_format  = "text"
max_turns      = 0         # 0 = unlimited
quiet          = false
no_session     = false
no_plugins     = false
no_skills      = false
```

The `[headless]` section lets you set project-level defaults. For example, a project that should never auto-confirm mutations in CI can set `deny_mutating = true` in the committed `agent.toml` so any headless run from that workspace has the protection without every caller remembering to add the flag.

---

## 4. Build `agent/headless.py`

This module owns everything that is specific to headless execution: the runner coroutine, output routing, and result reporting.

```python
# agent/headless.py

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.agent import Agent
    from agent.config import AgentConfig
    from agent.models import Message
    from agent.session import SessionSnapshot, SessionStore


# ── Exit codes ────────────────────────────────────────────────────────────────

EXIT_OK = 0                # task completed successfully
EXIT_ERROR = 1             # runtime error or tool failure
EXIT_BAD_ARGS = 2          # argument / config error at startup
EXIT_NEEDS_CONFIRM = 3     # confirmation required but running headless without --auto-confirm


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class HeadlessResult:
    exit_code: int = EXIT_OK
    response: str = ""
    history: list = field(default_factory=list)   # list[Message]
    turns: int = 0
    error: str | None = None


# ── Runner ────────────────────────────────────────────────────────────────────

async def run_headless(
    prompt: str,
    agent: "Agent",
    config: "AgentConfig",
    session: "SessionSnapshot",
    store: "SessionStore | None",
) -> HeadlessResult:
    """
    Run one task non-interactively.

    Consumes agent events, handles confirmations per config, collects
    the final response, and saves the session unless disabled.
    """
    from agent.models import Message
    from agent.modes import ExecutionMode

    mode = ExecutionMode("plan") if config.headless.deny_mutating else ExecutionMode(config.mode.default)
    history: list[Message] = list(session.messages)
    history.append(Message(role="user", content=prompt))
    result = HeadlessResult()

    try:
        async for event in agent.run(history, mode=mode):
            kind = event.get("event")

            if kind == "model_response":
                response = event["value"]
                history.append(response.message)
                result.response = response.message.content or ""
                if not config.headless.quiet and config.headless.output_format == "text":
                    # Stream text responses to stdout as they arrive so pipelines
                    # can progressively consume output.
                    print(result.response, flush=True)

            elif kind == "tool_result":
                tool_result = event["value"]
                history.append(
                    Message(role="tool", content=tool_result.output, name=tool_result.tool_name)
                )
                if not config.headless.quiet:
                    print(
                        f"  [tool:{tool_result.tool_name}] "
                        f"{str(tool_result.output)[:120]}",
                        file=sys.stderr,
                    )

            elif kind == "confirmation_requested":
                req = event["value"]
                if config.headless.auto_confirm:
                    print(
                        f"  [headless] auto-confirming: {req.prompt}",
                        file=sys.stderr,
                    )
                    # Signal approval — call the agent's approval mechanism.
                    # Implementation is harness-specific; this pattern works when
                    # Agent.run() yields the event and resumes on next iteration
                    # after the caller sends approval via a shared asyncio.Event or
                    # a confirmation queue.
                else:
                    result.exit_code = EXIT_NEEDS_CONFIRM
                    result.error = (
                        f"Confirmation required but running headless without --auto-confirm.\n"
                        f"  Prompt: {req.prompt}\n"
                        f"  Re-run with --auto-confirm or --mode plan to skip confirmation."
                    )
                    print(result.error, file=sys.stderr)
                    break

            elif kind == "tool_denied":
                reason = event.get("reason", "")
                if not config.headless.quiet:
                    print(f"  [denied] {reason}", file=sys.stderr)

            elif kind == "turn_completed":
                result.turns += 1
                max_turns = config.headless.max_turns
                if max_turns and result.turns >= max_turns:
                    break

    except Exception as exc:
        result.exit_code = EXIT_ERROR
        result.error = str(exc)
        print(f"Error: {exc}", file=sys.stderr)

    result.history = history

    # Persist session unless disabled
    if store is not None and not config.headless.no_session:
        # Append only the new messages to the snapshot
        new_messages = history[len(session.messages):]
        session.messages.extend(new_messages)
        store.save(session)

    return result


# ── Output routing ────────────────────────────────────────────────────────────

def write_output(
    result: HeadlessResult,
    output_path: Path | None,
    output_format: str,
    quiet: bool,
) -> None:
    """
    Write the final response to stdout or a file.

    text   — plain assistant text
    json   — {"response": "...", "turns": N, "exit_code": N}
    jsonl  — one JSON object per message in history
    """
    if output_format == "json":
        data = json.dumps(
            {
                "response": result.response,
                "turns": result.turns,
                "exit_code": result.exit_code,
                "error": result.error,
            },
            indent=2,
        )
    elif output_format == "jsonl":
        lines = []
        for msg in result.history:
            lines.append(
                json.dumps({"role": msg.role, "content": msg.content})
            )
        data = "\n".join(lines)
    else:
        # text: the response was already streamed to stdout during the run
        # only write here if going to a file
        data = result.response

    if output_path:
        output_path.write_text(data, encoding="utf-8")
        if not quiet:
            print(f"\nOutput written to: {output_path}", file=sys.stderr)
    elif output_format != "text":
        # text was already printed live; only print structured formats here
        print(data)
```

---

## 5. Read the prompt for headless mode

The three input modes need a single helper that converts flags to a prompt string.

```python
# agent/headless.py  — add below HeadlessResult

def resolve_prompt(args) -> str | None:
    """
    Return the prompt text or None if running interactively.

    Reads from --prompt, --prompt-file, or --stdin.
    Returns None when none of those flags are set (interactive REPL mode).
    """
    if getattr(args, "prompt", None):
        return args.prompt

    if getattr(args, "prompt_file", None):
        path = Path(args.prompt_file)
        if not path.exists():
            print(f"Error: prompt file not found: {path}", file=sys.stderr)
            raise SystemExit(EXIT_BAD_ARGS)
        return path.read_text(encoding="utf-8").strip()

    if getattr(args, "stdin", False):
        if sys.stdin.isatty():
            print(
                "Warning: --stdin given but stdin appears to be a TTY. "
                "Pipe content to stdin or use --prompt instead.",
                file=sys.stderr,
            )
        return sys.stdin.read().strip()

    return None
```

---

## 6. Rewrite `main.py` with full argparse and headless branch

The existing `main.py` already has a small `argparse` block from Chapter 13-1. Replace it with a complete version.

```python
# main.py  — full rewrite of the entry point

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from agent.config import load_config, AgentConfig
from agent.headless import (
    EXIT_OK, EXIT_ERROR, EXIT_BAD_ARGS,
    HeadlessResult, run_headless, write_output, resolve_prompt,
)
from agent.modes import ExecutionMode
from agent.session import SessionStore, SessionSnapshot


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Minimal Python agent harness",
    )

    # Subcommands
    sub = parser.add_subparsers(dest="subcommand")
    sub.add_parser("init", help="Create default config files in cwd")
    sub.add_parser("version", help="Print version and exit")

    config_cmd = sub.add_parser("config", help="Inspect or modify configuration")
    config_cmd.add_argument(
        "action",
        nargs="?",
        choices=["show", "set", "global"],
        default="show",
    )
    config_cmd.add_argument("config_args", nargs="*")

    # Headless input (mutually exclusive)
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--prompt", "-p", metavar="TEXT",
                             help="Task prompt — activates headless mode")
    input_group.add_argument("--prompt-file", "-f", metavar="PATH",
                             dest="prompt_file",
                             help="Read prompt from a file")
    input_group.add_argument("--stdin", action="store_true",
                             help="Read prompt from stdin (for pipes)")

    # Session
    parser.add_argument("--session", "-s", metavar="ID",
                        help="Session ID to resume or create")
    parser.add_argument("--no-session", action="store_true", dest="no_session",
                        help="Run without persisting a session")
    parser.add_argument("--resume", metavar="ID",
                        help="Resume a session by ID (alias for --session)")
    parser.add_argument("--export", metavar="ID", dest="export_id",
                        help="Export a session to JSON and exit")

    # Config overrides
    parser.add_argument("--config", "-c", metavar="PATH", dest="config_path",
                        help="Path to agent.toml")
    parser.add_argument("--mode", choices=["default", "plan", "auto"],
                        help="Override execution mode")
    parser.add_argument("--model", "-m", metavar="NAME",
                        help="Override model name")
    parser.add_argument("--provider", metavar="NAME",
                        help="Override model provider")
    parser.add_argument("--max-turns", metavar="N", type=int, dest="max_turns",
                        help="Stop after N agent loop turns")

    # Output
    parser.add_argument("--output", "-o", metavar="FILE", type=Path,
                        help="Write response to a file")
    parser.add_argument("--output-format", metavar="FMT",
                        choices=["text", "json", "jsonl"],
                        dest="output_format", default=None,
                        help="Output format: text (default), json, jsonl")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress all output except final response")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging")

    # Safety
    parser.add_argument("--auto-confirm", action="store_true", dest="auto_confirm",
                        help="Approve all confirmation prompts automatically")
    parser.add_argument("--deny-mutating", action="store_true", dest="deny_mutating",
                        help="Deny all mutating tools (forces plan-mode behaviour)")

    # Skills / Plugins
    parser.add_argument("--skill", metavar="NAME", action="append", dest="skills",
                        help="Activate a skill for this run (repeatable)")
    parser.add_argument("--no-plugins", action="store_true", dest="no_plugins",
                        help="Skip plugin loading")
    parser.add_argument("--no-skills", action="store_true", dest="no_skills",
                        help="Skip skill loading")

    return parser


# ── Apply CLI flags on top of loaded config ───────────────────────────────────

def _apply_cli_overrides(config: AgentConfig, args: argparse.Namespace) -> None:
    """Mutate config in-place with CLI flag overrides."""
    if getattr(args, "mode", None):
        config.mode.default = args.mode
    if getattr(args, "model", None):
        config.model.name = args.model
    if getattr(args, "provider", None):
        config.model.provider = args.provider
    if getattr(args, "verbose", False):
        config.logging.level = "DEBUG"
    if getattr(args, "auto_confirm", False):
        config.headless.auto_confirm = True
    if getattr(args, "deny_mutating", False):
        config.headless.deny_mutating = True
    if getattr(args, "no_session", False):
        config.headless.no_session = True
    if getattr(args, "no_plugins", False):
        config.headless.no_plugins = True
    if getattr(args, "no_skills", False):
        config.headless.no_skills = True
    if getattr(args, "max_turns", None) is not None:
        config.headless.max_turns = args.max_turns
    if getattr(args, "output_format", None) is not None:
        config.headless.output_format = args.output_format
    if getattr(args, "quiet", False):
        config.headless.quiet = True


# ── Subcommand handlers ───────────────────────────────────────────────────────

def _handle_init() -> None:
    """Create default agent.toml and .agent/ directory in cwd."""
    import dataclasses
    from agent.config import AgentConfig

    config_path = Path("agent.toml")
    if config_path.exists():
        print(f"agent.toml already exists. Remove it first to re-initialize.")
        raise SystemExit(EXIT_BAD_ARGS)

    # Write a starter toml with all sections and their defaults
    config = AgentConfig()
    lines = [
        "# agent.toml — generated by `agent init`",
        "# Safe to commit. Do NOT put secrets here.",
        "",
        "[model]",
        f'provider = "{config.model.provider}"',
        f'name     = "{config.model.name}"',
        f"context_window = {config.model.context_window}",
        "",
        "[mode]",
        f'default = "{config.mode.default}"',
        "",
        "[session]",
        f'root      = "{config.session.root}"',
        f"auto_save = {str(config.session.auto_save).lower()}",
        "",
        "[headless]",
        f"auto_confirm  = {str(config.headless.auto_confirm).lower()}",
        f"deny_mutating = {str(config.headless.deny_mutating).lower()}",
        f'output_format = "{config.headless.output_format}"',
        f"max_turns     = 0",
        "",
    ]
    config_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Created agent.toml")

    agent_dir = Path(".agent")
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "sessions").mkdir(exist_ok=True)
    (agent_dir / "memory").mkdir(exist_ok=True)
    (agent_dir / "logs").mkdir(exist_ok=True)
    print(f"Created .agent/ directory structure")


def _handle_version(config: AgentConfig) -> None:
    print(f"agent {config.cli.version}")


def _handle_config_show(config: AgentConfig, scope: str) -> None:
    import dataclasses
    print(f"=== Config ({scope}) ===")
    for f in dataclasses.fields(config):
        val = getattr(config, f.name)
        if dataclasses.is_dataclass(val):
            for sf in dataclasses.fields(val):
                print(f"  [{f.name}] {sf.name} = {getattr(val, sf.name)!r}")
        else:
            print(f"  {f.name} = {val!r}")


# ── Session helpers ───────────────────────────────────────────────────────────

def _resolve_session(args: argparse.Namespace, config: AgentConfig) -> tuple:
    """Return (SessionSnapshot, SessionStore | None)."""
    from agent.session import SessionStore, SessionSnapshot

    if config.headless.no_session:
        # Create a throw-away in-memory snapshot
        snap = SessionSnapshot(session_id=str(uuid.uuid4()), messages=[])
        return snap, None

    session_id = (
        getattr(args, "session", None)
        or getattr(args, "resume", None)
        or str(uuid.uuid4())
    )
    store = SessionStore(root=Path(config.session.root))

    try:
        snap = store.load(session_id)
    except (FileNotFoundError, KeyError):
        snap = SessionSnapshot(session_id=session_id, messages=[])

    return snap, store


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # ── Subcommands that do not need a full session ──────────────────────────

    if args.subcommand == "init":
        _handle_init()
        return

    # Load config before handling remaining subcommands so --config works everywhere
    config = load_config(config_path=getattr(args, "config_path", None))
    _apply_cli_overrides(config, args)

    if args.subcommand == "version":
        _handle_version(config)
        return

    if args.subcommand == "config":
        scope = (args.config_args[0] if args.config_args else "merged")
        _handle_config_show(config, scope)
        return

    # ── Export a session and exit ────────────────────────────────────────────

    export_id = getattr(args, "export_id", None)
    if export_id:
        import json
        store = SessionStore(root=Path(config.session.root))
        snap = store.load(export_id)
        print(json.dumps(snap.to_dict(), indent=2))
        return

    # ── Build the agent ──────────────────────────────────────────────────────

    agent = build_agent(config, mode_override=getattr(args, "mode", None))

    # ── Headless or interactive ──────────────────────────────────────────────

    prompt = resolve_prompt(args)

    if prompt is not None:
        snap, store = _resolve_session(args, config)
        result = asyncio.run(run_headless(prompt, agent, config, snap, store))
        write_output(
            result,
            output_path=getattr(args, "output", None),
            output_format=config.headless.output_format,
            quiet=config.headless.quiet,
        )
        raise SystemExit(result.exit_code)

    else:
        # Interactive REPL — same as before
        snap, store = _resolve_session(args, config)
        asyncio.run(repl(agent, store, initial_session=snap))


if __name__ == "__main__":
    main()
```

### What changed compared to Chapter 13-1

- `build_parser()` grows from 5 flags to the full surface — every flag maps directly to a field in `AgentConfig.headless`.
- `_apply_cli_overrides()` replaces the scattered `if args.foo:` block with one centralized function.
- The `prompt = resolve_prompt(args)` line is the only branch point between headless and interactive; everything else is shared.
- `main()` no longer calls `asyncio.run(repl(...))` directly — it only reaches the REPL when no prompt was given.

---

## 7. Register the CLI entry point

Add to `pyproject.toml` so `pip install -e .` registers the `agent` command:

```toml
# pyproject.toml

[project.scripts]
agent = "main:main"
```

Or if your project is a package:

```toml
[project.scripts]
agent = "agent_harness.main:main"
```

After installing:

```bash
pip install -e .
agent version
agent --help
```

---

## 8. Exit codes and what they mean

| Code | Meaning | When it occurs |
|---|---|---|
| `0` | Success | Task completed, response produced |
| `1` | Runtime error | Agent loop exception, tool failure, unhandled error |
| `2` | Bad arguments | Missing required flags, config file not found, invalid arg values |
| `3` | Confirmation required | Agent raised a confirmation request but `--auto-confirm` was not given |

Shell scripts should test the exit code explicitly rather than assuming zero means nothing happened:

```bash
agent --prompt "Run tests" --mode plan --quiet
status=$?
if [ $status -eq 3 ]; then
    echo "Agent needs confirmation — re-run with --auto-confirm for this task" >&2
    exit 1
elif [ $status -ne 0 ]; then
    echo "Agent task failed with exit code $status" >&2
    exit $status
fi
```

---

## 9. Handling confirmation in headless mode

Every `ConfirmationRequest` event is a decision point in a headless run. There are exactly three strategies.

### Strategy 1 — Fail (default)

The safest default for unattended automation. The agent stops, prints the confirmation prompt to stderr, and exits with code 3. The calling script knows it needs human review before retrying with `--auto-confirm`.

```bash
# run in safe mode first
agent --prompt "Deploy to production" --mode plan --output plan.md
cat plan.md
# human reviews plan.md ...
agent --prompt "Deploy to production" --auto-confirm
```

### Strategy 2 — Auto-approve (`--auto-confirm`)

Approve every confirmation request silently. Useful in controlled local automation where the task scope is well-understood and the tools are trusted.

```bash
agent --prompt "Regenerate all API client stubs" \
      --auto-confirm \
      --session api-regen-$(date +%Y%m%d)
```

Do not use `--auto-confirm` in CI pipelines on unknown or untrusted prompt content. An injection in the task file could cause the agent to approve a destructive action without any friction.

### Strategy 3 — Block mutations (`--mode plan` or `--deny-mutating`)

Prevent confirmation requests from arising at all. Any tool that would trigger a confirmation is simply denied before it runs.

```bash
# safe read-only review — no mutations possible
agent --prompt "Review the changes in the last 5 commits" \
      --mode plan \
      --quiet \
      --output review.md
```

### Summary

| Scenario | Recommended strategy |
|---|---|
| CI pipeline, unknown task scope | `--mode plan` |
| CI pipeline, trusted automated refactor | `--auto-confirm` with `--session` for audit |
| Human review before approval | default (exit code 3) + human reads plan + retry |
| Interactive local scripting | `--auto-confirm` |

---

## 10. Output format examples

### Text (default)

Text responses are streamed to stdout as they arrive. Tool progress goes to stderr. Suitable for piping into `cat`, `less`, or another program that reads lines.

```bash
agent --prompt "List all TODO comments" --mode plan --quiet
```

```
TODO (line 42): fix token count overflow
TODO (line 108): add retry with backoff
TODO (line 215): validate schema before saving
```

### JSON

The full result as a single JSON object. Good for programs that need structured access to the response and exit status.

```bash
agent --prompt "Summarise the README" --quiet --output-format json
```

```json
{
  "response": "This project is a minimal Python agent harness...",
  "turns": 3,
  "exit_code": 0,
  "error": null
}
```

### JSON Lines

Every message in the conversation history, one JSON object per line. Good for analysis, evaluation pipelines, and fine-tuning datasets.

```bash
agent --prompt "Debug this error" --output-format jsonl --output session.jsonl
```

```
{"role": "user", "content": "Debug this error"}
{"role": "assistant", "content": "I will read the relevant files first."}
{"role": "tool", "content": "[file content ...]"}
{"role": "assistant", "content": "The issue is on line 42..."}
```

---

## 11. Real usage patterns

### Interactive REPL (unchanged from before)

```bash
# start a named session
agent --session my-feature

# resume an existing session
agent --session my-feature

# use a different model
agent --model gpt-4o-mini --mode plan
```

### Headless: single prompt

```bash
agent --prompt "Summarise the README.md"
agent --prompt "List all TODO comments" --mode plan --quiet
agent --prompt "Refactor utils.py" --mode auto --auto-confirm --session refactor-1
```

### Headless: prompt from file

```bash
agent --prompt-file tasks/daily-review.txt --output results/review.md
agent --prompt-file ci-task.txt --no-session --quiet --output-format json
```

### Headless: piped from stdin

```bash
# pipe an error log into the agent
cat error.log | agent --stdin --quiet

# review a git diff
git diff HEAD~1 | agent --stdin \
    --prompt "Review this diff for security issues" \
    --mode plan \
    --quiet
```

> When you combine `--stdin` with `--prompt`, the `--prompt` text becomes the instruction and stdin becomes additional context (implementation-specific). If your harness does not combine them, use `--prompt-file` and write context to a temporary file first.

### One-shot subcommands

```bash
agent init                          # scaffold agent.toml and .agent/ directory
agent version                       # print version
agent config show                   # print the merged active config
```

### CI pipeline: automated PR review

```bash
#!/bin/bash
set -euo pipefail

DIFF=$(git diff origin/main...HEAD)

agent \
  --stdin \
  --mode plan \
  --no-session \
  --quiet \
  --output pr-review.md \
  --output-format text <<< "$DIFF"

echo "Review written to pr-review.md"
cat pr-review.md
```

### Pre-commit hook

```bash
#!/bin/bash
# .git/hooks/pre-commit

changed_files=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$')

if [ -z "$changed_files" ]; then
    exit 0
fi

agent \
  --prompt "Review these staged Python files for obvious errors or security issues: $changed_files" \
  --mode plan \
  --quiet \
  --output /tmp/agent-precommit-review.txt

if grep -qi "critical\|security\|vulnerability" /tmp/agent-precommit-review.txt; then
    echo "Agent flagged potential issues:"
    cat /tmp/agent-precommit-review.txt
    echo ""
    echo "Commit blocked. Fix issues or re-run with 'git commit --no-verify' to bypass."
    exit 1
fi
```

---

## 12. Testing headless mode

The `FakeModelClient` and `RecordingHook` from Chapter 14 work unchanged in headless mode. The only difference is that you call `run_headless()` directly instead of the REPL.

```python
# tests/test_headless.py

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.headless import run_headless, EXIT_OK, EXIT_NEEDS_CONFIRM
from agent.models import ModelResponse, ToolCall
from agent.config import AgentConfig, HeadlessConfig
from agent.session import SessionSnapshot

# FakeModelClient from tests/conftest.py
# (imported via pytest fixtures in practice)


@pytest.mark.asyncio
async def test_headless_simple_response(fake_agent_factory):
    """A prompt with a direct text response should exit 0."""
    agent = fake_agent_factory([
        ModelResponse(text="The answer is 42."),
    ])
    config = AgentConfig()
    session = SessionSnapshot(session_id="test", messages=[])

    result = await run_headless("What is the answer?", agent, config, session, store=None)

    assert result.exit_code == EXIT_OK
    assert "42" in result.response
    assert result.turns == 1


@pytest.mark.asyncio
async def test_headless_exits_on_confirmation_without_auto_confirm(fake_agent_factory):
    """Unhandled confirmation request should exit with EXIT_NEEDS_CONFIRM."""
    from agent.events import ConfirmationRequest, ConfirmationKind, DangerLevel

    agent = fake_agent_factory([
        # model response that triggers a confirmation event
        ModelResponse(tool_calls=[ToolCall(id="t1", name="delete_file", input={"path": "/tmp/x"})]),
    ])
    config = AgentConfig(headless=HeadlessConfig(auto_confirm=False))
    session = SessionSnapshot(session_id="test", messages=[])

    result = await run_headless("Delete the temp file", agent, config, session, store=None)

    assert result.exit_code == EXIT_NEEDS_CONFIRM


@pytest.mark.asyncio
async def test_headless_max_turns_stops_loop(fake_agent_factory):
    """--max-turns should stop the agent after N turns."""
    agent = fake_agent_factory([
        ModelResponse(text="Turn 1"),
        ModelResponse(text="Turn 2"),
        ModelResponse(text="Turn 3"),
    ])
    config = AgentConfig(headless=HeadlessConfig(max_turns=1))
    session = SessionSnapshot(session_id="test", messages=[])

    result = await run_headless("Generate content", agent, config, session, store=None)

    assert result.turns == 1


@pytest.mark.asyncio
async def test_headless_writes_output_file(fake_agent_factory, tmp_path):
    """--output should write the response to a file."""
    from agent.headless import write_output, HeadlessResult

    result = HeadlessResult(response="hello from the agent", turns=1)
    out_file = tmp_path / "out.txt"

    write_output(result, output_path=out_file, output_format="text", quiet=True)

    assert out_file.exists()
    assert "hello from the agent" in out_file.read_text()


@pytest.mark.asyncio
async def test_headless_json_output_format(fake_agent_factory):
    """output_format=json should produce valid JSON with the response field."""
    import json
    from agent.headless import write_output, HeadlessResult
    import io

    result = HeadlessResult(response="test response", turns=2, exit_code=0)
    # Capture stdout
    import contextlib, sys

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        write_output(result, output_path=None, output_format="json", quiet=False)

    data = json.loads(captured.getvalue())
    assert data["response"] == "test response"
    assert data["turns"] == 2
    assert data["exit_code"] == 0
```

---

## 13. Add to `agent.toml` for a CI-safe project config

If your project should always run in plan mode when automated, commit this to `agent.toml` so every headless run inherits the safety setting without needing a flag:

```toml
[headless]
deny_mutating = true
output_format = "text"
quiet         = false
```

Then in CI you only need:

```bash
agent --prompt "Review the PR diff" --output review.md --no-session
```

The mutation guard comes from committed config, not from the CI script.

---

## 14. Checklist before moving on

- [ ] `HeadlessConfig` added to `AgentConfig` and loaded from `agent.toml [headless]`
- [ ] `run_headless()` handles all event kinds: `model_response`, `tool_result`, `confirmation_requested`, `tool_denied`, `turn_completed`
- [ ] `resolve_prompt()` handles `--prompt`, `--prompt-file`, and `--stdin` as mutually exclusive
- [ ] `write_output()` produces valid `text`, `json`, and `jsonl` output
- [ ] Exit codes `0`, `1`, `2`, `3` are documented and returned correctly
- [ ] `_apply_cli_overrides()` maps all CLI flags to `AgentConfig` fields in one place
- [ ] `main()` branches on `resolve_prompt()` result — no prompt means REPL, any prompt means headless
- [ ] `build_parser()` marks the three input flags as mutually exclusive
- [ ] `--auto-confirm` and `--mode plan` / `--deny-mutating` are documented as distinct strategies
- [ ] `agent init` creates `agent.toml` and `.agent/` directory
- [ ] `pyproject.toml` registers the `agent` entry point
- [ ] Tests cover: clean response, confirmation exit, max-turns stop, output file write, JSON format

---

## 15. Exercises

**Exercise A — Budget guard in headless**

Add a `--max-cost <usd>` flag. Using the `SessionCostTracker` from Chapter 16, stop the agent loop and exit code 1 when estimated cost exceeds the given amount. Log the overage to stderr.

**Exercise B — Dry-run mode**

Add `--dry-run`. In dry-run mode, build the system prompt, print it, and exit immediately without sending any messages to the model. Useful for debugging context engineering without spending tokens.

**Exercise C — Structured error output**

When `--output-format json` is active and `exit_code != 0`, always include the `error` field in the JSON output. Ensure the calling script never receives an empty JSON object when the run failed.

**Exercise D — Prompt templating**

Add `--var KEY=VALUE` (repeatable). When reading from `--prompt-file`, expand `{{KEY}}` placeholders in the file content before sending to the agent. Useful for parameterized automation like nightly reports with the current date.

---

Next: [18-config-hierarchy.md](18-config-hierarchy.md) — add a two-tier config system (`~/.agent/agent.toml` + `agent.toml`), deep-merge logic, `UIConfig`, and `agent config show global|local|merged`.
