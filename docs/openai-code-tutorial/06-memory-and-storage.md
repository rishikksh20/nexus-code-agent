# 06 — Memory and Storage: Durable Knowledge Across Sessions

## Prerequisites

Complete [05-context-engineering.md](05-context-engineering.md) first.

Your agent can now save sessions and build rich prompts — but it forgets **everything** between different projects and across long gaps. If the user told the agent "always use PyJWT for authentication" three sessions ago, the agent has no idea.

This chapter adds a **memory store** — durable, human-readable, file-based knowledge that survives indefinitely and feeds into the `ContextBuilder` from Chapter 05.

---

## What you will build

```
agent/
    memory.py           ← NEW: MemoryStore class
    tools.py            ← updated: SaveMemoryTool, SearchMemoryTool
    prompts.py          ← updated: retrieves memory per-turn
.agent-memory/          ← created at runtime
    MEMORY.md           ← index of all memory entries
    preferences.md      ← example memory file
    architecture.md     ← example memory file
```

---

## 1. Session history vs memory — the critical distinction

```
SESSION HISTORY (Chapter 03)          MEMORY (this chapter)
────────────────────────────          ────────────────────────────
What happened in this conversation    What should matter in ALL conversations
Stored in sessions/*.json             Stored in .agent-memory/*.md
Lost when session expires             Persists indefinitely
Example: "you ran get_time"           Example: "user prefers PyJWT for auth"
```

Never mix these. If you store session events in memory, it becomes unmanageable. If you store durable facts in sessions, they disappear too soon.

---

## 2. Why file-based memory first

Many guides jump straight to vector databases. You almost never need that at first.

Plain Markdown files are:
- **Human-readable** — you can `cat .agent-memory/preferences.md` to see what the agent knows
- **Human-editable** — fix bad memories by opening a text editor
- **Diffable** — version-control them in git
- **Low-friction** — no database to set up, no schema migrations
- **Debuggable** — if the model behaves oddly, check the memory file

Start here. Add vector search only when keyword retrieval genuinely fails you.

---

## 3. Create `agent/memory.py`

```python
# agent/memory.py

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MemoryEntry:
    """One stored memory note."""
    title: str
    content: str
    slug: str              # filename-safe identifier
    tags: list[str] = field(default_factory=list)

    def matches(self, query: str) -> bool:
        """
        Simple keyword retrieval.

        Returns True if any word in the query appears in the title, content, or tags.
        No embeddings needed at this stage.
        """
        q_lower = query.lower()
        words = re.findall(r"\w+", q_lower)
        searchable = f"{self.title} {self.content} {' '.join(self.tags)}".lower()
        return any(word in searchable for word in words)


class MemoryStore:
    """
    File-based long-term memory.

    Layout:
        root/
            MEMORY.md          ← index listing all entries
            {slug}.md          ← one file per memory entry

    The model interacts with memory through SaveMemoryTool and SearchMemoryTool.
    The runtime reads memory through retrieve() for prompt injection.
    """

    INDEX_FILE = "MEMORY.md"

    def __init__(self, root: Path = Path(".agent-memory")) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # ── Write ─────────────────────────────────────────────────────────────────

    def save(self, title: str, content: str, tags: list[str] | None = None) -> MemoryEntry:
        """
        Save or overwrite a memory entry.

        If a file with the same slug already exists, it is overwritten.
        The index is always updated after saving.
        """
        slug = self._to_slug(title)
        entry = MemoryEntry(title=title, content=content, slug=slug, tags=tags or [])

        # Write entry file
        path = self.root / f"{slug}.md"
        tag_line = f"tags: {', '.join(tags)}\n\n" if tags else ""
        path.write_text(
            f"# {title}\n{tag_line}{content.strip()}\n",
            encoding="utf-8",
        )

        # Update index
        self._update_index(entry)
        return entry

    # ── Read ──────────────────────────────────────────────────────────────────

    def load_all(self) -> list[MemoryEntry]:
        """Load all memory entries from disk."""
        entries = []
        for path in self.root.glob("*.md"):
            if path.name == self.INDEX_FILE:
                continue
            entry = self._parse_file(path)
            if entry:
                entries.append(entry)
        return entries

    def load_by_slug(self, slug: str) -> MemoryEntry | None:
        """Load one memory entry by slug."""
        path = self.root / f"{slug}.md"
        if not path.exists():
            return None
        return self._parse_file(path)

    # ── Retrieve (for prompt injection) ───────────────────────────────────────

    def retrieve(self, query: str, max_entries: int = 3) -> str:
        """
        Find memory entries relevant to the query using keyword matching.

        Returns a formatted string ready for ContextBuilder.add_memory().
        Returns empty string if nothing matches (so no section is added).
        """
        if not query.strip():
            return ""

        all_entries = self.load_all()
        matching = [e for e in all_entries if e.matches(query)][:max_entries]

        if not matching:
            return ""

        parts = []
        for entry in matching:
            parts.append(f"### {entry.title}")
            parts.append(entry.content.strip())
        return "\n\n".join(parts)

    # ── Delete ────────────────────────────────────────────────────────────────

    def delete(self, slug: str) -> bool:
        """Delete a memory entry. Returns True if deleted."""
        path = self.root / f"{slug}.md"
        if not path.exists():
            return False
        path.unlink()
        self._rebuild_index()
        return True

    # ── Index ─────────────────────────────────────────────────────────────────

    def _update_index(self, entry: MemoryEntry) -> None:
        """Add entry to index if not already present."""
        index_path = self.root / self.INDEX_FILE
        existing = index_path.read_text(encoding="utf-8") if index_path.exists() else "# Memory Index\n"

        link = f"[{entry.title}]({entry.slug}.md)"
        if entry.slug not in existing:
            existing = existing.rstrip() + f"\n- {link}\n"
            index_path.write_text(existing, encoding="utf-8")

    def _rebuild_index(self) -> None:
        """Rebuild the index from all existing files."""
        entries = self.load_all()
        lines = ["# Memory Index\n"]
        for e in sorted(entries, key=lambda x: x.title):
            lines.append(f"- [{e.title}]({e.slug}.md)")
        index_path = self.root / self.INDEX_FILE
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _to_slug(title: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "memory"

    def _parse_file(self, path: Path) -> MemoryEntry | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None

        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() if lines else path.stem
        tags: list[str] = []

        # Parse optional tags line: "tags: foo, bar"
        content_start = 1
        if len(lines) > 1 and lines[1].startswith("tags:"):
            tag_str = lines[1].removeprefix("tags:").strip()
            tags = [t.strip() for t in tag_str.split(",") if t.strip()]
            content_start = 3  # skip blank line after tags

        content = "\n".join(lines[content_start:]).strip()
        return MemoryEntry(title=title, content=content, slug=path.stem, tags=tags)
```

---

## 4. Add memory tools to `agent/tools.py`

```python
# agent/tools.py  — add these two tools

from agent.memory import MemoryStore


class SaveMemoryTool(BaseTool):
    """
    Save a fact or note that should persist across sessions.

    The model uses this to store durable knowledge it discovers:
    user preferences, architecture facts, recurring conventions.
    """
    name = "save_memory"
    description = (
        "Save a durable memory note that will be available in future sessions. "
        "Use this for stable facts: user preferences, architecture decisions, "
        "coding conventions. Do NOT use for session-specific details."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title for this memory (used as the filename).",
            },
            "content": {
                "type": "string",
                "description": "The memory content to store.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for retrieval (e.g. ['python', 'auth']).",
            },
        },
        "required": ["title", "content"],
    }

    def __init__(self, memory_store: MemoryStore) -> None:
        self._store = memory_store

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        title = arguments.get("title", "").strip()
        content = arguments.get("content", "").strip()
        tags = arguments.get("tags", [])

        if not title:
            return ToolResult(output="Error: 'title' is required.", is_error=True)
        if not content:
            return ToolResult(output="Error: 'content' is required.", is_error=True)

        entry = self._store.save(title, content, tags)
        return ToolResult(
            output=f"Memory saved: '{entry.title}' → {entry.slug}.md",
            metadata={"slug": entry.slug, "tags": tags},
        )


class SearchMemoryTool(BaseTool):
    """
    Search memory for notes relevant to a query.
    Returns matching memory entries as text.
    """
    name = "search_memory"
    description = (
        "Search saved memory notes for information relevant to a query. "
        "Use this when you need to recall a fact from a previous session."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords or phrase to search for.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 3).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, memory_store: MemoryStore) -> None:
        self._store = memory_store

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        query = arguments.get("query", "").strip()
        max_results = int(arguments.get("max_results", 3))

        if not query:
            return ToolResult(output="Error: 'query' is required.", is_error=True)

        result_text = self._store.retrieve(query, max_entries=max_results)
        if not result_text:
            return ToolResult(output=f"No memory found for: '{query}'")
        return ToolResult(output=result_text)
```

---

## 5. Update `default_registry` to include memory tools

```python
# agent/tools.py  — updated default_registry

from agent.memory import MemoryStore

def default_registry(memory_store: MemoryStore | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    registry.register(EchoTool())
    registry.register(ReadFileTool())
    registry.register(GlobTool())
    registry.register(WriteFileTool())
    registry.register(AskUserQuestionTool())
    if memory_store is not None:
        registry.register(SaveMemoryTool(memory_store))
        registry.register(SearchMemoryTool(memory_store))
    return registry
```

---

## 6. Feed memory into the context builder per turn

Update `Agent.__init__` and `_build_system_prompt` to auto-retrieve memory:

```python
# agent/agent.py  — add memory_store parameter and auto-retrieval

from agent.memory import MemoryStore

class Agent:
    def __init__(
        self,
        model_client: Any,
        tool_registry: ToolRegistry,
        base_prompt: str = DEFAULT_BASE_PROMPT,
        cwd: str | None = None,
        model_name: str = "demo",
        hook_executor: HookExecutor | None = None,
        project_notes: str = "",
        memory_store: MemoryStore | None = None,    # ← new
    ) -> None:
        # ...existing init...
        self.memory_store = memory_store            # ← new

    def _build_system_prompt(self, user_text: str = "") -> str:
        carry_over = self._snapshot.carry_over if self._snapshot else {}

        # Auto-retrieve memory relevant to the current user input
        memory_text = ""
        if self.memory_store and user_text:
            memory_text = self.memory_store.retrieve(user_text, max_entries=3)

        return build_runtime_prompt(
            cwd=self.cwd,
            tool_names=self.tool_registry.names(),
            project_notes=self.project_notes,
            carry_over=carry_over,
            user_text=user_text,
            memory_text=memory_text,           # ← auto-retrieved
            base_prompt=self.base_prompt,
        )
```

Memory retrieval is automatic — the model sees relevant memory without needing to ask for it explicitly. It can still call `search_memory` for more targeted lookup.

---

## 7. Update `main.py`

```python
# main.py  — updated build_agent()

from agent.memory import MemoryStore
from pathlib import Path

def build_agent(project_notes: str = "") -> Agent:
    client = DemoModelClient()
    memory_store = MemoryStore(root=Path(".agent-memory"))
    registry = default_registry(memory_store=memory_store)

    executor = HookExecutor()
    executor.register(LoggingHook())
    executor.register(AuditLogHook())
    executor.register(TurnSummaryHook())

    return Agent(
        model_client=client,
        tool_registry=registry,
        base_prompt=DEFAULT_BASE_PROMPT,
        cwd=__import__("os").getcwd(),
        model_name="demo",
        hook_executor=executor,
        project_notes=project_notes,
        memory_store=memory_store,          # ← new
    )
```

---

## 8. Run it and test memory

```bash
python main.py
```

**Save a memory:**

```
you> remember that this project uses PyJWT for authentication, not the built-in sessions
  · Thinking... (turn 1)
  ⚙ save_memory(title='Authentication library preference', content='Use PyJWT ...')
  ✓ save_memory → Memory saved: 'Authentication library preference' → authentication_library_preference.md
```

**Quit and restart:**

```bash
python main.py
```

**Query triggers auto-retrieval:**

```
you> how should I implement login?
  · Thinking... (turn 1)

agent> Based on your project preferences, use PyJWT for authentication...
```

The model had PyJWT in its prompt automatically — retrieved by keyword match on "login" → "authentication".

**Inspect what was saved:**

```bash
cat .agent-memory/authentication_library_preference.md
```

```markdown
# Authentication Library Preference
Use PyJWT for authentication, not the built-in sessions module.
```

```bash
cat .agent-memory/MEMORY.md
```

```markdown
# Memory Index

- [Authentication Library Preference](authentication_library_preference.md)
```

---

## 9. Pre-populate memory manually

You can write memory files by hand — no agent required:

```bash
mkdir -p .agent-memory
cat > .agent-memory/project_conventions.md << 'EOF'
# Project Conventions
- Use ruff for linting: `ruff check .`
- Tests live in ./tests/ and run with: `pytest -q`
- All API endpoints must have docstrings
- Never commit directly to main
EOF

# Update the index
cat > .agent-memory/MEMORY.md << 'EOF'
# Memory Index
- [Project Conventions](project_conventions.md)
EOF
```

Now every session knows these rules without being told.

---

## 10. What should go in memory vs session carry-over vs the prompt

| Where | Lifetime | Examples |
|---|---|---|
| `base_prompt` | Permanent (code) | Role definition, core operating rules |
| `project_notes` (file) | Project lifetime | Linting rules, test commands, architecture |
| `memory_store` | Indefinite | User preferences, durable decisions |
| `carry_over` (session) | Session | Last file read, current task summary |
| `messages` (session) | Session | Full conversation history |

As a rule: if the user would need to re-tell the agent something every session, it belongs in memory.

---

## 11. Common mistakes

### Mistake 1 — Storing session events in memory

```python
# WRONG — this is session-specific, not durable knowledge
memory_store.save("Thing I did today", "I ran get_time and it returned 10:44 UTC")
```

**Fix:** memory is for stable, reusable facts. Session events belong in `messages`.

### Mistake 2 — Loading all memory into every prompt

```python
# WRONG — floods the context with irrelevant memory
memory_text = "\n".join(e.content for e in memory_store.load_all())
```

**Fix:** use `memory_store.retrieve(user_text)` to select only relevant entries.

### Mistake 3 — Storing credentials or secrets in memory

Memory files are plain text on disk. Never store API keys, passwords, or tokens in memory entries.

---

## 12. Exercises

**Exercise A — Tag-based retrieval**

Extend `MemoryStore.retrieve()` to also accept a `tags: list[str]` filter. If tags are provided, only return entries that have at least one matching tag.

**Exercise B — Memory list command**

Add a `memory` command to the REPL (like `history` from Chapter 03):

```
you> memory
── Saved memories (2 entries) ──
  authentication_library_preference  "Use PyJWT for authentication..."
  project_conventions               "Use ruff for linting..."
```

**Exercise C — Memory from `post_tool_use` hook**

Create a `MemoryHook` for `POST_TOOL_USE`. If `write_file` succeeds, automatically save a memory entry: `"Last written file: {resolved_path}"` with tag `"files"`. This makes the agent remember file history across sessions.

**Exercise D — Memory edit tool**

Add a `DeleteMemoryTool` that takes a `slug` argument and calls `memory_store.delete(slug)`. Register it. Let the model clean up outdated memories.

---

## 13. Checklist before moving on

- [ ] `MemoryStore` saves entries as Markdown files in `.agent-memory/`
- [ ] `MEMORY.md` index is updated every time an entry is saved or deleted
- [ ] `MemoryEntry.matches(query)` uses keyword overlap (no embeddings needed)
- [ ] `MemoryStore.retrieve(query)` returns only relevant entries, not all entries
- [ ] `SaveMemoryTool` and `SearchMemoryTool` are registered in `default_registry`
- [ ] `Agent._build_system_prompt()` auto-retrieves memory per turn
- [ ] Memory is injected via `ContextBuilder.add_memory()` — same path as all context
- [ ] Memory files are human-readable (plain Markdown)
- [ ] Session carry-over and long-term memory remain deliberately separate
- [ ] `MemoryEntry` has an `updated_at` field and `MemoryStore` supports TTL pruning

### Improvement: memory expiry and decay

`MemoryStore` grows forever without bounds. Add TTL pruning so stale entries are removed automatically:

```python
# agent/memory.py  — add to MemoryEntry
from datetime import datetime, timezone

@dataclass
class MemoryEntry:
    # ...existing fields...
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

# agent/memory.py  — add to MemoryStore
def prune(self, ttl_days: int = 90, max_entries: int = 200) -> int:
    """Remove entries older than ttl_days or over the max_entries budget."""
    entries = sorted(self.load_all(), key=lambda e: e.updated_at or "", reverse=True)
    removed = 0
    now = datetime.now(timezone.utc)

    for i, entry in enumerate(entries):
        over_budget = (max_entries > 0 and i >= max_entries)
        too_old = False
        if ttl_days > 0 and entry.updated_at:
            try:
                age = (now - datetime.fromisoformat(entry.updated_at)).days
                too_old = age > ttl_days
            except ValueError:
                pass
        if over_budget or too_old:
            self.delete(entry.slug)
            removed += 1
    return removed
```

Call `memory_store.prune()` once per session start in `main.py` — not on every turn.

---

Next: [07-permissions.md](07-permissions.md)

