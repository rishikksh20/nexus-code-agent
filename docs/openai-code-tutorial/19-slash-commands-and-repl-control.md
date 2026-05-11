# 19 — Slash Commands: REPL Control and Live Configuration

## Prerequisites

Complete [18-config-hierarchy.md](18-config-hierarchy.md) first.

The interactive REPL built across earlier chapters does one thing: it passes every line of user input to the agent. That is correct for normal conversation. It is the wrong model for operational control.

Consider the things you need to do mid-session today:

- switch the execution mode from `default` to `plan` without restarting,
- see which skills are active,
- load a skill that was not present at startup,
- list or resume a past session without leaving the REPL,
- inspect what the active config says,
- save the current session manually,
- or just clear the screen.

Currently all of these require killing the process and restarting with different flags. That breaks your session history and forces you to re-explain context to the model.

Slash commands fix this. A line that starts with `/` is intercepted before it reaches the agent and dispatched to a command handler instead. The agent never sees it. The session continues.

---

## What you will build

```text
agent/
    slash_commands.py   ← NEW: SlashCommandRouter, SlashCommand, ReplState,
                                all command handlers, build_router()

main.py                 ← updated: intercept "/" lines in the REPL loop
agent/
    config.py           ← unchanged (SlashCommandRouter reads AgentConfig directly)
```

By the end of the chapter, the REPL supports these commands out of the box, and plugins can register their own.

---

## 1. The interception pattern

The entire feature is one conditional in the REPL input loop:

```python
# main.py  — updated REPL loop

async def repl(agent, store, initial_session=None, router=None):
    ...
    while True:
        try:
            line = await asyncio.get_event_loop().run_in_executor(None, input, prompt)
        except (EOFError, KeyboardInterrupt):
            break

        line = line.strip()
        if not line:
            continue

        # ── Slash command interception ────────────────────────────────────
        if line.startswith("/"):
            if router is not None:
                await router.dispatch(line, state)
            else:
                print("No command router registered.")
            continue          # do NOT forward to the agent
        # ─────────────────────────────────────────────────────────────────

        # Normal user message — forward to agent as before
        history.append(Message(role="user", content=line))
        async for event in agent.run(history, mode=state.mode):
            ...
```

Everything else — the agent loop, session management, hooks — is unchanged.

---

## 2. The data structures

```python
# agent/slash_commands.py

from __future__ import annotations

import asyncio
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent.modes import ExecutionMode


# ── ReplState — live harness state ────────────────────────────────────────────

@dataclass
class ReplState:
    """
    A single container for every piece of live harness state that slash
    command handlers might need to read or modify.

    Pass one instance to build_router() and to the REPL loop. Handlers
    mutate it directly instead of returning values.
    """
    config: Any                               # AgentConfig
    mode: ExecutionMode                        # current execution mode
    session: Any                               # SessionSnapshot
    store: Any                                 # SessionStore | None
    tool_registry: Any                         # ToolRegistry
    history: list = field(default_factory=list)    # list[Message]
    active_skills: list[str] = field(default_factory=list)
    disabled_tools: set[str] = field(default_factory=set)


# ── SlashCommand — one registered command ────────────────────────────────────

@dataclass
class SlashCommand:
    """
    Descriptor for a single slash command.

    handler(args: list[str], state: ReplState) -> None | Awaitable[None]
    """
    name: str                        # primary name, e.g. "config"
    description: str
    handler: Callable
    aliases: list[str] = field(default_factory=list)
    usage: str = ""                  # short usage hint shown by /help


# ── SlashCommandRouter ────────────────────────────────────────────────────────

class SlashCommandRouter:
    """
    Maps slash command names to handlers and dispatches input lines.

    Command names may be compound: "/config show" dispatches to the
    handler registered under the key "config show".
    The router tries the longest matching prefix first.
    """

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command
        for alias in command.aliases:
            self._commands[alias] = command

    def commands(self) -> list[SlashCommand]:
        """Return unique commands (no alias duplicates) sorted by name."""
        seen: set[int] = set()
        result = []
        for cmd in self._commands.values():
            if id(cmd) not in seen:
                seen.add(id(cmd))
                result.append(cmd)
        return sorted(result, key=lambda c: c.name)

    async def dispatch(self, line: str, state: ReplState) -> None:
        """
        Parse a slash-prefixed line and call the matching handler.

        /config show global  → try "config show global", then "config show",
                               then "config", then print unknown-command error.
        """
        try:
            parts = shlex.split(line.lstrip("/"))
        except ValueError as e:
            print(f"  Parse error: {e}", file=sys.stderr)
            return

        if not parts:
            await self._handle_help([], state)
            return

        # Try longest compound key first
        for length in range(len(parts), 0, -1):
            key = " ".join(parts[:length])
            cmd = self._commands.get(key)
            if cmd is not None:
                remaining_args = parts[length:]
                result = cmd.handler(remaining_args, state)
                if asyncio.iscoroutine(result):
                    await result
                return

        print(f"  Unknown command: /{parts[0]}  (type /help for a list)")

    async def _handle_help(self, args: list[str], state: ReplState) -> None:
        _print_help(self.commands())
```

---

## 3. All command handlers

Implement one handler per command. Keep each function focused: read or mutate `state`, print feedback, return `None`.

```python
# agent/slash_commands.py  — continued

# ── /help ────────────────────────────────────────────────────────────────────

def _print_help(commands: list[SlashCommand]) -> None:
    print("\nAvailable commands:")
    print(f"  {'Command':<28} {'Description'}")
    print(f"  {'-'*28} {'-'*40}")
    for cmd in commands:
        usage = f"/{cmd.name} {cmd.usage}".strip()
        print(f"  {usage:<28} {cmd.description}")
    print()


def handle_help(args: list[str], state: ReplState) -> None:
    # router calls _handle_help directly; this is a no-op placeholder
    pass


# ── /mode ────────────────────────────────────────────────────────────────────

def handle_mode(args: list[str], state: ReplState) -> None:
    """
    /mode              — show current mode
    /mode plan         — switch to plan mode
    /mode default      — switch to default mode
    /mode auto         — switch to auto mode
    """
    if not args:
        print(f"  Mode: {state.mode.value}")
        return

    new_mode_str = args[0].lower()
    try:
        new_mode = ExecutionMode(new_mode_str)
        state.mode = new_mode
        print(f"  Mode changed to: {new_mode.value}")
    except ValueError:
        valid = [m.value for m in ExecutionMode]
        print(f"  Unknown mode '{new_mode_str}'. Valid: {', '.join(valid)}")


# ── /config ───────────────────────────────────────────────────────────────────

def handle_config_show(args: list[str], state: ReplState) -> None:
    """
    /config show [global|local|merged]
    """
    import dataclasses
    scope = args[0] if args else "merged"
    config = state.config

    if scope == "merged":
        print("\n=== Active config (merged) ===")
        for f in dataclasses.fields(config):
            val = getattr(config, f.name)
            if dataclasses.is_dataclass(val):
                for sf in dataclasses.fields(val):
                    print(f"  [{f.name}] {sf.name} = {getattr(val, sf.name)!r}")
    elif scope in ("global", "local"):
        from pathlib import Path
        from agent.config import _read_toml
        if scope == "global":
            path = Path.home() / ".agent" / "agent.toml"
        else:
            from agent.config import _find_local_config
            path = _find_local_config(Path.cwd()) or Path("agent.toml")
        data = _read_toml(path)
        print(f"\n=== Config ({scope}: {path}) ===")
        _print_nested(data)
    else:
        print(f"  Unknown scope '{scope}'. Use: global, local, merged")
    print()


def _print_nested(data: dict, indent: int = 0) -> None:
    prefix = "  " * (indent + 1)
    for k, v in data.items():
        if isinstance(v, dict):
            print(f"{prefix}[{k}]")
            _print_nested(v, indent + 1)
        else:
            print(f"{prefix}  {k} = {v!r}")


def handle_config_set(args: list[str], state: ReplState) -> None:
    """
    /config set <section>.<key> <value>

    Writes to the local agent.toml and reloads config into state.
    """
    if len(args) < 2:
        print("  Usage: /config set <section>.<key> <value>")
        return

    dotted_key, raw_value = args[0], args[1]
    parts = dotted_key.split(".", 1)
    if len(parts) != 2:
        print(f"  Key must be in section.key format, got: {dotted_key!r}")
        return

    section, key = parts
    from pathlib import Path
    from agent.config import _read_toml, _parse_bool

    local_path = Path("agent.toml")
    data = _read_toml(local_path)
    section_dict = data.setdefault(section, {})

    # Coerce the value to a reasonable Python type
    value: object
    if raw_value.lower() in ("true", "false"):
        value = raw_value.lower() == "true"
    else:
        try:
            value = int(raw_value)
        except ValueError:
            try:
                value = float(raw_value)
            except ValueError:
                value = raw_value

    section_dict[key] = value
    _write_toml(local_path, data)

    # Reload config into state so changes take effect immediately
    from agent.config import load_config
    state.config = load_config(config_path=local_path)
    print(f"  Set [{section}] {key} = {value!r} in {local_path}")
    print(f"  Config reloaded.")


def _write_toml(path, data: dict) -> None:
    """Minimal TOML writer sufficient for simple flat-section config files."""
    lines = []
    for section, content in data.items():
        if isinstance(content, dict):
            lines.append(f"\n[{section}]")
            for k, v in content.items():
                lines.append(f"{k} = {_toml_value(v)}")
        else:
            lines.append(f"{section} = {_toml_value(content)}")
    path.write_text("\n".join(lines).lstrip() + "\n", encoding="utf-8")


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(i) for i in v)
        return f"[{items}]"
    return str(v)


def handle_config_reload(args: list[str], state: ReplState) -> None:
    """/config reload — re-read both config files from disk."""
    from pathlib import Path
    from agent.config import load_config
    state.config = load_config()
    print("  Config reloaded from disk.")


# ── /skills ───────────────────────────────────────────────────────────────────

def handle_skills(args: list[str], state: ReplState) -> None:
    """
    /skills            — list active skills
    /skills list       — same
    /skills add <name> — activate a skill by name
    /skills remove <name> — deactivate a skill
    /skills show <name>  — print the skill's instructions
    /skills reload     — reload skills directory from disk
    """
    sub = args[0] if args else "list"

    if sub == "list":
        if not state.active_skills:
            print("  No skills active.")
        else:
            print("  Active skills:")
            for name in state.active_skills:
                print(f"    • {name}")
        return

    if sub == "add":
        if len(args) < 2:
            print("  Usage: /skills add <name>")
            return
        name = args[1]
        if name in state.active_skills:
            print(f"  Skill '{name}' is already active.")
        else:
            state.active_skills.append(name)
            print(f"  Skill '{name}' activated.")
        return

    if sub == "remove":
        if len(args) < 2:
            print("  Usage: /skills remove <name>")
            return
        name = args[1]
        if name in state.active_skills:
            state.active_skills.remove(name)
            print(f"  Skill '{name}' deactivated.")
        else:
            print(f"  Skill '{name}' was not active.")
        return

    if sub == "show":
        if len(args) < 2:
            print("  Usage: /skills show <name>")
            return
        name = args[1]
        from pathlib import Path
        from agent.config import AgentConfig
        skills_dir = Path(state.config.skills.root)
        skill_file = skills_dir / name / "SKILL.md"
        if not skill_file.exists():
            print(f"  SKILL.md not found for '{name}': {skill_file}")
            return
        print(f"\n=== Skill: {name} ===")
        print(skill_file.read_text(encoding="utf-8"))
        return

    if sub == "reload":
        from pathlib import Path
        from agent.skills import load_skills_from_dir, SkillTool
        skills_dir = Path(state.config.skills.root)
        registry = load_skills_from_dir(skills_dir)
        skill_tool = SkillTool(registry)
        state.tool_registry.register(skill_tool)
        state.active_skills = list(registry.names())
        print(f"  Skills reloaded from {skills_dir}. Active: {state.active_skills}")
        return

    print(f"  Unknown subcommand '{sub}'. Valid: list, add, remove, show, reload")


# ── /session ──────────────────────────────────────────────────────────────────

def handle_session(args: list[str], state: ReplState) -> None:
    """
    /session           — show current session info
    /session list      — list all saved sessions
    /session new       — start a fresh session
    /session resume <id> — resume a session by ID
    /session save      — save current session to disk now
    /session export    — print current session history as JSON
    """
    sub = args[0] if args else "info"

    if sub in ("", "info"):
        snap = state.session
        print(f"  Session ID:  {snap.session_id}")
        print(f"  Messages:    {len(snap.messages)}")
        return

    if sub == "list":
        if state.store is None:
            print("  Session persistence is disabled (--no-session).")
            return
        sessions = state.store.list()
        if not sessions:
            print("  No saved sessions.")
            return
        print("  Saved sessions:")
        for sid in sessions:
            print(f"    {sid}")
        return

    if sub == "new":
        import uuid
        from agent.session import SessionSnapshot
        state.session = SessionSnapshot(session_id=str(uuid.uuid4()), messages=[])
        state.history.clear()
        print(f"  New session started: {state.session.session_id}")
        return

    if sub == "resume":
        if len(args) < 2:
            print("  Usage: /session resume <id>")
            return
        if state.store is None:
            print("  Session persistence is disabled.")
            return
        session_id = args[1]
        try:
            state.session = state.store.load(session_id)
            state.history = list(state.session.messages)
            print(f"  Resumed session: {session_id} ({len(state.history)} messages)")
        except (FileNotFoundError, KeyError):
            print(f"  Session not found: {session_id}")
        return

    if sub == "save":
        if state.store is None:
            print("  Session persistence is disabled.")
            return
        state.session.messages = list(state.history)
        state.store.save(state.session)
        print(f"  Session saved: {state.session.session_id}")
        return

    if sub == "export":
        import json
        data = [{"role": m.role, "content": m.content} for m in state.history]
        print(json.dumps(data, indent=2))
        return

    print(f"  Unknown subcommand '{sub}'. Valid: list, new, resume, save, export")


# ── /tools ────────────────────────────────────────────────────────────────────

def handle_tools(args: list[str], state: ReplState) -> None:
    """
    /tools             — list all registered tools
    /tools list        — same
    /tools info <name> — show description and schema for one tool
    /tools disable <name> — add tool to the disabled set for this session
    /tools enable <name>  — remove tool from the disabled set
    """
    sub = args[0] if args else "list"

    if sub in ("", "list"):
        tools = state.tool_registry.all()
        print(f"  Registered tools ({len(tools)}):")
        for tool in sorted(tools, key=lambda t: t.name):
            status = "[disabled]" if tool.name in state.disabled_tools else ""
            print(f"    {tool.name:<30} {status}")
        return

    if sub == "info":
        if len(args) < 2:
            print("  Usage: /tools info <name>")
            return
        name = args[1]
        tool = state.tool_registry.get(name)
        if tool is None:
            print(f"  Tool not found: {name}")
            return
        print(f"\n  Tool: {tool.name}")
        print(f"  Description: {tool.description}")
        if hasattr(tool, "schema"):
            import json
            print(f"  Schema: {json.dumps(tool.schema, indent=4)}")
        print()
        return

    if sub == "disable":
        if len(args) < 2:
            print("  Usage: /tools disable <name>")
            return
        name = args[1]
        state.disabled_tools.add(name)
        print(f"  Tool '{name}' disabled for this session.")
        return

    if sub == "enable":
        if len(args) < 2:
            print("  Usage: /tools enable <name>")
            return
        name = args[1]
        state.disabled_tools.discard(name)
        print(f"  Tool '{name}' enabled.")
        return

    print(f"  Unknown subcommand '{sub}'. Valid: list, info, disable, enable")


# ── /memory ───────────────────────────────────────────────────────────────────

def handle_memory(args: list[str], state: ReplState) -> None:
    """
    /memory search <terms>  — search memory for matching entries
    /memory list            — list all memory entry IDs
    /memory show <id>       — print the content of one entry
    /memory delete <id>     — delete a memory entry
    /memory save <text>     — save a new memory entry immediately
    """
    from pathlib import Path
    from agent.memory import MemoryStore

    store = MemoryStore(root=Path(state.config.memory.root))
    sub = args[0] if args else "list"

    if sub == "search":
        query = " ".join(args[1:])
        if not query:
            print("  Usage: /memory search <terms>")
            return
        results = store.search(query, max_results=10)
        if not results:
            print("  No matching memory entries.")
        for entry in results:
            print(f"  [{entry.id}] {entry.content[:100]}")
        return

    if sub == "list":
        entries = store.all()
        if not entries:
            print("  Memory is empty.")
        for entry in entries:
            print(f"  [{entry.id}] {entry.content[:80]}")
        return

    if sub == "show":
        if len(args) < 2:
            print("  Usage: /memory show <id>")
            return
        entry = store.get(args[1])
        if entry is None:
            print(f"  Entry not found: {args[1]}")
            return
        print(f"\n  [{entry.id}]\n  {entry.content}\n")
        return

    if sub == "delete":
        if len(args) < 2:
            print("  Usage: /memory delete <id>")
            return
        store.delete(args[1])
        print(f"  Deleted memory entry: {args[1]}")
        return

    if sub == "save":
        text = " ".join(args[1:])
        if not text:
            print("  Usage: /memory save <text>")
            return
        entry = store.save(text)
        print(f"  Saved as [{entry.id}]")
        return

    print(f"  Unknown subcommand '{sub}'. Valid: search, list, show, delete, save")


# ── /history ──────────────────────────────────────────────────────────────────

def handle_history(args: list[str], state: ReplState) -> None:
    """
    /history           — show last 10 messages
    /history <n>       — show last n messages
    /history clear     — clear history (starts fresh turn for the model)
    """
    if args and args[0] == "clear":
        state.history.clear()
        state.session.messages.clear()
        print("  History cleared.")
        return

    n = 10
    if args:
        try:
            n = int(args[0])
        except ValueError:
            print(f"  Expected a number, got '{args[0]}'")
            return

    messages = state.history[-n:]
    if not messages:
        print("  History is empty.")
        return

    print(f"\n  Last {len(messages)} messages:")
    for msg in messages:
        role = msg.role.upper()
        content = str(msg.content or "")[:200]
        print(f"  [{role}] {content}")
    print()


# ── /context ──────────────────────────────────────────────────────────────────

def handle_context(args: list[str], state: ReplState) -> None:
    """
    /context           — show token budget and message count
    /context show      — same
    """
    messages = state.history
    # Rough token estimate: 4 chars per token
    char_count = sum(len(str(m.content or "")) for m in messages)
    estimated_tokens = char_count // 4
    budget = state.config.model.context_window
    used_pct = (estimated_tokens / budget * 100) if budget else 0

    print(f"  Messages in history:    {len(messages)}")
    print(f"  Estimated tokens:       ~{estimated_tokens:,}")
    print(f"  Context window:         {budget:,}")
    print(f"  Budget used (approx):   {used_pct:.1f}%")


# ── /clear ────────────────────────────────────────────────────────────────────

def handle_clear(args: list[str], state: ReplState) -> None:
    """/clear — clear the terminal screen."""
    import os
    os.system("clear" if os.name != "nt" else "cls")


# ── /quit ─────────────────────────────────────────────────────────────────────

def handle_quit(args: list[str], state: ReplState) -> None:
    """/quit — save the session and exit the REPL."""
    if state.store is not None:
        state.session.messages = list(state.history)
        state.store.save(state.session)
        print(f"  Session saved: {state.session.session_id}")
    print("  Goodbye.")
    raise SystemExit(0)
```

---

## 4. Wire all handlers into a router

```python
# agent/slash_commands.py  — add build_router() at the end

def build_router() -> SlashCommandRouter:
    """
    Create and return a SlashCommandRouter with all built-in commands registered.
    Call this once in main() and pass the router to the REPL loop.
    """
    router = SlashCommandRouter()

    router.register(SlashCommand(
        name="help", aliases=["h", "?"],
        description="Show this help message",
        handler=lambda args, state: _print_help(router.commands()),
        usage="",
    ))
    router.register(SlashCommand(
        name="mode",
        description="Show or change the execution mode",
        handler=handle_mode,
        usage="[plan|default|auto]",
    ))
    router.register(SlashCommand(
        name="config show",
        description="Show active configuration",
        handler=handle_config_show,
        usage="[global|local|merged]",
    ))
    router.register(SlashCommand(
        name="config set",
        description="Set a config value in local agent.toml",
        handler=handle_config_set,
        usage="<section>.<key> <value>",
    ))
    router.register(SlashCommand(
        name="config reload",
        description="Reload both config files from disk",
        handler=handle_config_reload,
        usage="",
    ))
    router.register(SlashCommand(
        name="config",
        description="Config subcommands: show, set, reload",
        handler=handle_config_show,   # default: show merged
        usage="<show|set|reload> ...",
    ))
    router.register(SlashCommand(
        name="skills",
        description="Manage skills: list, add, remove, show, reload",
        handler=handle_skills,
        usage="[list|add|remove|show|reload] ...",
    ))
    router.register(SlashCommand(
        name="session",
        description="Manage sessions: list, new, resume, save, export",
        handler=handle_session,
        usage="[list|new|resume|save|export] ...",
    ))
    router.register(SlashCommand(
        name="tools",
        description="Manage tools: list, info, disable, enable",
        handler=handle_tools,
        usage="[list|info|disable|enable] ...",
    ))
    router.register(SlashCommand(
        name="memory",
        description="Inspect memory: search, list, show, delete, save",
        handler=handle_memory,
        usage="[search|list|show|delete|save] ...",
    ))
    router.register(SlashCommand(
        name="history",
        description="Show or clear conversation history",
        handler=handle_history,
        usage="[n|clear]",
    ))
    router.register(SlashCommand(
        name="context",
        description="Show token budget and message count",
        handler=handle_context,
        usage="",
    ))
    router.register(SlashCommand(
        name="clear",
        description="Clear the terminal screen",
        handler=handle_clear,
        usage="",
    ))
    router.register(SlashCommand(
        name="quit", aliases=["exit", "q"],
        description="Save session and exit the REPL",
        handler=handle_quit,
        usage="",
    ))

    return router
```

---

## 5. Update `main.py` to pass the router into the REPL

```python
# main.py — updated interactive REPL setup

async def repl(agent, store, initial_session=None, router=None) -> None:
    from agent.slash_commands import ReplState, build_router
    from agent.modes import ExecutionMode

    router = router or build_router()

    state = ReplState(
        config=config,
        mode=ExecutionMode(config.mode.default),
        session=initial_session or SessionSnapshot(session_id=str(uuid.uuid4()), messages=[]),
        store=store,
        tool_registry=agent.tool_registry,
    )

    prompt_label = state.config.ui.prompt_prefix
    history = list(state.session.messages)
    state.history = history

    while True:
        try:
            session_label = f" [{state.session.session_id[:8]}]" if state.config.ui.show_session_id else ""
            line = await asyncio.get_event_loop().run_in_executor(
                None, input, f"\n{prompt_label}{session_label}> "
            )
        except (EOFError, KeyboardInterrupt):
            print()
            break

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            await router.dispatch(line, state)
            continue

        # Normal message — forward to the agent
        from agent.models import Message
        history.append(Message(role="user", content=line))

        async for event in agent.run(history, mode=state.mode):
            kind = event.get("event")
            if kind == "model_response":
                response = event["value"]
                history.append(response.message)
                label = state.config.ui.response_prefix
                content = response.message.content or ""
                if state.config.ui.stream_tokens:
                    print(f"\n{label}> {content}", flush=True)
                else:
                    print(f"\n{label}> {content}")
            elif kind == "tool_result":
                result = event["value"]
                if state.config.ui.show_tool_names:
                    print(f"  [tool:{result.tool_name}] {str(result.output)[:200]}")
            elif kind == "confirmation_requested":
                req = event["value"]
                answer = input(f"  Confirm: {req.prompt} [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("  Denied.")

    # Save on exit
    if store is not None:
        state.session.messages = list(history)
        store.save(state.session)
```

---

## 6. The complete command reference

```
/help                             Show this list
/mode [plan|default|auto]         Show or switch execution mode
/config show [global|local]       Show configuration
/config set <section>.<key> <v>   Write a value to local agent.toml
/config reload                    Reload config from disk
/skills list                      List active skills
/skills add <name>                Activate a skill
/skills remove <name>             Deactivate a skill
/skills show <name>               Print a skill's SKILL.md content
/skills reload                    Reload skills directory
/session                          Show current session info
/session list                     List all saved sessions
/session new                      Start a fresh session
/session resume <id>              Resume a saved session
/session save                     Manually save now
/session export                   Print history as JSON
/tools list                       List registered tools
/tools info <name>                Show tool schema
/tools disable <name>             Disable a tool for this session
/tools enable <name>              Re-enable a tool
/memory search <terms>            Search memory entries
/memory list                      List all entries
/memory show <id>                 Print one entry
/memory delete <id>               Delete one entry
/memory save <text>               Save a new entry
/history [n]                      Show last n messages (default 10)
/history clear                    Clear history
/context                          Show token budget and usage
/clear                            Clear the terminal screen
/quit  (or /exit, /q)             Save session and exit
```

---

## 7. Extending with plugin commands

Plugins loaded in Chapter 02-2 can register their own slash commands. The pattern is simple: call `router.register()` with a `SlashCommand` from within the plugin's `setup()` function.

```python
# plugins/my_plugin.py  — example plugin registering a slash command

def setup(registry, hook_executor, router=None):
    if router is not None:
        from agent.slash_commands import SlashCommand

        def handle_deploy(args, state):
            env = args[0] if args else "staging"
            print(f"  [deploy] Triggering deploy to {env}...")
            # call your deploy API here

        router.register(SlashCommand(
            name="deploy",
            description="Trigger a deployment",
            handler=handle_deploy,
            usage="[staging|production]",
        ))
```

Update `main.py`'s plugin loading to pass the router:

```python
if config.plugins.root:
    load_plugins(
        registry=registry,
        hook_executor=executor,
        plugins_dir=Path(config.plugins.root),
        allow_list=config.plugins.allow_list or None,
        router=router,          # pass the router so plugins can register commands
    )
```

---

## 8. Testing slash commands

```python
# tests/test_slash_commands.py

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock
import pytest

from agent.slash_commands import build_router, ReplState
from agent.modes import ExecutionMode


def make_state(config=None, mode=None):
    cfg = config or MagicMock()
    cfg.skills = MagicMock(root="skills")
    cfg.memory = MagicMock(root=".agent-memory")
    cfg.model = MagicMock(context_window=128000)
    cfg.ui = MagicMock(show_session_id=False)
    session = MagicMock()
    session.session_id = "test-session-id"
    session.messages = []
    return ReplState(
        config=cfg,
        mode=mode or ExecutionMode("default"),
        session=session,
        store=None,
        tool_registry=MagicMock(),
    )


@pytest.mark.asyncio
async def test_mode_switch():
    router = build_router()
    state = make_state()

    await router.dispatch("/mode plan", state)

    assert state.mode == ExecutionMode("plan")


@pytest.mark.asyncio
async def test_mode_invalid():
    router = build_router()
    state = make_state()
    original_mode = state.mode

    await router.dispatch("/mode invalid_mode", state)

    assert state.mode == original_mode  # unchanged


@pytest.mark.asyncio
async def test_history_clear():
    from agent.models import Message
    router = build_router()
    state = make_state()
    state.history = [Message(role="user", content="hello")]
    state.session.messages = list(state.history)

    await router.dispatch("/history clear", state)

    assert state.history == []


@pytest.mark.asyncio
async def test_unknown_command(capsys):
    router = build_router()
    state = make_state()

    await router.dispatch("/totally_unknown_command", state)

    captured = capsys.readouterr()
    assert "Unknown command" in captured.out or "Unknown command" in captured.err


@pytest.mark.asyncio
async def test_compound_command_routing():
    """'/config show' should route to handle_config_show, not handle_config."""
    router = build_router()
    state = make_state()
    called_with = []

    from agent.slash_commands import SlashCommand
    router.register(SlashCommand(
        name="config show",
        description="test override",
        handler=lambda args, state: called_with.append(args),
    ))

    await router.dispatch("/config show global", state)
    assert called_with == [["global"]]


@pytest.mark.asyncio
async def test_session_new_clears_history():
    router = build_router()
    from agent.models import Message
    state = make_state()
    state.history = [Message(role="user", content="old message")]

    await router.dispatch("/session new", state)

    assert state.history == []
    assert state.session.messages == []
```

---

## 9. Checklist before moving on

- [ ] `/`-prefixed lines are intercepted before the agent sees them
- [ ] Unknown commands print a helpful error and do not crash
- [ ] Compound commands (`/config show`, `/config set`) route to the right handler
- [ ] `/mode plan` changes `state.mode` in-place; subsequent agent calls use the new mode
- [ ] `/config set` writes to `agent.toml` and reloads config into `state.config`
- [ ] `/config reload` re-reads both config files
- [ ] `/session resume <id>` replaces `state.session` and `state.history`
- [ ] `/tools disable <name>` adds to `state.disabled_tools`; the agent loop must respect this set
- [ ] `/memory save` works without the agent being involved
- [ ] `/history clear` clears both `state.history` and `state.session.messages`
- [ ] `/quit` saves the session before calling `SystemExit`
- [ ] Plugins can register commands by calling `router.register()` in their `setup()`
- [ ] Tests cover: mode switch, history clear, unknown command, compound routing, session new

---

## 10. Exercises

**Exercise A — /status**

Add `/status` that prints a one-line summary of the most important live state:

```
mode: plan  |  session: abc12345  |  messages: 14  |  tokens: ~3,200  |  cost: ~$0.02
```

Pull the cost from `SessionCostTracker` added in Chapter 16.

**Exercise B — /undo**

Add `/undo` that removes the last user+assistant message pair from `state.history`. Useful when the agent went in the wrong direction and you want to try a different prompt.

**Exercise C — Aliases from config**

Let the user define command aliases in `agent.toml`:

```toml
[aliases]
p = "mode plan"
d = "mode default"
```

Load these in `build_router()` and register each alias. Dispatching `/p` should behave exactly like `/mode plan`.

**Exercise D — Tab completion**

Integrate Python's `readline` module to provide tab completion for slash command names. Register completers for known command prefixes so `/con<Tab>` expands to `/config`.

**Exercise E — Command history**

Store each executed slash command in a list. Add `/commands` that prints the last 20 slash commands issued in the current session. Useful for reproducing a sequence of mode changes or skill activations.

---

Next: See [README.md](README.md) for the full series index.
