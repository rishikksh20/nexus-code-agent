# Chapter 4: Session State, Context, And Memory

## Objective

Convert the minimal harness into a stateful system that can survive multiple turns and multiple runs. This chapter combines three major tutorial ideas:

- session persistence from `openai-code-tutorial`
- dynamic prompt layering from both tutorial sets
- durable file-based memory instead of opaque storage systems

## Why This Layer Matters

Without explicit session handling, every run starts from zero. Without context assembly, the model sees raw messages with no environment framing. Without durable memory, the harness cannot retain useful knowledge beyond one conversation.

These are separate concerns. Keep them separate in code too.

## Add Session Snapshots

Represent persisted state as a typed snapshot rather than serializing the live agent object.

```python
from dataclasses import asdict, dataclass, field
from datetime import datetime, UTC


@dataclass(slots=True)  # NOT frozen: messages list is appended to during a turn
class SessionSnapshot:
    session_id: str
    created_at: str
    updated_at: str
    messages: list[Message] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


def new_snapshot(session_id: str) -> SessionSnapshot:
    now = datetime.now(UTC).isoformat()
    return SessionSnapshot(session_id=session_id, created_at=now, updated_at=now)
```

> **Why not `frozen=True`?** `frozen=True` on a dataclass prevents reassigning fields, but it does not make `list` or `dict` fields immutable — you can still call `.append()` or `.update()` on them. Since `messages` is an accumulator that grows with every turn, marking the class frozen gives false safety without benefit. Keep `SessionSnapshot` mutable and enforce the append-only convention in `SessionStore.save()`.

## Create A Session Store

Keep the first implementation file-based and transparent.

```python
import json
from pathlib import Path


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def save(self, snapshot: SessionSnapshot) -> None:
        payload = asdict(snapshot)
        target = self._path(snapshot.session_id)
        # Atomic write: write to a temp file then rename to avoid partial reads
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)  # atomic on POSIX; best-effort on Windows

    def load(self, session_id: str) -> SessionSnapshot:
        raw = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        messages = [Message(**item) for item in raw["messages"]]
        return SessionSnapshot(
            session_id=raw["session_id"],
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            messages=messages,
            metadata=raw.get("metadata", {}),
        )
```

This store is intentionally plain. You should be able to inspect session files directly during debugging.

## Build Context Dynamically

One of the best upgrades in `openai-code-tutorial` is treating context construction as a real subsystem instead of a single giant system prompt string.

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class ContextSections:
    base_instruction: str
    environment: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    project_notes: list[str] = field(default_factory=list)
    task_focus: list[str] = field(default_factory=list)


class ContextBuilder:
    def build(self, sections: ContextSections) -> str:
        blocks = [sections.base_instruction]

        for items in (
            sections.environment,
            sections.tools,
            sections.project_notes,
            sections.task_focus,
        ):
            if items:
                blocks.append("\n".join(f"- {item}" for item in items))

        return "\n\n".join(blocks)
```

### Good Context Sections

Use small sections with clear purpose:

- base instruction: what the harness is and how it should behave
- environment: current directory, OS, user mode, available capabilities
- tools: short descriptions of registered tools
- project notes: stable facts about the repo or task domain
- task focus: constraints for the current turn

Do not bury everything in the base prompt.

## Add Long Context Compaction

This is one of the most important improvements from `openai-code-tutorial`. If you skip it, your harness will degrade quickly on long sessions.

```python
class TokenEstimator:
    def estimate(self, text: str) -> int:
        return max(1, len(text) // 4)


def compact_messages(messages: list[Message], max_tokens: int, estimator: TokenEstimator) -> list[Message]:
    kept: list[Message] = []
    running_total = 0

    for message in reversed(messages):
        size = estimator.estimate(message.content)
        if running_total + size > max_tokens:
            break
        kept.append(message)
        running_total += size

    return list(reversed(kept))
```

Later you can preserve pinned messages and summarize older turns. For now, a sliding window is enough.

## Add Durable Memory

Use a human-readable file-based memory store instead of a vector database for the first serious version.

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class MemoryEntry:
    key: str
    content: str
    keywords: tuple[str, ...]


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, entry: MemoryEntry) -> None:
        path = self.root / f"{entry.key}.md"
        body = entry.content + "\n\nKeywords: " + ", ".join(entry.keywords)
        path.write_text(body, encoding="utf-8")

    def search(self, query: str) -> list[str]:
        results: list[str] = []
        lowered = query.lower()
        for path in self.root.glob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue  # skip unreadable or corrupted files
            if lowered in text.lower():
                results.append(text)
        return results
```

This approach is simple, inspectable, and easy to debug. It matches the spirit of the tutorial series: first build correctness and clarity, then optimize if needed.

## Suggested Package Growth

After this chapter your project can grow into:

```text
agent_harness/
  models.py
  prompts.py
  runtime/
    agent.py
    context_builder.py
    sessions.py
  memory/
    store.py
```

## Action Plan

1. Add a file-based `SessionStore`.
2. Persist and resume message history by session ID.
3. Build context from named sections instead of one static prompt.
4. Add token estimation and message compaction before model calls.
5. Add a simple file-based memory store for durable notes.
6. Keep session history and memory as separate concepts.

## Validation Checklist

- You can stop the harness and resume a prior session.
- The system prompt is assembled from separate sections.
- Long conversations are compacted before model calls.
- Durable memory is saved in readable files.
- Memory search does not mutate session history.

## Definition Of Done

This chapter is complete when you can explain the difference between:

- live session state
- compacted context sent to the model
- durable memory retained across sessions

If those are not separate in your design, future features will become brittle.

---

## Current Nexus Implementation

### Auto-resume on startup

When `uv run nexus` is launched without `--no-session`, the runtime reads `.nexus/sessions/latest_session.txt` and automatically resumes the most recently saved session. The startup banner shows:

```
Nexus Agent Framework
Provider: mistral  |  Model: mistral-medium-latest  |  Mode: default
Resumed session abc123def (14 messages). Use /session new to start fresh or /session list to pick another.
```

`SessionStore.load_latest()` handles this — it reads `latest_session.txt`, loads the referenced snapshot, and returns `None` gracefully if the pointer is missing, corrupt, or the file no longer exists.

`_resolve_session` in `app.py` returns `(snapshot, resumed: bool)`. When no explicit `--session <id>` is given, it calls `store.load_latest()` and auto-resumes. `run_repl` receives `session_resumed: bool` and prints the banner line only when `True`.

Pass `--no-session` to disable persistence entirely. Use `--session <id>` to resume a specific session by ID.

### Session slash commands

| Command | What it does |
|---|---|
| `/session` | Show current session ID and message count |
| `/session new` | Save current session and start a fresh one |
| `/session list` | List all saved sessions (ID, timestamp, summary) |
| `/session resume <id>` | Load a previous session's messages into context |
| `/session save` | Persist the current session immediately |
| `/session export <path>` | Export session messages as JSON |
| `/session help` | Show this table |
