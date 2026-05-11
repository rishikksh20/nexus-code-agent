# Chapter 22: Slash Commands And REPL Control

## Objective

Give the REPL a first-class command system so users can manage config, skills, sessions, tools, memory, and execution mode without leaving the session. Slash commands keep the agent's operational surface accessible during a conversation rather than requiring restarts or separate CLI invocations.

## Why Slash Commands Belong In The REPL

An agent harness is not a black box. While a session is running you should be able to:

- inspect the active config and change a value without restarting
- switch execution mode mid-session when the task changes
- add or remove a skill for the current conversation
- list registered tools and disable one temporarily
- search memory and save a note inline
- start or resume a session without exiting

Slash commands are the surface for all of that. They are distinguishable from agent prompts (they start with `/`), they execute locally without a model call, and they give immediate feedback.

## Current Nexus Notes

The current Nexus runtime now implements a comprehensive slash-command surface:

**Fully implemented handlers:** `/config`, `/mode`, `/skills`, `/session`, `/tools`, `/memory`, `/context`, `/history`, `/mcp`, `/delegate`, `/provider`, `/help`, `/quit`, `/exit`

**Key additions since the original continuity pass:**

- every slash command now accepts `help` as a subcommand (e.g. `/context help`, `/mode help`), which prints a Rich table listing all subcommands, what each does, and a usage example — consistent across all 11 command handlers
- `/context usage` — shows a formatted table with the active provider, model, known context window size (from the built-in model-limits table), estimated token counts for the current system prompt and message history, compaction soft/hard thresholds, and the total percentage of context window consumed
- `/context show` — prints the assembled system prompt (was the only `/context` behaviour before)
- unknown slash commands (e.g. `/anything`) are no longer rejected with an error; they are forwarded to the agent as a natural-language query, so users do not need to rephrase input just because it starts with `/`
- the REPL startup banner now shows the active provider, model, and mode; if no API key is detected for a live provider, a warning is printed before the first prompt
- `/provider list|set <param> <value>` is fully implemented for hot-reloading provider, model, temperature, and other settings without restarting
- agent responses are rendered as **Rich Markdown** rather than raw text — headers, bold, inline code, fenced code blocks (with syntax highlighting), tables, and horizontal rules are all styled; tool calls appear in dim cyan with `⚙ tool_name args`, tool results appear as a truncated dim preview (`↳ …`), and denied actions are shown in bold red
- `stream_output = true` (default) uses a word-by-word animated reveal via Rich `Live` + `Markdown`; `stream_output = false` renders the full response instantly

**`/config reinit [local|global]`** rewrites the target config file to clean Nexus defaults and reloads the merged config immediately. `reinit` (no scope argument) targets the local `.nexus/config.toml`; `reinit global` targets `~/.nexus/config.toml`. Sessions, memory, and knowledge are not affected.

**Tool toggling** (`/tools enable|disable`) and `/config init` remain next-step improvements rather than fully completed runtime behaviour.

---

## Command Reference

Every slash command starts with `/`. Unrecognised commands print the help hint.

### Config Commands

```
/config show                     Show the fully merged active config
/config show global              Show only the global config file contents
/config show local               Show only the local config file contents
/config set <key> <value>        Write a key to the local .agent/config.toml
/config global set <key> <value> Write a key to the global ~/.agent/config.toml
/config reset <key>              Remove a key override from the local config
/config init                     Create default config files if they do not exist
/config reload                   Re-read and merge config files without restarting
```

### Mode Commands

```
/mode                            Show the current execution mode
/mode plan                       Switch to plan mode (read-only, no mutations)
/mode default                    Switch to default mode (confirm mutations)
/mode auto                       Switch to auto mode (allow more without prompting)
```

### Skills Commands

```
/skills                          List all loaded skills
/skills list                     Same as /skills
/skills show <name>              Print the full content of a skill
/skills add <name>               Activate a skill for this session
/skills remove <name>            Deactivate a skill from this session
/skills reload                   Re-scan the skills directory and reload registry
```

### Sessions Commands

```
/session                         Show the current session ID and turn count
/session new                     Start a fresh session (saves current first)
/session list                    List saved sessions with timestamps
/session resume <id>             Load a prior session and continue it
/session save                    Force-save the current session now
/session export <file>           Write the current session messages to a file
```

### Tools Commands

```
/tools                           List all registered tools with mutating flag
/tools list                      Same as /tools
/tools enable <name>             Enable a previously disabled tool
/tools disable <name>            Disable a tool for this session only
/tools info <name>               Show name, description, and mutating flag
```

### Memory Commands

```
/memory search <query>           Search memory for entries matching the query
/memory save <key> <content>     Save a new memory entry
/memory list                     List all memory entry keys
/memory show <key>               Print a memory entry
/memory delete <key>             Delete a memory entry
```

### Context Commands

```
/context show                    Print the current assembled system prompt
/context sections                List the active context section names
/history                         Show the message history for this session
/history <n>                     Show the last n messages
```

### Utility Commands

```
/help                            Show this command list
/help <command>                  Show detailed help for a specific command
/clear                           Clear the terminal screen
/version                         Print the harness version
/quit                            Save the session and exit
/exit                            Same as /quit
```

---

## Building The Slash Command Router

Separate command parsing from execution. The router maps command names to handlers. Each handler receives the remaining tokens as arguments.

```python
# runtime/slash_commands.py
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runtime.repl_state import ReplState


CommandHandler = Callable[["ReplState", list[str]], Awaitable[None]]


@dataclass(slots=True)
class SlashCommand:
    name: str          # primary name, e.g. "config"
    aliases: tuple[str, ...]
    description: str
    handler: CommandHandler


class SlashCommandRouter:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command
        for alias in command.aliases:
            self._commands[alias] = command

    async def dispatch(self, state: ReplState, raw_input: str) -> bool:
        """
        Try to dispatch raw_input as a slash command.
        Returns True if it was a slash command, False if it should be
        forwarded to the agent as a regular prompt.
        """
        if not raw_input.startswith("/"):
            return False

        parts = raw_input[1:].split()
        if not parts:
            return True

        top = parts[0].lower()
        sub = parts[1].lower() if len(parts) > 1 else ""
        args = parts[2:]

        # Try compound key first (e.g. "config show"), then single key
        compound_key = f"{top} {sub}" if sub else ""
        if compound_key and compound_key in self._commands:
            await self._commands[compound_key].handler(state, args)
        elif top in self._commands:
            # Pass sub and args together so top-level handlers can dispatch further
            await self._commands[top].handler(state, parts[1:])
        else:
            print(f"Unknown command: /{top}. Type /help to see available commands.")

        return True
```

---

## The REPL State Object

Slash commands need access to live harness state — the config, skill registry, session, tool registry, and execution mode. Bundle that into one object the router can pass to each handler.

```python
# runtime/repl_state.py
from dataclasses import dataclass, field

from config.loader import AgentConfig
from models import Message
from runtime.execution_modes import ExecutionMode
from runtime.sessions import SessionSnapshot
from tools.registry import ToolRegistry


@dataclass(slots=True)
class ReplState:
    config: AgentConfig
    mode: ExecutionMode
    session: SessionSnapshot
    tool_registry: ToolRegistry
    history: list[Message] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)
    disabled_tools: set[str] = field(default_factory=set)
```

---

## Implementing Key Handlers

### `/config show`

```python
import json
import tomllib
from pathlib import Path


async def handle_config_show(state: ReplState, args: list[str]) -> None:
    scope = args[0].lower() if args else "merged"

    if scope == "global":
        path = Path.home() / ".agent" / "config.toml"
        label = "Global config"
    elif scope == "local":
        path = Path.cwd() / ".agent" / "config.toml"
        label = "Local config"
    else:
        # Print the fully merged config as the dataclass fields
        import dataclasses
        print("=== Active merged config ===")
        for f in dataclasses.fields(state.config):
            print(f"  {f.name} = {getattr(state.config, f.name)!r}")
        return

    if path.exists():
        print(f"=== {label}: {path} ===")
        print(path.read_text(encoding="utf-8"))
    else:
        print(f"{label} file not found: {path}")
```

### `/config set <key> <value>`

```python
async def handle_config_set(state: ReplState, args: list[str]) -> None:
    if len(args) < 2:
        print("Usage: /config set <key> <value>")
        return

    key, value = args[0], " ".join(args[1:])
    path = Path.cwd() / ".agent" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing content, update in place, write back
    existing = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    # Attempt type coercion to match likely TOML intent
    if value.lower() in {"true", "false"}:
        existing[key] = value.lower() == "true"
    elif value.isdigit():
        existing[key] = int(value)
    else:
        existing[key] = value

    # Write using tomli_w or format manually for simplicity
    lines = [f'{k} = {_toml_value(v)}' for k, v in existing.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Set {key} = {existing[key]!r} in local config. Use /config reload to apply.")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(f'"{x}"' for x in v) + "]"
    return f'"{v}"'
```

> **Note:** For production use, install `tomli-w` (`pip install tomli-w`) and replace the manual serializer with `tomli_w.dumps(existing)`. The manual version above handles the common cases but does not support nested tables.

### `/config reload`

```python
from config.loader import load_config


async def handle_config_reload(state: ReplState, args: list[str]) -> None:
    new_config = load_config(workspace_root=Path.cwd())
    # Mutate state in place so all subsystems see the new values immediately
    for f in dataclasses.fields(new_config):
        object.__setattr__(state.config, f.name, getattr(new_config, f.name))
    print("Config reloaded.")
```

### `/mode <name>`

```python
async def handle_mode(state: ReplState, args: list[str]) -> None:
    if not args:
        print(f"Current mode: {state.mode.value}")
        return

    name = args[0].lower()
    try:
        state.mode = ExecutionMode(name)
        print(f"Mode switched to: {state.mode.value}")
    except ValueError:
        print(f"Unknown mode: {name}. Valid values: plan, default, auto")
```

### `/skills list` and `/skills add <name>`

```python
async def handle_skills(state: ReplState, args: list[str]) -> None:
    sub = args[0].lower() if args else "list"

    if sub in {"list", ""}:
        registry = state.config  # assumes skill_registry is on state in practice
        print("Available skills:")
        # In practice: iterate state.skill_registry
        for name in state.active_skills:
            print(f"  * {name}  (active)")
        return

    if sub == "add":
        if len(args) < 2:
            print("Usage: /skills add <name>")
            return
        name = args[1]
        if name not in state.active_skills:
            state.active_skills.append(name)
            print(f"Skill '{name}' activated for this session.")
        else:
            print(f"Skill '{name}' is already active.")
        return

    if sub == "remove":
        if len(args) < 2:
            print("Usage: /skills remove <name>")
            return
        name = args[1]
        if name in state.active_skills:
            state.active_skills.remove(name)
            print(f"Skill '{name}' removed from this session.")
        else:
            print(f"Skill '{name}' is not currently active.")
        return

    if sub == "reload":
        print("Skills directory rescanned.")  # trigger reload via hook in practice
        return
```

### `/session list` and `/session resume <id>`

```python
import json


async def handle_session(state: ReplState, args: list[str]) -> None:
    sub = args[0].lower() if args else "show"

    if sub == "list":
        session_dir = state.config.session_dir
        sessions = sorted(session_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not sessions:
            print("No saved sessions.")
            return
        print(f"{'Session ID':<36}  {'Last modified'}")
        print("-" * 58)
        for p in sessions[:20]:
            import datetime
            mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            print(f"{p.stem:<36}  {mtime}")
        return

    if sub == "new":
        # Save current session then reset
        from runtime.sessions import SessionStore, new_snapshot
        import uuid
        store = SessionStore(state.config.session_dir)
        store.save(state.session)
        state.session = new_snapshot(str(uuid.uuid4()))
        state.history.clear()
        print(f"New session started: {state.session.session_id}")
        return

    if sub == "resume":
        if len(args) < 2:
            print("Usage: /session resume <session-id>")
            return
        from runtime.sessions import SessionStore
        store = SessionStore(state.config.session_dir)
        try:
            loaded = store.load(args[1])
            state.session = loaded
            state.history = list(loaded.messages)
            print(f"Resumed session: {loaded.session_id} ({len(loaded.messages)} messages)")
        except FileNotFoundError:
            print(f"Session not found: {args[1]}")
        return
```

### `/tools list` and `/tools disable <name>`

```python
async def handle_tools(state: ReplState, args: list[str]) -> None:
    sub = args[0].lower() if args else "list"

    if sub == "list":
        print(f"{'Name':<24}  {'Mutating':<8}  Status")
        print("-" * 48)
        for tool in state.tool_registry.all():
            status = "disabled" if tool.name in state.disabled_tools else "active"
            mutating = "yes" if tool.is_mutating else "no"
            print(f"{tool.name:<24}  {mutating:<8}  {status}")
        return

    if sub == "disable":
        if len(args) < 2:
            print("Usage: /tools disable <name>")
            return
        state.disabled_tools.add(args[1])
        print(f"Tool '{args[1]}' disabled for this session.")
        return

    if sub == "enable":
        if len(args) < 2:
            print("Usage: /tools enable <name>")
            return
        state.disabled_tools.discard(args[1])
        print(f"Tool '{args[1]}' re-enabled.")
        return
```

---

## Wiring It Into The REPL

The REPL input loop checks every line before forwarding to the agent:

```python
async def repl(state: ReplState, agent: Agent, router: SlashCommandRouter) -> None:
    print(f"Agent ready. Mode: {state.mode.value}. Type /help for commands.\n")

    while True:
        try:
            user_text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break

        if not user_text:
            continue

        # Try slash command first
        handled = await router.dispatch(state, user_text)
        if handled:
            continue

        # Forward to agent
        state.history.append(Message(role="user", content=user_text))
        async for event in agent.run(state.history, build_context(state)):
            render_event(event, state.config)
```

The agent never sees slash commands. The router intercepts them transparently.

---

## Registering All Handlers

Wire up at startup:

```python
def build_router() -> SlashCommandRouter:
    router = SlashCommandRouter()

    router.register(SlashCommand("config", (), "Manage configuration", handle_config))
    router.register(SlashCommand("mode", (), "Switch execution mode", handle_mode))
    router.register(SlashCommand("skills", ("skill",), "Manage skills", handle_skills))
    router.register(SlashCommand("session", ("sessions",), "Manage sessions", handle_session))
    router.register(SlashCommand("tools", ("tool",), "Manage tools", handle_tools))
    router.register(SlashCommand("memory", ("mem",), "Search and manage memory", handle_memory))
    router.register(SlashCommand("history", ("hist",), "Show turn history", handle_history))
    router.register(SlashCommand("context", ("ctx",), "Inspect context", handle_context))
    router.register(SlashCommand("help", ("?",), "Show help", handle_help))
    router.register(SlashCommand("clear", ("cls",), "Clear screen", handle_clear))
    router.register(SlashCommand("quit", ("exit", "q"), "Exit", handle_quit))
    router.register(SlashCommand("version", (), "Print version", handle_version))

    return router
```

---

## Making `/help` Useful

```python
async def handle_help(state: ReplState, args: list[str]) -> None:
    if args:
        # Specific command help
        cmd_name = args[0].lower()
        # In practice look up the command and print its extended help string
        print(f"Help for /{cmd_name}: not yet documented.")
        return

    print("""
Available slash commands:

  Config
    /config show [global|local]       Show config
    /config set <key> <value>         Set local config key
    /config global set <key> <value>  Set global config key
    /config reset <key>               Remove local key override
    /config reload                    Reload config files
    /config init                      Create default config files

  Mode
    /mode [plan|default|auto]         Show or switch execution mode

  Skills
    /skills list                      List skills
    /skills add <name>                Activate a skill
    /skills remove <name>             Deactivate a skill
    /skills show <name>               Print skill content
    /skills reload                    Rescan skills directory

  Sessions
    /session                          Show current session
    /session list                     List saved sessions
    /session new                      Start a new session
    /session resume <id>              Resume a session
    /session save                     Force save now
    /session export <file>            Export session to file

  Tools
    /tools list                       List registered tools
    /tools disable <name>             Disable a tool this session
    /tools enable <name>              Re-enable a tool
    /tools info <name>                Show tool details

  Memory
    /memory search <query>            Search memory
    /memory save <key> <content>      Save a memory entry
    /memory list                      List memory keys
    /memory show <key>                Print a memory entry
    /memory delete <key>              Delete a memory entry

  Context and History
    /context show                     Print assembled system prompt
    /history [n]                      Show last n messages

  Utility
    /help [command]                   Show this help
    /clear                            Clear screen
    /version                          Print version
    /quit                             Save and exit
""")
```

---

## Adding Custom Slash Commands Via Plugins

Plugins can extend the router the same way they extend the tool registry. A plugin's `register()` function receives the router and can add new commands:

```python
# .agent/plugins/my_plugin.py
from runtime.slash_commands import SlashCommand


async def handle_my_command(state, args):
    print("Custom plugin command executed.")


def register(registry, hooks, router=None):
    if router is not None:
        router.register(SlashCommand(
            name="mycommand",
            aliases=("mc",),
            description="A custom command from my_plugin",
            handler=handle_my_command,
        ))
```

This keeps the slash command surface extensible without changing core harness code.

---

## Action Plan

1. Define `ReplState` as the single container for live harness state.
2. Implement `SlashCommandRouter` with `register()` and `dispatch()`.
3. Implement handlers for config, mode, skills, session, tools, memory, and help.
4. Wire the router into the REPL input loop before forwarding to the agent.
5. Add `/help` with a full command table.
6. Allow plugins to register additional slash commands through `register()`.
7. Test that an unrecognised `/xyz` prints a useful hint instead of crashing.

## Validation Checklist

- `/mode auto` changes `state.mode` immediately without restarting.
- `/config set model_name gpt-4o` writes to the local config file.
- `/skills add refactoring` adds the skill to `state.active_skills`.
- `/session list` shows sessions sorted by most recent first.
- `/tools disable run_command` prevents that tool from running even in auto mode.
- `/help` output is complete and unambiguous.
- A plugin can add a new slash command without touching core router code.

## Definition Of Done

This chapter is complete when a user can manage the full operational state of their harness without leaving the REPL. If any operational action requires a restart or a separate terminal command, there is a gap still to fill.
