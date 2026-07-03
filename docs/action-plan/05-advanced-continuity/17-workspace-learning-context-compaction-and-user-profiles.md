# Chapter 17: Workspace Learning, Context Compaction, And User Profiles

## Objective

Close one of the biggest gaps between a useful harness and a repeatedly useful harness. Up to Chapter 9, your system can execute tasks, persist sessions, and store durable memory. What it still lacks is a disciplined way to learn from completed sessions at two different scopes:

- workspace scope: what this project looks like and how it is usually handled
- user scope: how this developer prefers to work across projects

This chapter also deepens context compaction beyond the earlier sliding-window treatment so long sessions remain useful instead of slowly degrading.

## Why This Chapter Exists

The attached `openai-code-tutorial` material adds an important practical insight: not all long-lived context belongs in the same place.

Keep these concepts separate:

- session history: the exact turn-by-turn record for one run
- memory entries: durable facts or notes explicitly stored by the harness
- workspace knowledge: a rolling summary of the current repository and environment
- user profile: a rolling summary of the operator's preferences and recurring patterns

If you collapse those into one storage bucket, retrieval becomes noisy and maintenance gets harder.

## What You Will Build

```text
{workspace}/.agent/
  sessions/
  memory/
  knowledge.md
  facts.json
  audit-trail.jsonl

~/.agent/
  profile.md
  workspaces.json
  tools.md

agent_harness/
  memory/
    workspace.py
    profiles.py
  runtime/
    context_builder.py
    post_session.py
```

## Two Scopes, One Rule

The rule is simple: update workspace knowledge and user profile after the session ends, not after every turn.

That rule gives you three benefits:

1. turn execution stays fast
2. updates become easier to audit and reason about
3. partial failures do not corrupt live turn state

## Current Nexus Notes

The current Nexus runtime now implements this chapter in a minimal but real way:

- workspace knowledge is updated after the session ends, not during the active turn loop
- structured workspace facts are persisted in `.nexus/facts.json`
- human-readable workspace knowledge is regenerated into `.nexus/knowledge.md`
- user-scoped learning is written to `~/.nexus/profile.md` and `~/.nexus/workspaces.json`
- context compaction now preserves recent turns and moves older detail into explicit carry-over summaries
- compaction thresholds are no longer fixed at 10,000/14,000 tokens; at startup, Nexus looks up the active model in the built-in model catalogue (`nexus/config/model_catalog.py`) and auto-sets soft to 65% and hard to 85% of that model's known context window; user-defined values are respected and never overwritten
- the current compaction limits and estimated token usage for system prompt and history are visible at any time via `/context usage` inside the REPL

The current implementation still keeps learning conservative on purpose. It records stable workspace facts, recent tasks, and tool preferences rather than speculative summaries about the user.

## Add A Directory Resolver

Start with an object that resolves both local and global agent directories.

```python
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class AgentDirs:
    workspace_root: Path
    global_root: Path

    @property
    def local_agent_root(self) -> Path:
        return self.workspace_root / ".agent"

    @property
    def sessions_dir(self) -> Path:
        return self.local_agent_root / "sessions"

    @property
    def memory_dir(self) -> Path:
        return self.local_agent_root / "memory"

    @property
    def knowledge_file(self) -> Path:
        return self.local_agent_root / "knowledge.md"

    @property
    def facts_file(self) -> Path:
        return self.local_agent_root / "facts.json"

    @property
    def profile_file(self) -> Path:
        return self.global_root / "profile.md"

    def ensure(self) -> None:
        for path in (self.local_agent_root, self.sessions_dir, self.memory_dir, self.global_root):
            path.mkdir(parents=True, exist_ok=True)
```

Do not let random subsystems invent their own path layout. Make directory ownership explicit.

## Build Workspace Knowledge

Workspace knowledge should be readable by humans and useful to the context builder.

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class WorkspaceKnowledge:
    project_name: str
    description: str = ""
    tech_stack: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    key_files: dict[str, str] = field(default_factory=dict)
    recent_tasks: list[str] = field(default_factory=list)
    facts: list[dict[str, str]] = field(default_factory=list)
    session_count: int = 0

    def to_markdown(self) -> str:
        lines = [f"# Workspace Knowledge: {self.project_name}", ""]

        if self.description:
            lines.extend([self.description, ""])
        if self.tech_stack:
            lines.append("## Tech Stack")
            lines.extend(f"- {item}" for item in self.tech_stack)
            lines.append("")
        if self.conventions:
            lines.append("## Conventions")
            lines.extend(f"- {item}" for item in self.conventions)
            lines.append("")
        if self.key_files:
            lines.append("## Key Files")
            lines.extend(f"- {path}: {purpose}" for path, purpose in self.key_files.items())
            lines.append("")
        if self.recent_tasks:
            lines.append("## Recent Tasks")
            lines.extend(f"- {item}" for item in self.recent_tasks[-10:])
            lines.append("")

        return "\n".join(lines).strip() + "\n"
```

This file should answer: what is this project, what does it use, and what patterns have already been discovered here?

## Add User Profiles Carefully

The user profile is not a surveillance log. It is a compact working-preference record.

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class UserProfile:
    preferred_languages: list[str] = field(default_factory=list)
    response_style: str = "concise"
    preferred_tools: list[str] = field(default_factory=list)
    recurring_workflows: list[str] = field(default_factory=list)
    common_constraints: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = ["# User Profile", ""]
        lines.append(f"- Response style: {self.response_style}")
        lines.extend(f"- Preferred language: {item}" for item in self.preferred_languages)
        lines.extend(f"- Preferred tool: {item}" for item in self.preferred_tools)
        lines.extend(f"- Workflow: {item}" for item in self.recurring_workflows)
        lines.extend(f"- Constraint: {item}" for item in self.common_constraints)
        return "\n".join(lines) + "\n"
```

Only store stable and useful preferences. Do not write speculative personality summaries.

## Deepen Context Compaction

Earlier chapters introduced a basic sliding window. That is not enough once sessions become large and tool-heavy.

Use a three-layer compaction strategy:

1. keep pinned messages intact
2. keep recent turns intact
3. summarize or digest older turns and bulky tool results

```python
from dataclasses import dataclass, field

# Message is imported from models.py (from models import Message)


@dataclass(slots=True)
class CarryOverState:
    pinned_facts: list[str] = field(default_factory=list)
    summarized_history: list[str] = field(default_factory=list)
    active_constraints: list[str] = field(default_factory=list)


class ContextCompactor:
    def __init__(self, token_estimator, soft_limit: int, hard_limit: int) -> None:
        self.token_estimator = token_estimator
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit

    def should_compact(self, messages: list[Message]) -> bool:
        total = sum(self.token_estimator.estimate(message.content) for message in messages)
        return total >= self.soft_limit

    def compact(self, messages: list[Message], carry_over: CarryOverState) -> tuple[list[Message], CarryOverState]:
        recent = messages[-12:]
        older = messages[:-12]

        if older:
            summary = f"Summarized {len(older)} earlier messages into carry-over state."
            carry_over.summarized_history.append(summary)

        return recent, carry_over
```

In a stronger implementation, the summary would be generated by a model or a deterministic digest process. The key point is architectural: older detail moves into explicit carry-over state instead of vanishing silently.

## Extract Workspace Facts

The attached tutorial material also highlights fact extraction from transcripts. This lets the harness remember environment details without re-deriving them every time.

Useful facts include:

- active virtual environment or conda environment
- common test commands
- build commands
- important service hosts
- data directories

```python
import re


FACT_PATTERNS = {
    "venv_path": re.compile(r"([./~]\S+/bin/activate)"),
    "test_command": re.compile(r"\b(pytest|npm test|cargo test|go test)\b"),
    "build_command": re.compile(r"\b(npm run build|cargo build|make build|python -m build)\b"),
}


def extract_facts(text: str) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    for fact_type, pattern in FACT_PATTERNS.items():
        for match in pattern.finditer(text):
            facts.append({"type": fact_type, "value": match.group(1) if match.lastindex else match.group(0)})
    return facts
```

## Use Post-Session Hooks

Do not update knowledge and profile state inline during tool execution. Use a post-session hook or equivalent finalization step.

```python
class PostSessionKnowledgeHook:
    def __init__(self, dirs: AgentDirs) -> None:
        self.dirs = dirs

    async def run(self, snapshot: SessionSnapshot) -> None:
        transcript = "\n".join(message.content for message in snapshot.messages)
        facts = extract_facts(transcript)

        knowledge = WorkspaceKnowledge(project_name=self.dirs.workspace_root.name)
        knowledge.recent_tasks.append(snapshot.metadata.get("summary", "Completed session"))
        knowledge.facts.extend(facts)
        knowledge.session_count += 1

        self.dirs.knowledge_file.write_text(knowledge.to_markdown(), encoding="utf-8")
```

The example is intentionally simple. In a real harness you would load, merge, deduplicate, and then write the updated file.

## Protect File Writes

This chapter is also a good place to fix one of the practical hardening gaps surfaced in the audit documents: concurrent file safety.

When multiple processes or workers may update `.agent/knowledge.md`, `profile.md`, or session files, use:

- atomic writes to a temporary file followed by rename
- lock files when multiple writers are plausible
- version stamps if you prefer optimistic concurrency

Do not leave knowledge files vulnerable to partial writes.

## Action Plan

1. Add a directory resolver for local `.agent/` and global `~/.agent/` roots.
2. Represent workspace knowledge separately from durable memory and sessions.
3. Represent user profile data as stable preferences only.
4. Upgrade context compaction from a raw sliding window to recent-plus-carry-over behavior.
5. Extract reusable workspace facts from completed session transcripts.
6. Update knowledge and profile files only at session close.
7. Use atomic writes or lock-based coordination for shared files.

## Validation Checklist

- The harness can create and reuse local `.agent/knowledge.md`.
- The harness can create and reuse global `~/.agent/profile.md`.
- Compaction preserves recent turns and explicit carry-over state.
- Fact extraction does not mutate session history.
- Post-session updates can fail without corrupting the active turn.
- Concurrent writes to `.agent/` files are handled deliberately.

## Definition Of Done

This chapter is complete when the harness can remember the project and the operator without mixing those two scopes together. If workspace facts, user preferences, session history, and memory entries still blur together, the storage model is not mature enough yet.
