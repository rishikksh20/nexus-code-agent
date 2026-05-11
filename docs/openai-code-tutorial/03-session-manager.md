# 03 — Session Manager: Persist, Resume, and Export Conversations

## Prerequisites

Complete [02-tools.md](02-tools.md) first.

Your project should look like this:

```
agent/
    __init__.py
    models.py      ← Message, ToolCall, ModelResponse, ToolResult, ToolExecutionContext
    tools.py       ← BaseTool, ToolRegistry, all tools
    events.py      ← all event dataclasses
    adapters.py    ← OpenAI wire-format bridge
    client.py      ← DemoModelClient, OpenAIModelClient
    agent.py       ← Agent class with context-aware tool execution
main.py
```

Right now, every time you run `python main.py` the agent starts from a blank slate. If you `quit` and reopen the terminal, everything the agent learned or did is gone.

This chapter fixes that by adding a **session manager** — the layer that saves conversations to disk, loads them back, and exports human-readable transcripts.

---

## What you will build

After this chapter:

```
agent/
    ...
    session.py     ← SessionSnapshot dataclass + SessionStore class
main.py            ← updated with --continue / --resume / --export flags
sessions/          ← directory created at runtime
    session-abc123.json
    session-def456.json
    latest.json    ← alias to the most recent session
    transcript-abc123.md   ← human-readable export
```

You will be able to run:

```bash
python main.py                    # start a fresh session
python main.py --continue         # resume the last session
python main.py --resume abc123    # resume a specific session by ID
python main.py --export abc123    # write a readable transcript and exit
```

---

## 1. Three kinds of state — only two belong in a snapshot

Before writing any code, be clear about what to save and what not to save. Getting this wrong leads to serialization bugs that are hard to debug.

```
┌──────────────────────────────────────────────────────────────┐
│  SESSION STATE         → always save                         │
│  What happened in the conversation                           │
│  • messages (user, assistant, tool_call, tool_result)        │
│  • model name / system prompt                                │
│  • working directory                                         │
│  • turn count, timestamps                                    │
├──────────────────────────────────────────────────────────────┤
│  CARRY-OVER STATE      → save selectively                    │
│  What the runtime learned and wants to remember              │
│  • recently read/written files                               │
│  • current task summary                                      │
│  • any flags the user set during the session                 │
├──────────────────────────────────────────────────────────────┤
│  PROCESS STATE         → never save                          │
│  Only meaningful while the program is alive                  │
│  • open file handles                                         │
│  • asyncio tasks                                             │
│  • live callbacks (context.ask_user)                         │
│  • network sockets                                           │
└──────────────────────────────────────────────────────────────┘
```

**The restore pattern:** rebuild process state from scratch, hydrate session + carry-over state from the snapshot.

---

## 2. Design the snapshot shape

A snapshot is a plain JSON document. Plain JSON means:

- you can `cat sessions/latest.json` to see what is going on
- it survives Python version changes
- you can write migration scripts if the format changes
- it is easy to write tests for

Here is what one fully-saved session looks like:

```json
{
  "session_id": "abc12345ef67",
  "created_at": "2026-04-24T10:44:21Z",
  "updated_at": "2026-04-24T10:51:03Z",
  "cwd": "/home/user/project",
  "model": "demo",
  "system_prompt": "You are a helpful assistant.",
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "what time is it?"}]},
    {"role": "assistant", "content": [{"type": "text", "text": ""}]},
    {
      "role": "user",
      "content": [
        {"type": "tool_result", "tool_call_id": "tc-001", "content": "2026-04-24 10:44:21 UTC"}
      ]
    }
  ],
  "usage": {"turns": 1, "tool_calls": 1},
  "carry_over": {"last_read_file": null, "task_summary": ""},
  "summary": "what time is it?"
}
```

Notice:
- `messages` stores your **internal** `Message` format, not raw OpenAI JSON
- `usage` tracks simple counters — no provider-specific billing data
- `carry_over` holds a small metadata dict that survives restart
- `summary` is the first user message — useful for listing sessions

---

## 3. Add `SessionSnapshot` to `agent/models.py`

Open `agent/models.py` and add this dataclass at the bottom:

```python
# agent/models.py  — add at the bottom

from datetime import datetime, timezone
from uuid import uuid4


@dataclass
class SessionSnapshot:
    """
    The durable representation of one agent session.

    This is what gets written to disk and read back on resume.
    Do NOT store process state here — only data that survives restart.
    """
    session_id: str
    cwd: str
    model: str
    system_prompt: str
    messages: list[dict[str, Any]]          # serialized Message objects
    usage: dict[str, int]                   # turn count, tool call count, etc.
    carry_over: dict[str, Any]              # selective runtime metadata
    summary: str                            # first user message text, for listings
    created_at: str                         # ISO 8601
    updated_at: str                         # ISO 8601

    @classmethod
    def new(cls, *, cwd: str, model: str, system_prompt: str) -> "SessionSnapshot":
        """Create a brand-new blank session."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(
            session_id=uuid4().hex[:12],
            cwd=cwd,
            model=model,
            system_prompt=system_prompt,
            messages=[],
            usage={"turns": 0, "tool_calls": 0},
            carry_over={"last_read_file": None, "task_summary": ""},
            summary="",
            created_at=now,
            updated_at=now,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for JSON storage."""
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cwd": self.cwd,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "messages": self.messages,
            "usage": self.usage,
            "carry_over": self.carry_over,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionSnapshot":
        """Deserialize from a plain dict (loaded from JSON)."""
        return cls(
            session_id=data["session_id"],
            cwd=data.get("cwd", ""),
            model=data.get("model", "demo"),
            system_prompt=data.get("system_prompt", ""),
            messages=data.get("messages", []),
            usage=data.get("usage", {"turns": 0, "tool_calls": 0}),
            carry_over=data.get("carry_over", {}),
            summary=data.get("summary", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )
```

**Why `to_dict` / `from_dict` instead of `dataclasses.asdict`?**

`dataclasses.asdict` recurses into every nested object — if any field contains a non-serializable type (like `datetime`), it crashes. Explicit `to_dict` / `from_dict` keep you in control of exactly what enters and leaves the JSON file.

---

## 4. Create `agent/session.py` — the `SessionStore`

Create a new file `agent/session.py`:

```python
# agent/session.py

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from agent.models import Message, SessionSnapshot


class SessionStore:
    """
    Saves and loads session snapshots as JSON files.

    Directory layout:
        {root}/
            session-{id}.json    ← one file per session
            latest.json          ← copy of the most recently saved session
    """

    def __init__(self, root: Path = Path("sessions")) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # ── Save ──────────────────────────────────────────────────────────────────

    def save(self, snapshot: SessionSnapshot) -> Path:
        """
        Write the snapshot to disk.

        Always updates:
        - sessions/session-{id}.json  (the canonical file)
        - sessions/latest.json        (alias for --continue)
        """
        # Stamp the update time
        snapshot.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        data = json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False) + "\n"

        # Write canonical session file
        session_path = self.root / f"session-{snapshot.session_id}.json"
        session_path.write_text(data, encoding="utf-8")

        # Update the latest alias
        latest_path = self.root / "latest.json"
        latest_path.write_text(data, encoding="utf-8")

        return session_path

    # ── Load ──────────────────────────────────────────────────────────────────

    def load_latest(self) -> SessionSnapshot | None:
        """Return the most recently saved session, or None if no sessions exist."""
        path = self.root / "latest.json"
        if not path.exists():
            return None
        return self._read(path)

    def load_by_id(self, session_id: str) -> SessionSnapshot | None:
        """Return a session by its ID, or None if not found."""
        path = self.root / f"session-{session_id}.json"
        if not path.exists():
            return None
        return self._read(path)

    def list_sessions(self) -> list[SessionSnapshot]:
        """Return all sessions sorted by updated_at descending (newest first)."""
        snapshots = []
        for path in self.root.glob("session-*.json"):
            try:
                snapshots.append(self._read(path))
            except (json.JSONDecodeError, KeyError):
                continue  # skip corrupt files
        snapshots.sort(key=lambda s: s.updated_at, reverse=True)
        return snapshots

    # ── Transcript export ─────────────────────────────────────────────────────

    def export_transcript(self, session_id: str) -> Path | None:
        """
        Write a human-readable Markdown transcript of a session.

        Returns the path of the created file, or None if session not found.
        """
        snapshot = self.load_by_id(session_id)
        if snapshot is None:
            return None

        lines = [
            f"# Session Transcript: {session_id}",
            f"",
            f"- **Created:** {snapshot.created_at}",
            f"- **Updated:** {snapshot.updated_at}",
            f"- **Model:** {snapshot.model}",
            f"- **Working directory:** {snapshot.cwd}",
            f"- **Turns:** {snapshot.usage.get('turns', 0)}",
            f"- **Tool calls:** {snapshot.usage.get('tool_calls', 0)}",
            f"",
            f"---",
            f"",
        ]

        for msg in snapshot.messages:
            role = msg.get("role", "unknown")
            for block in msg.get("content", []):
                block_type = block.get("type", "")

                if block_type == "text" and block.get("text"):
                    prefix = "**You:**" if role == "user" else "**Agent:**"
                    lines.append(f"{prefix}")
                    lines.append(f"")
                    lines.append(block["text"])
                    lines.append(f"")
                    lines.append("---")
                    lines.append(f"")

                elif block_type == "tool_result":
                    tool_id = block.get("tool_call_id", "?")
                    content = block.get("content", "")
                    lines.append(f"**Tool result** (`{tool_id}`):")
                    lines.append(f"")
                    lines.append(f"```")
                    lines.append(content[:500])   # truncate very long outputs
                    if len(content) > 500:
                        lines.append(f"... [{len(content) - 500} more characters]")
                    lines.append(f"```")
                    lines.append(f"")
                    lines.append("---")
                    lines.append(f"")

        transcript_path = self.root / f"transcript-{session_id}.md"
        transcript_path.write_text("\n".join(lines), encoding="utf-8")
        return transcript_path

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete(self, session_id: str) -> bool:
        """Delete a session file. Returns True if deleted, False if not found."""
        path = self.root / f"session-{session_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

    # ── Internal ──────────────────────────────────────────────────────────────

    def _read(self, path: Path) -> SessionSnapshot:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionSnapshot.from_dict(data)
```

**The `latest.json` alias pattern:**

Instead of scanning all session files to find the newest one, `save()` always writes a copy to `latest.json`. This makes `--continue` an O(1) file read with zero scanning. The tradeoff is one extra write per save — worth it.

---

## 5. Teach `Message` to serialize and deserialize

The snapshot stores messages as plain dicts. Add two helpers to `Message` in `agent/models.py`:

```python
# agent/models.py  — add methods to the Message dataclass

@dataclass(slots=True)
class Message:
    # ...existing fields and methods...

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict safe for JSON storage."""
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        """Deserialize from a plain dict (loaded from JSON snapshot)."""
        return cls(role=data["role"], content=data["content"])
```

This is all you need. The `content` field is already a `list[dict]` of plain JSON-safe values — no extra conversion required.

---

## 6. Add snapshot and restore methods to `Agent`

Open `agent/agent.py` and extend the `Agent` class with three new methods:

```python
# agent/agent.py  — add these methods to the Agent class

from agent.models import Message, ModelResponse, ToolExecutionContext, SessionSnapshot

class Agent:
    def __init__(
        self,
        model_client: Any,
        tool_registry: ToolRegistry,
        system_prompt: str = "You are a helpful assistant.",
        cwd: str | None = None,
        model_name: str = "demo",          # ← new: stored in snapshot
    ) -> None:
        self.model_client = model_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd or __import__("os").getcwd()
        self.model_name = model_name
        self.messages: list[Message] = []
        self._turn_count: int = 0
        self._tool_call_count: int = 0
        self._snapshot: SessionSnapshot | None = None  # set on restore or first save

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def snapshot(self, carry_over: dict | None = None) -> SessionSnapshot:
        """
        Create a SessionSnapshot from the current agent state.

        Call this after each turn to persist progress.
        """
        if self._snapshot is None:
            self._snapshot = SessionSnapshot.new(
                cwd=self.cwd,
                model=self.model_name,
                system_prompt=self.system_prompt,
            )

        # Sync current state into the snapshot
        self._snapshot.messages = [m.to_dict() for m in self.messages]
        self._snapshot.usage = {
            "turns": self._turn_count,
            "tool_calls": self._tool_call_count,
        }
        self._snapshot.carry_over = carry_over or self._snapshot.carry_over

        # Use first user message as the session summary
        if not self._snapshot.summary and self.messages:
            self._snapshot.summary = self.messages[0].text[:80]

        return self._snapshot

    def restore(self, snapshot: SessionSnapshot) -> None:
        """
        Hydrate the agent from a saved snapshot.

        Restores:  messages, usage counters, carry-over metadata
        Rebuilds:  model client, tool registry, context (done by the caller)
        """
        self._snapshot = snapshot
        self.messages = [Message.from_dict(m) for m in snapshot.messages]
        self._turn_count = snapshot.usage.get("turns", 0)
        self._tool_call_count = snapshot.usage.get("tool_calls", 0)
        self.cwd = snapshot.cwd

    # ── Updated run() — count turns and tool calls ────────────────────────────

    async def run(self, user_text: str):   # return type unchanged
        self.messages.append(Message.user(user_text))
        self._turn_count += 1              # ← new
        yield StatusEvent(message=f"Thinking... (turn {self._turn_count})")

        context = self._build_context()

        while True:
            try:
                response: ModelResponse = await self.model_client.complete(
                    messages=self.messages,
                    tools=self.tool_registry.schemas(),
                    system_prompt=self.system_prompt,
                )
            except Exception as exc:
                yield ErrorEvent(message="Model call failed.", details=str(exc))
                return

            if response.text:
                self.messages.append(Message.assistant(response.text))
                yield AssistantTextDelta(text=response.text)

            if not response.wants_tool:
                return

            for tool_call in response.tool_calls:
                self._tool_call_count += 1   # ← new
                yield ToolExecutionStarted(
                    tool_name=tool_call.name,
                    tool_input=tool_call.input,
                )

                tool = self.tool_registry.get(tool_call.name)
                if tool is None:
                    result_text = f"Error: tool '{tool_call.name}' is not registered."
                    is_error = True
                    result_metadata: dict = {}
                else:
                    try:
                        result = await tool.execute(tool_call.input, context)
                        result_text = result.output
                        is_error = result.is_error
                        result_metadata = result.metadata
                    except Exception as exc:
                        result_text = f"Tool raised an exception: {exc}"
                        is_error = True
                        result_metadata = {}

                self.messages.append(Message.tool_result(tool_call.id, result_text))
                yield ToolExecutionCompleted(
                    tool_name=tool_call.name,
                    output=result_text,
                    is_error=is_error,
                    metadata=result_metadata,
                )

    def _build_context(self, ask_user_fn=None) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=self.cwd,
            ask_user=ask_user_fn,
            metadata={"turn": self._turn_count},
        )
```

**The restore pattern in plain English:**

1. Build fresh dependencies (model client, tool registry) — these are process state
2. Call `agent.restore(snapshot)` — this hydrates messages and counters
3. Continue the REPL normally — the agent picks up from exactly where it stopped

---

## 7. Update `main.py` — add CLI flags and auto-save

This is the most visible change. Replace `main.py` entirely:

```python
# main.py  — full Chapter 03 version

import argparse
import asyncio
import os
from pathlib import Path

from agent.agent import Agent
from agent.client import DemoModelClient
from agent.session import SessionStore
from agent.tools import default_registry
from agent.events import (
    AssistantTextDelta,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)


# ── Renderer ──────────────────────────────────────────────────────────────────

async def render(event: object) -> None:
    if isinstance(event, StatusEvent):
        print(f"  · {event.message}")
    elif isinstance(event, ToolExecutionStarted):
        args = event.tool_input or {}
        args_display = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "no args"
        print(f"  ⚙ {event.tool_name}({args_display})")
    elif isinstance(event, ToolExecutionCompleted):
        icon = "✗" if event.is_error else "✓"
        preview = event.output[:120].replace("\n", "↵")
        print(f"  {icon} {event.tool_name} → {preview}")
        if event.metadata:
            meta_str = ", ".join(f"{k}={v}" for k, v in event.metadata.items())
            print(f"    [{meta_str}]")
    elif isinstance(event, AssistantTextDelta):
        print(f"\nagent> {event.text}\n")
    elif isinstance(event, ErrorEvent):
        print(f"\n[ERROR] {event.message}")
        if event.details:
            print(f"        {event.details}")


# ── REPL with auto-save ───────────────────────────────────────────────────────

async def repl(agent: Agent, store: SessionStore) -> None:
    """Run the interactive REPL. Auto-saves after every turn."""
    print(f"Session: {agent._snapshot.session_id if agent._snapshot else 'new'}")
    print(f"Tools: {agent.tool_registry.names()}")
    print(f"Type 'quit' to exit, 'history' to see messages, 'sessions' to list sessions.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            _save_and_print(agent, store)
            return

        if not user_input:
            continue

        # Built-in REPL commands
        if user_input in {"quit", "exit", "q"}:
            _save_and_print(agent, store)
            return

        if user_input == "history":
            _print_history(agent)
            continue

        if user_input == "sessions":
            _print_sessions(store)
            continue

        # Run one agent turn
        async for event in agent.run(user_input):
            await render(event)

        # Auto-save after every turn
        snapshot = agent.snapshot()
        path = store.save(snapshot)
        print(f"  💾 saved → {path.name}")


def _save_and_print(agent: Agent, store: SessionStore) -> None:
    snapshot = agent.snapshot()
    path = store.save(snapshot)
    print(f"\nSession saved: {path}")
    print("Goodbye.")


def _print_history(agent: Agent) -> None:
    print(f"\n── Conversation history ({len(agent.messages)} messages) ──")
    for i, msg in enumerate(agent.messages, 1):
        text = msg.text
        role = msg.role
        if text:
            print(f"  {i:2}. [{role}] {text[:80]}")
        else:
            # tool result or tool call message
            for block in msg.content:
                if block.get("type") == "tool_result":
                    print(f"  {i:2}. [tool_result] id={block.get('tool_call_id', '?')}")
    print()


def _print_sessions(store: SessionStore) -> None:
    sessions = store.list_sessions()
    if not sessions:
        print("\nNo saved sessions found.\n")
        return
    print(f"\n── Saved sessions ({len(sessions)}) ──")
    for s in sessions:
        print(f"  {s.session_id}  {s.updated_at}  turns={s.usage.get('turns', 0)}  {s.summary[:50]!r}")
    print()


# ── Agent factory ─────────────────────────────────────────────────────────────

def build_agent() -> Agent:
    """Build a fresh Agent with default configuration."""
    client = DemoModelClient()
    registry = default_registry()
    return Agent(
        model_client=client,
        tool_registry=registry,
        system_prompt=(
            "You are a helpful assistant with access to filesystem tools. "
            "Use ask_user_question when you need clarification instead of guessing."
        ),
        cwd=os.getcwd(),
        model_name="demo",
    )


# ── Entry point with argparse ─────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal CLI Agent")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--continue", dest="resume_latest", action="store_true",
        help="Resume the most recent session."
    )
    group.add_argument(
        "--resume", metavar="SESSION_ID",
        help="Resume a specific session by ID."
    )
    group.add_argument(
        "--export", metavar="SESSION_ID",
        help="Export a session transcript to Markdown and exit."
    )
    parser.add_argument(
        "--sessions-dir", default="sessions",
        help="Directory for session files (default: ./sessions)."
    )
    args = parser.parse_args()

    store = SessionStore(root=Path(args.sessions_dir))

    # ── Export mode ───────────────────────────────────────────────────────────
    if args.export:
        path = store.export_transcript(args.export)
        if path:
            print(f"Transcript written to: {path}")
        else:
            print(f"Session '{args.export}' not found.")
        return

    # ── Build the agent ───────────────────────────────────────────────────────
    agent = build_agent()

    # ── Resume modes ──────────────────────────────────────────────────────────
    if args.resume_latest:
        snapshot = store.load_latest()
        if snapshot is None:
            print("No saved sessions found. Starting fresh.\n")
        else:
            agent.restore(snapshot)
            print(f"Resumed session {snapshot.session_id} ({snapshot.usage.get('turns', 0)} turns)\n")

    elif args.resume:
        snapshot = store.load_by_id(args.resume)
        if snapshot is None:
            print(f"Session '{args.resume}' not found. Starting fresh.\n")
        else:
            agent.restore(snapshot)
            print(f"Resumed session {snapshot.session_id} ({snapshot.usage.get('turns', 0)} turns)\n")

    # ── Fresh session: stamp a new snapshot now so the ID is available ────────
    else:
        agent._snapshot = agent.snapshot()
        print(f"New session: {agent._snapshot.session_id}\n")

    # ── Run the REPL ──────────────────────────────────────────────────────────
    await repl(agent, store)


if __name__ == "__main__":
    asyncio.run(main())
```

---

## 8. Run it and test session persistence

**Fresh session:**

```bash
python main.py
```

```
New session: 4f2a9c1b3e8d
Tools: ['get_time', 'echo', 'read_file', 'glob', 'write_file', 'ask_user_question']
Type 'quit' to exit, 'history' to see messages, 'sessions' to list sessions.

you> what time is it?
  · Thinking... (turn 1)
  ⚙ get_time(no args)
  ✓ get_time → 2026-04-24 10:44:21 UTC
  💾 saved → session-4f2a9c1b3e8d.json
you> quit
Session saved: sessions/session-4f2a9c1b3e8d.json
Goodbye.
```

**Resume the last session:**

```bash
python main.py --continue
```

```
Resumed session 4f2a9c1b3e8d (1 turns)

you> what did I ask you?
  · Thinking... (turn 2)

agent> You asked what time it is.
  💾 saved → session-4f2a9c1b3e8d.json
```

**Export a transcript:**

```bash
python main.py --export 4f2a9c1b3e8d
```

```
Transcript written to: sessions/transcript-4f2a9c1b3e8d.md
```

Open `sessions/transcript-4f2a9c1b3e8d.md`:

```markdown
# Session Transcript: 4f2a9c1b3e8d

- **Created:** 2026-04-24T10:44:21Z
- **Updated:** 2026-04-24T10:45:03Z
- **Model:** demo
- **Turns:** 2
- **Tool calls:** 1

---

**You:**
what time is it?

---

**Tool result** (`tc-001`):
```
2026-04-24 10:44:21 UTC
```

---

**You:**
what did I ask you?

---

**Agent:**
You asked what time it is.

---
```

---

## 9. Inspect the session file directly

```bash
cat sessions/latest.json
```

```json
{
  "session_id": "4f2a9c1b3e8d",
  "created_at": "2026-04-24T10:44:21Z",
  "updated_at": "2026-04-24T10:45:03Z",
  "cwd": "/home/user/project",
  "model": "demo",
  "system_prompt": "You are a helpful assistant...",
  "messages": [
    {
      "role": "user",
      "content": [{"type": "text", "text": "what time is it?"}]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_call_id": "tc-001",
          "content": "2026-04-24 10:44:21 UTC"
        }
      ]
    },
    {
      "role": "user",
      "content": [{"type": "text", "text": "what did I ask you?"}]
    },
    {
      "role": "assistant",
      "content": [{"type": "text", "text": "You asked what time it is."}]
    }
  ],
  "usage": {"turns": 2, "tool_calls": 1},
  "carry_over": {"last_read_file": null, "task_summary": ""},
  "summary": "what time is it?"
}
```

This is fully human-readable. You can edit it in a text editor, validate it with `python -m json.tool sessions/latest.json`, or write a migration script if the format changes.

---

## 10. Using carry-over metadata

`carry_over` is the place for small amounts of runtime data that survive restart but are too dynamic for the system prompt. Extend it by passing a dict to `agent.snapshot()`:

```python
# After a turn where the agent wrote a file:
carry_over = {
    "last_read_file": "/home/user/project/main.py",
    "task_summary": "Refactoring the main entry point",
    "last_written_file": "/home/user/project/main.py",
}
snapshot = agent.snapshot(carry_over=carry_over)
store.save(snapshot)
```

On restore, read it back:

```python
snapshot = store.load_latest()
agent.restore(snapshot)
last_file = agent._snapshot.carry_over.get("last_read_file")
```

**What belongs in carry-over:**
- recently accessed file paths (so the agent starts knowing what was opened)
- a one-line task summary
- any user-set flags (`verbose_mode`, `dry_run`)

**What does NOT belong in carry-over:**
- full file contents (that is long-term memory — Chapter 06)
- the agent's full reasoning history (that is in `messages`)
- process handles or callbacks (they cannot survive restart)

---

## 11. The rebuild-then-hydrate restore pattern

The correct restore sequence is always this order:

```python
# ✓ CORRECT — rebuild process state first, then hydrate session state

agent = build_agent()           # 1. build model client, registry, context fresh
agent.restore(snapshot)         # 2. hydrate messages, counters, cwd from JSON

# ✗ WRONG — trying to serialize live objects
import pickle
pickle.dump(agent, open("state.pkl", "wb"))   # open sockets, callbacks → crash
```

**Why "rebuild then hydrate"?**

Process state (async loops, network connections, open file handles) cannot be serialized. If you try, you get errors or silent corruption. Instead:

1. Always build fresh infrastructure from config
2. Only restore the pure data parts (messages, counters, metadata)

The agent becomes stateful again by re-establishing live connections, then loading saved conversation history. This is the same pattern used by databases (WAL replay), browsers (session restore), and IDEs (workspace state).

---

## 12. Common mistakes and fixes

### Mistake 1 — Saving raw provider JSON instead of internal Messages

```python
# WRONG — saves OpenAI-specific fields that change between API versions
snapshot["messages"] = raw_openai_response["choices"][0]["message"]
```

**Fix:** always serialize your internal `Message` objects via `message.to_dict()`. The provider wire format is an implementation detail — your snapshot format should outlive any single provider.

### Mistake 2 — Not saving after every turn

```python
# WRONG — saves only on quit; a crash loses everything
if user_input == "quit":
    store.save(agent.snapshot())
```

**Fix:** save after every turn. Each save is a small JSON write — the cost is negligible, and you never lose more than one turn on crash.

### Mistake 3 — Restoring process state from the snapshot

```python
# WRONG — callbacks and async handles cannot live in JSON
snapshot.carry_over["ask_user_callback"] = context.ask_user  # not serializable
```

**Fix:** only persist data, not behaviour. Rebuild callbacks in `build_agent()` or `_build_context()`.

### Mistake 4 — One giant pickle of the whole agent

```python
# WRONG — breaks on any code change, not human-readable, not debuggable
import pickle; pickle.dump(agent, f)
```

**Fix:** serialize only `SessionSnapshot.to_dict()` as JSON. Keep the snapshot schema under version control so you can write migrations when it changes.

---

## 13. Exercises

**Exercise A — Session listing command**

The REPL already has a `sessions` command. Extend it with a `--list` CLI flag:

```bash
python main.py --list
```

Print all sessions with their ID, timestamp, turn count, and summary. Implement it by calling `store.list_sessions()` and printing, then exiting.

**Exercise B — `carry_over` auto-tracking**

After any turn where `ReadFileTool` was called, automatically update `carry_over["last_read_file"]` with the resolved path from `ToolExecutionCompleted.metadata`. Do this inside the REPL loop by inspecting events as they arrive.

**Exercise C — Session pruning**

Add a `--prune` flag that deletes all sessions older than N days:

```bash
python main.py --prune 7    # delete sessions older than 7 days
```

Use `snapshot.updated_at` to compute age.

**Exercise D — Corrupt file resilience**

Manually corrupt a session file (`echo "not json" > sessions/latest.json`). Make sure `store.load_latest()` handles `json.JSONDecodeError` gracefully and returns `None` instead of crashing. Add the same guard to `list_sessions()`.

---

## 14. Full updated file structure

```
agent/
    __init__.py
    models.py      ← + SessionSnapshot dataclass with to_dict/from_dict
                   ← Message now has to_dict/from_dict
    tools.py       ← unchanged from Chapter 02
    events.py      ← unchanged from Chapter 02
    adapters.py    ← unchanged from Chapter 02
    client.py      ← unchanged from Chapter 02
    agent.py       ← + snapshot(), restore(), turn/tool_call counters
    session.py     ← NEW: SessionStore with save/load/list/export/delete
main.py            ← + argparse, --continue, --resume, --export, auto-save
sessions/          ← created at runtime
    session-{id}.json
    latest.json
    transcript-{id}.md
```

---

## 15. Checklist before moving on

- [ ] `SessionSnapshot` has all fields: id, cwd, model, system_prompt, messages, usage, carry_over, summary, timestamps
- [ ] `SessionSnapshot.to_dict()` and `from_dict()` are explicit — no `dataclasses.asdict()`
- [ ] `Message.to_dict()` and `Message.from_dict()` exist and are used in snapshot serialization
- [ ] `SessionStore.save()` writes both `session-{id}.json` and `latest.json`
- [ ] `SessionStore.load_latest()` and `load_by_id()` return `None` when not found
- [ ] `SessionStore.export_transcript()` writes a human-readable `.md` file
- [ ] `Agent.snapshot()` serializes current messages and usage counters
- [ ] `Agent.restore()` rebuilds `self.messages` from the snapshot's dict list
- [ ] `main.py` auto-saves after every turn, not only on quit
- [ ] `--continue`, `--resume`, and `--export` flags all work from the terminal
- [ ] Carry-over metadata is small, data-only, and does not contain process state
- [ ] Corrupt/missing session files are handled gracefully (no crash)
- [ ] `schema_version` is saved in every snapshot and read in `from_dict`
- [ ] A `migrate_snapshot(data, from_version)` function handles old snapshots
- [ ] Session ID is set in a `contextvars.ContextVar` in `repl()` for log correlation

---

### Two improvements to add now

**Schema versioning** prevents silent failures when snapshot fields change across chapters:

```python
# In SessionSnapshot.to_dict():
"schema_version": 2,   # increment when shape changes

# In SessionSnapshot.from_dict():
version = data.get("schema_version", 1)
if version < 2:
    data.setdefault("mode", "default")   # field added in Chapter 09
```

**Session ID correlation** lets all log lines carry the same session ID:

```python
# agent/session.py  — add at module level
import contextvars
SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="none")

# main.py repl()  — set immediately after build/restore
from agent.session import SESSION_ID
SESSION_ID.set(agent._snapshot.session_id if agent._snapshot else "new")
```

`contextvars` propagates through async tasks — every audit log, hook output, and error from that turn will be traceable to the right session.

---

Next: [03-1-context-compaction.md](03-1-context-compaction.md) — prevent context window overflow in long sessions, then continue to [04-hooks.md](04-hooks.md).

---

## Current Nexus Notes

### Auto-resume on startup

When `uv run nexus` is launched without `--no-session`, Nexus reads `.nexus/sessions/latest_session.txt` and automatically resumes the most recently saved session. The startup banner immediately shows:

```
Nexus Agent Framework
Provider: mistral  |  Model: mistral-medium-latest  |  Mode: default
Resumed session abc123def (14 messages). Use /session new to start fresh or /session list to pick another.
Type /help for commands or /quit to exit.
```

`load_latest()` on `SessionStore` implements this — it reads `latest_session.txt`, loads the referenced snapshot file, and returns `None` (gracefully) if the file is missing, corrupt, or the referenced session no longer exists.

```python
def load_latest(self) -> SessionSnapshot | None:
    latest_file = self.root / "latest_session.txt"
    if not latest_file.exists():
        return None
    session_id = latest_file.read_text(encoding="utf-8").strip()
    try:
        return self.load(session_id)
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return None
```

`_resolve_session` in `app.py` returns `(snapshot, resumed: bool)`:

```python
def _resolve_session(session_id, store, *, persist_sessions=True):
    if not persist_sessions:
        return new_snapshot(), False
    if session_id is not None:          # explicit --session <id>
        try:
            return store.load(session_id), True
        except FileNotFoundError:
            return new_snapshot(session_id=session_id), False
    latest = store.load_latest()        # auto-resume
    if latest is not None:
        return latest, True
    return new_snapshot(), False        # first ever run
```

`run_repl` accepts `session_resumed: bool = False` and uses it to conditionally print the resume line.

### Session slash commands

| Command | What it does |
|---|---|
| `/session` | Show current session ID and message count |
| `/session new` | Save current session and start a fresh one |
| `/session list` | List all saved sessions (ID, timestamp, summary) |
| `/session resume <id>` | Load a previous session's messages into context |
| `/session save` | Persist current session to disk immediately |
| `/session export <path>` | Export session messages as JSON |
| `/session help` | Show the help table |

