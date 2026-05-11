# 15 — Advanced Context and Storage: Workspace Knowledge and User Profile

## Prerequisites

Complete [14-testing-the-harness.md](14-testing-the-harness.md) first.

You now have: sessions, memory, hooks, guardrails, and configuration. But each session starts mostly fresh — the agent does not know this project's conventions, the user's preferences, or what was already figured out two weeks ago.

This chapter adds two persistent context layers that make the agent genuinely smarter over time:

1. **Local `.agent/`** — workspace-scoped: learns the current project
2. **Global `~/.agent/`** — user-scoped: learns the person using the agent

---

## What you will build

```
{cwd}/
    .agent/
        sessions/           # session JSON files (from agent.toml: session.root)
        memory/             # memory entries (from agent.toml: memory.root)
        knowledge.md        # NEW: auto-updated workspace knowledge summary
        facts.json          # NEW: extracted environment facts (paths, envs, hosts)
        audit-trail.jsonl   # audit log (from Chapter 13)

~/.agent/
    profile.md              # NEW: user behavioral model and preferences
    workspaces.json         # NEW: registry of known workspaces with summaries
    tools.md                # NEW: user's preferred tools and working process

agent/
    agent_dir.py            # NEW: directory layout, path resolution
    workspace_knowledge.py  # NEW: WorkspaceKnowledge, KnowledgeUpdater, FactExtractor
    user_profile.py         # NEW: UserProfile, ProfileUpdater
    hooks.py                # updated: PostSessionKnowledgeHook
    prompts.py              # updated: ContextBuilder loads from .agent/ and ~/.agent/
agent.toml                  # updated: agent_dir and global_agent_dir paths
```

---

## 1. Design: two scopes, one principle

```
┌──────────────────────────────────────────────────────────────────┐
│   GLOBAL  ~/.agent/                                              │
│   "Who is this person?"                                          │
│   • programming language preferences                            │
│   • response style (terse vs verbose)                            │
│   • expertise level per domain                                   │
│   • recurring tools and workflows                                │
│   Updated: once per session (lightweight observation)            │
├──────────────────────────────────────────────────────────────────┤
│   LOCAL  {cwd}/.agent/                                           │
│   "What is this project?"                                        │
│   • tech stack, language, framework                              │
│   • key files and their purposes                                 │
│   • conventions and rules observed                               │
│   • recent tasks and their outcomes                              │
│   • environment facts (conda env, API hosts, data paths)         │
│   Updated: once per session (medium — rebuilds summary)          │
└──────────────────────────────────────────────────────────────────┘
```

The **single principle**: after every session ends, update both files. No real-time updates during a turn — only at session close. This keeps turns fast and makes the update an atomic operation.

---

## 2. Create `agent/agent_dir.py`

This module owns the directory layout for both scopes.

```python
# agent/agent_dir.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


AGENT_DIRNAME = ".agent"
GLOBAL_AGENT_DIRNAME = ".agent"


@dataclass
class AgentDirs:
    """
    Resolves all paths used by the agent harness.

    local_root  = {cwd}/.agent/         — workspace-scoped
    global_root = ~/.agent/             — user-scoped
    """
    local_root: Path
    global_root: Path

    # ── Local paths ───────────────────────────────────────────────
    @property
    def sessions(self) -> Path:
        return self.local_root / "sessions"

    @property
    def memory(self) -> Path:
        return self.local_root / "memory"

    @property
    def knowledge_file(self) -> Path:
        return self.local_root / "knowledge.md"

    @property
    def facts_file(self) -> Path:
        return self.local_root / "facts.json"

    @property
    def audit_log(self) -> Path:
        return self.local_root / "audit-trail.jsonl"

    # ── Global paths ──────────────────────────────────────────────
    @property
    def profile_file(self) -> Path:
        return self.global_root / "profile.md"

    @property
    def workspaces_file(self) -> Path:
        return self.global_root / "workspaces.json"

    @property
    def tools_file(self) -> Path:
        return self.global_root / "tools.md"

    # ── Init ──────────────────────────────────────────────────────
    def ensure(self) -> None:
        """Create all directories if they do not exist."""
        for d in [self.local_root, self.sessions, self.memory, self.global_root]:
            d.mkdir(parents=True, exist_ok=True)

    def summary(self) -> str:
        files = {
            "knowledge": self.knowledge_file.exists(),
            "profile":   self.profile_file.exists(),
            "facts":     self.facts_file.exists(),
        }
        found = [k for k, v in files.items() if v]
        return f"[.agent] {', '.join(found) or 'empty'}"


def resolve_agent_dirs(
    cwd: str | None = None,
    global_dir: str | None = None,
) -> AgentDirs:
    """
    Resolve agent directory paths.

    cwd defaults to os.getcwd().
    global_dir defaults to ~/.agent/.
    """
    local = Path(cwd or os.getcwd()) / AGENT_DIRNAME
    glob = Path(global_dir or Path.home() / GLOBAL_AGENT_DIRNAME)
    return AgentDirs(local_root=local, global_root=glob)
```

---

## 3. Create `agent/workspace_knowledge.py`

The workspace knowledge file is a structured Markdown document that grows richer across sessions.

```python
# agent/workspace_knowledge.py

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Fact extraction patterns ──────────────────────────────────────────────────
# Lightweight regex extraction — no LLM call needed

_FACT_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("conda_env",    "Conda environment",  re.compile(r"conda\s+activate\s+(\S+)", re.I)),
    ("python_ver",   "Python version",     re.compile(r"[Pp]ython\s*(3\.\d+(?:\.\d+)?)")),
    ("venv_path",    "Virtualenv path",    re.compile(r"(?:source\s+)?([./~]\S+/bin/activate)")),
    ("git_remote",   "Git remote",         re.compile(r"(?:github|gitlab|bitbucket)\.com[:/](\S+?)(?:\.git)?")),
    ("docker_image", "Docker image",       re.compile(r"FROM\s+([\w./:-]+)", re.I)),
    ("api_host",     "API host",           re.compile(r"(https?://[\w.-]+(?::\d+)?)/(?:api|v\d+)\b")),
    ("data_path",    "Data path",          re.compile(r"(/(?:data|mnt|ext|datasets|scratch)\S+)")),
    ("env_var",      "Environment var",    re.compile(r"export\s+([A-Z][A-Z0-9_]{2,})(?:=\S+)?")),
    ("port",         "Service port",       re.compile(r"(?:port|--port|-p)\s+(\d{4,5})\b", re.I)),
    ("test_cmd",     "Test command",       re.compile(r"\b(pytest|go test|npm test|cargo test|make test)\b")),
    ("build_cmd",    "Build command",      re.compile(r"\b(make|cargo build|npm run build|gradle build)\b")),
]


def extract_facts_from_text(text: str) -> list[dict[str, str]]:
    """
    Extract environment-specific facts from conversation text using patterns.
    Returns deduped list of {type, label, value} dicts.
    """
    facts: list[dict[str, str]] = []
    seen: set[str] = set()

    for fact_type, label, pattern in _FACT_PATTERNS:
        for match in pattern.finditer(text):
            value = (match.group(1) if match.lastindex else match.group(0)).strip().rstrip(".,;:)")
            if not value or len(value) < 3:
                continue
            key = f"{fact_type}:{value}"
            if key in seen:
                continue
            seen.add(key)
            facts.append({"type": fact_type, "label": label, "value": value})

    return facts


# ── Workspace knowledge ───────────────────────────────────────────────────────

@dataclass
class WorkspaceKnowledge:
    """
    Running knowledge base for the current workspace.

    Stored as .agent/knowledge.md (human-readable Markdown).
    Loaded into the agent prompt at the start of each session.
    Updated at session end.
    """
    # Core project identity
    project_name: str = ""
    description: str = ""
    tech_stack: list[str] = field(default_factory=list)
    language: str = ""
    framework: str = ""

    # Structural knowledge
    key_files: dict[str, str] = field(default_factory=dict)   # path → purpose
    conventions: list[str] = field(default_factory=list)

    # Extracted environment facts
    facts: list[dict[str, str]] = field(default_factory=list)

    # Session history (recent only)
    recent_tasks: list[str] = field(default_factory=list)      # last 10 task summaries
    last_updated: str = ""
    session_count: int = 0

    # ── Serialization ─────────────────────────────────────────────

    def to_markdown(self) -> str:
        """Render knowledge as a Markdown document."""
        lines = ["# Workspace Knowledge\n"]
        ts = self.last_updated or "never"
        lines.append(f"*Last updated: {ts} | Sessions: {self.session_count}*\n")

        if self.project_name:
            lines.append(f"\n## Project: {self.project_name}\n")
            if self.description:
                lines.append(f"{self.description}\n")

        if self.language or self.framework or self.tech_stack:
            lines.append("\n## Tech Stack\n")
            if self.language:
                lines.append(f"- **Language:** {self.language}")
            if self.framework:
                lines.append(f"- **Framework:** {self.framework}")
            for item in self.tech_stack:
                if item not in (self.language, self.framework):
                    lines.append(f"- {item}")
            lines.append("")

        if self.key_files:
            lines.append("\n## Key Files\n")
            for path, purpose in list(self.key_files.items())[:15]:
                lines.append(f"- `{path}` — {purpose}")
            lines.append("")

        if self.conventions:
            lines.append("\n## Conventions Observed\n")
            for c in self.conventions[:10]:
                lines.append(f"- {c}")
            lines.append("")

        if self.facts:
            lines.append("\n## Environment Facts\n")
            for fact in self.facts[:20]:
                lines.append(f"- **{fact['label']}:** `{fact['value']}`")
            lines.append("")

        if self.recent_tasks:
            lines.append("\n## Recent Tasks\n")
            for task in self.recent_tasks[-8:]:
                lines.append(f"- {task}")
            lines.append("")

        return "\n".join(lines)

    def to_prompt_section(self, max_chars: int = 1500) -> str:
        """
        Return a compact version suitable for inclusion in the system prompt.
        Shorter than the full knowledge.md — only the most relevant parts.
        """
        parts = []
        if self.project_name:
            parts.append(f"Project: {self.project_name}")
        if self.description:
            parts.append(self.description[:200])
        if self.language:
            parts.append(f"Language: {self.language}")
        if self.framework:
            parts.append(f"Framework: {self.framework}")
        if self.conventions:
            parts.append("Conventions: " + "; ".join(self.conventions[:5]))
        if self.facts:
            fact_lines = [f"{f['label']}: {f['value']}" for f in self.facts[:8]]
            parts.append("Environment: " + ", ".join(fact_lines))
        if self.recent_tasks:
            parts.append("Recent work: " + " | ".join(self.recent_tasks[-3:]))

        return "\n".join(parts)[:max_chars]

    # ── Persistence ────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """Write knowledge.md to disk."""
        path.write_text(self.to_markdown(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "WorkspaceKnowledge":
        """
        Load from knowledge.md.

        We store the structured data as a JSON comment at the top of the file
        so it can be both human-readable and machine-parseable.
        """
        if not path.exists():
            return cls()
        content = path.read_text(encoding="utf-8")
        # Try to extract embedded JSON front-matter (HTML comment)
        match = re.search(r"<!--KNOWLEDGE_JSON\n(.*?)\n-->", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        # Fall back to empty (Markdown-only, not machine-parseable yet)
        return cls()

    def save_with_json(self, path: Path) -> None:
        """Write knowledge.md with embedded JSON front-matter for lossless round-trips."""
        import dataclasses
        json_block = json.dumps(dataclasses.asdict(self), indent=2)
        content = f"<!--KNOWLEDGE_JSON\n{json_block}\n-->\n\n" + self.to_markdown()
        path.write_text(content, encoding="utf-8")

    @classmethod
    def load_with_json(cls, path: Path) -> "WorkspaceKnowledge":
        if not path.exists():
            return cls()
        content = path.read_text(encoding="utf-8")
        match = re.search(r"<!--KNOWLEDGE_JSON\n(.*?)\n-->", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        return cls()


# ── Updater ───────────────────────────────────────────────────────────────────

class KnowledgeUpdater:
    """
    Updates WorkspaceKnowledge from a completed session.

    Called at session end. Merges new facts, appends task summary,
    and saves knowledge.md.

    Design: heuristic-first. No LLM call required for basic updates.
    If a model client is available, it can be used for richer summarization.
    """

    def __init__(
        self,
        knowledge_path: Path,
        facts_path: Path,
        model_client=None,          # optional: used for richer summarization
    ) -> None:
        self.knowledge_path = knowledge_path
        self.facts_path = facts_path
        self.model_client = model_client

    def update_from_session(
        self,
        messages: list[Any],
        task_summary: str = "",
        cwd: str = ".",
    ) -> WorkspaceKnowledge:
        """
        Extract new knowledge from the completed session and merge with existing.

        Args:
            messages:     All messages from the session (list of Message objects or dicts)
            task_summary: Short description of what was accomplished this session
            cwd:          Working directory (used for project name inference)

        Returns:
            Updated WorkspaceKnowledge (also saved to disk).
        """
        existing = WorkspaceKnowledge.load_with_json(self.knowledge_path)

        # 1. Extract facts from conversation text
        all_text = self._messages_to_text(messages)
        new_facts = extract_facts_from_text(all_text)
        merged_facts = self._merge_facts(existing.facts, new_facts)

        # 2. Infer project identity if not yet set
        if not existing.project_name:
            existing.project_name = Path(cwd).name
        if not existing.language:
            existing.language = self._infer_language(cwd)

        # 3. Infer tech stack from facts and file extensions
        stack = set(existing.tech_stack)
        stack.update(self._infer_stack_from_facts(merged_facts))
        stack.update(self._infer_stack_from_cwd(cwd))

        # 4. Update key files from tool calls (read_file / write_file)
        new_key_files = self._extract_key_files(messages, cwd)

        # 5. Append task summary to recent_tasks (keep last 10)
        recent = list(existing.recent_tasks)
        if task_summary:
            recent.append(task_summary)
        recent = recent[-10:]

        # 6. Build updated knowledge
        updated = WorkspaceKnowledge(
            project_name=existing.project_name,
            description=existing.description,
            language=existing.language,
            framework=existing.framework,
            tech_stack=sorted(stack),
            key_files={**existing.key_files, **new_key_files},
            conventions=existing.conventions,
            facts=merged_facts,
            recent_tasks=recent,
            last_updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            session_count=existing.session_count + 1,
        )

        # 7. Save
        updated.save_with_json(self.knowledge_path)
        self._save_facts_json(merged_facts)

        return updated

    # ── Private helpers ────────────────────────────────────────────

    def _messages_to_text(self, messages: list) -> str:
        parts = []
        for msg in messages:
            content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, "content", [])
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content") or ""
                    if isinstance(text, str):
                        parts.append(text)
        return "\n".join(parts)

    def _merge_facts(
        self, existing: list[dict], new: list[dict]
    ) -> list[dict]:
        """Merge new facts into existing, deduping by type+value."""
        seen = {f"{f['type']}:{f['value']}" for f in existing}
        merged = list(existing)
        for f in new:
            key = f"{f['type']}:{f['value']}"
            if key not in seen:
                merged.append(f)
                seen.add(key)
        return merged[:50]  # hard cap to avoid unbounded growth

    def _infer_language(self, cwd: str) -> str:
        counts: dict[str, int] = {}
        ext_map = {
            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
            ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
            ".cpp": "C++", ".c": "C",
        }
        try:
            for p in Path(cwd).rglob("*"):
                if p.suffix in ext_map and ".git" not in str(p) and "__pycache__" not in str(p):
                    lang = ext_map[p.suffix]
                    counts[lang] = counts.get(lang, 0) + 1
        except Exception:
            pass
        return max(counts, key=counts.get) if counts else ""

    def _infer_stack_from_facts(self, facts: list[dict]) -> list[str]:
        stack = []
        for f in facts:
            if f["type"] == "conda_env":
                stack.append("Conda")
            elif f["type"] == "docker_image":
                stack.append("Docker")
            elif f["type"] == "test_cmd" and "pytest" in f["value"]:
                stack.append("pytest")
            elif f["type"] == "test_cmd" and "go test" in f["value"]:
                stack.append("Go")
        return stack

    def _infer_stack_from_cwd(self, cwd: str) -> list[str]:
        stack = []
        markers = {
            "pyproject.toml": "Python", "setup.py": "Python", "requirements.txt": "Python",
            "package.json": "Node.js", "yarn.lock": "Yarn", "go.mod": "Go",
            "Cargo.toml": "Rust", "build.gradle": "Gradle", "pom.xml": "Maven",
            "Dockerfile": "Docker", "docker-compose.yml": "Docker Compose",
            ".github": "GitHub Actions",
        }
        try:
            cwd_path = Path(cwd)
            for marker, label in markers.items():
                if (cwd_path / marker).exists():
                    stack.append(label)
        except Exception:
            pass
        return stack

    def _extract_key_files(self, messages: list, cwd: str) -> dict[str, str]:
        """Extract file paths accessed via read_file / write_file tool calls."""
        key_files: dict[str, str] = {}
        for msg in messages:
            content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, "content", [])
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    # Check if the previous tool call was read_file or write_file
                    pass  # simplified — real impl parses tool_call blocks
        return key_files

    def _save_facts_json(self, facts: list[dict]) -> None:
        self.facts_path.write_text(
            json.dumps({"facts": facts, "count": len(facts)}, indent=2),
            encoding="utf-8",
        )
```

---

## 4. Create `agent/user_profile.py`

The user profile accumulates behavioral observations across all workspaces.

```python
# agent/user_profile.py

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class UserProfile:
    """
    A progressively built model of the user's preferences and patterns.

    Stored as ~/.agent/profile.md — one file per user, shared across all projects.

    Updated at session end. Never overwritten — only merged and extended.
    """
    # Identity and style
    preferred_name: str = ""                    # how the user likes to be addressed
    response_length: str = "medium"             # "terse" | "medium" | "detailed"
    response_style: str = "direct"              # "direct" | "teaching" | "collaborative"
    language: str = "English"                   # communication language

    # Expertise signals (extracted from what user asks vs what they already know)
    expertise: dict[str, str] = field(default_factory=dict)  # domain → "beginner"|"intermediate"|"expert"

    # Behavioral patterns observed
    preferred_tools: list[str] = field(default_factory=list)    # tools user relies on
    coding_style: list[str] = field(default_factory=list)       # e.g. "prefers type hints", "uses dataclasses"
    workflow_patterns: list[str] = field(default_factory=list)  # e.g. "test first", "reads before writes"
    avoided_patterns: list[str] = field(default_factory=list)   # patterns user dislikes

    # Cross-workspace context
    active_projects: list[str] = field(default_factory=list)    # recent project names
    total_sessions: int = 0
    last_seen: str = ""

    # ── Serialization ─────────────────────────────────────────────

    def to_markdown(self) -> str:
        ts = self.last_seen or "never"
        name = self.preferred_name or "User"
        lines = [
            f"# User Profile: {name}",
            f"*Last updated: {ts} | Total sessions: {self.total_sessions}*\n",
        ]

        lines.append("## Preferences\n")
        lines.append(f"- **Response length:** {self.response_length}")
        lines.append(f"- **Response style:** {self.response_style}")
        lines.append(f"- **Language:** {self.language}\n")

        if self.expertise:
            lines.append("## Expertise\n")
            for domain, level in self.expertise.items():
                lines.append(f"- **{domain}:** {level}")
            lines.append("")

        if self.coding_style:
            lines.append("## Coding Style\n")
            for s in self.coding_style[:8]:
                lines.append(f"- {s}")
            lines.append("")

        if self.workflow_patterns:
            lines.append("## Workflow Patterns\n")
            for p in self.workflow_patterns[:8]:
                lines.append(f"- {p}")
            lines.append("")

        if self.preferred_tools:
            lines.append("## Preferred Tools\n")
            for t in self.preferred_tools[:10]:
                lines.append(f"- {t}")
            lines.append("")

        if self.active_projects:
            lines.append("## Recent Projects\n")
            for p in self.active_projects[-5:]:
                lines.append(f"- {p}")
            lines.append("")

        return "\n".join(lines)

    def to_prompt_section(self, max_chars: int = 600) -> str:
        """Compact version for system prompt injection."""
        parts = []
        if self.preferred_name:
            parts.append(f"The user's name is {self.preferred_name}.")
        if self.response_length != "medium":
            parts.append(f"Preferred response length: {self.response_length}.")
        if self.response_style != "direct":
            parts.append(f"Preferred style: {self.response_style}.")
        if self.coding_style:
            parts.append("User coding style: " + "; ".join(self.coding_style[:3]) + ".")
        if self.workflow_patterns:
            parts.append("User workflow: " + "; ".join(self.workflow_patterns[:2]) + ".")
        if self.expertise:
            exp_str = ", ".join(f"{d}: {l}" for d, l in list(self.expertise.items())[:4])
            parts.append(f"Expertise: {exp_str}.")
        return " ".join(parts)[:max_chars]

    # ── Persistence ─────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """Write profile.md with embedded JSON front-matter."""
        import dataclasses
        json_block = json.dumps(dataclasses.asdict(self), indent=2)
        content = f"<!--PROFILE_JSON\n{json_block}\n-->\n\n" + self.to_markdown()
        path.write_text(content, encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "UserProfile":
        if not path.exists():
            return cls()
        content = path.read_text(encoding="utf-8")
        match = re.search(r"<!--PROFILE_JSON\n(.*?)\n-->", content, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        return cls()


class ProfileUpdater:
    """
    Observes session behavior and updates the user profile.

    Heuristic-based: detects patterns from tool usage, question types,
    and implicit style signals in the conversation.
    """

    def __init__(self, profile_path: Path, workspaces_path: Path) -> None:
        self.profile_path = profile_path
        self.workspaces_path = workspaces_path

    def update_from_session(
        self,
        messages: list[Any],
        project_name: str = "",
        tool_calls_made: list[str] | None = None,
    ) -> UserProfile:
        """
        Observe session and merge new signals into the profile.
        Called at session end.
        """
        profile = UserProfile.load(self.profile_path)

        # Update counters
        profile.total_sessions += 1
        profile.last_seen = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Track active projects
        if project_name and project_name not in profile.active_projects:
            profile.active_projects.append(project_name)
        profile.active_projects = profile.active_projects[-10:]

        # Update preferred tools from actual usage
        if tool_calls_made:
            for tool in tool_calls_made:
                if tool not in profile.preferred_tools and tool not in {
                    "get_time", "ask_user_question", "check_my_mailbox"
                }:
                    profile.preferred_tools.append(tool)
            profile.preferred_tools = list(dict.fromkeys(profile.preferred_tools))[:15]

        # Signal extraction from conversation
        all_text = self._to_text(messages)
        self._detect_coding_style(profile, all_text)
        self._detect_workflow_patterns(profile, all_text, tool_calls_made or [])
        self._detect_response_preference(profile, all_text)

        profile.save(self.profile_path)
        self._update_workspaces_registry(project_name)
        return profile

    def _to_text(self, messages: list) -> str:
        parts = []
        for msg in messages:
            content = msg.get("content", []) if isinstance(msg, dict) else getattr(msg, "content", [])
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict):
                    text = block.get("text") or ""
                    if isinstance(text, str) and block.get("type") != "tool_result":
                        parts.append(text)
        return "\n".join(parts)

    def _detect_coding_style(self, profile: UserProfile, text: str) -> None:
        signals = [
            (r"\btype hints?\b|\btyped?\b",        "uses type annotations"),
            (r"\bdataclass\b",                      "uses dataclasses"),
            (r"\bpydantic\b",                       "uses Pydantic for validation"),
            (r"\bblack\b|\brifff\b|\bformatter\b",  "uses formatter (black/ruff)"),
            (r"\bdocstring\b",                      "writes docstrings"),
            (r"\basync\b.{0,20}\bawait\b",          "writes async code"),
            (r"\bfunctional\b|\bland?a\b",          "functional style"),
            (r"\bclass\b.{0,10}\bABC\b",            "uses abstract base classes"),
        ]
        existing = set(profile.coding_style)
        for pattern, label in signals:
            if label not in existing and re.search(pattern, text, re.I):
                profile.coding_style.append(label)
        profile.coding_style = profile.coding_style[:10]

    def _detect_workflow_patterns(
        self, profile: UserProfile, text: str, tools: list[str]
    ) -> None:
        existing = set(profile.workflow_patterns)
        patterns: list[tuple[str, str]] = [
            (r"\btest\b.*\bfirst\b|\bTDD\b",        "test-first workflow"),
            (r"\bread\b.*\bbefore\b.*\bwrit",        "reads before writing"),
            (r"\bplan\b.*\bfirst\b|\bplan mode\b",   "plans before executing"),
            (r"\bminimal change\b|\bsmallest\b",     "prefers minimal changes"),
            (r"\bgit\s+(commit|add|push)\b",         "uses git regularly"),
        ]
        for pattern, label in patterns:
            if label not in existing and re.search(pattern, text, re.I):
                profile.workflow_patterns.append(label)
        # Tool-based patterns
        if "read_file" in tools and "write_file" in tools:
            if "reads before writing" not in existing:
                profile.workflow_patterns.append("reads before writing")
        profile.workflow_patterns = profile.workflow_patterns[:10]

    def _detect_response_preference(self, profile: UserProfile, text: str) -> None:
        """Detect user's preference for response length/style."""
        if re.search(r"\b(shorter|brief|concise|tl;dr|summary)\b", text, re.I):
            profile.response_length = "terse"
        elif re.search(r"\b(explain|detailed|elaborate|step.by.step)\b", text, re.I):
            profile.response_length = "detailed"

    def _update_workspaces_registry(self, project_name: str) -> None:
        """Keep a lightweight registry of known workspaces."""
        registry: dict = {}
        if self.workspaces_path.exists():
            try:
                registry = json.loads(self.workspaces_path.read_text())
            except Exception:
                pass

        if project_name:
            registry[project_name] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            # Keep only last 50 workspaces
            if len(registry) > 50:
                sorted_keys = sorted(registry, key=lambda k: registry[k])
                for old in sorted_keys[:len(registry) - 50]:
                    del registry[old]

        self.workspaces_path.write_text(
            json.dumps(registry, indent=2), encoding="utf-8"
        )
```

---

## 5. Add `PostSessionKnowledgeHook` to `agent/hooks.py`

```python
# agent/hooks.py  — add PostSessionKnowledgeHook

from agent.workspace_knowledge import KnowledgeUpdater
from agent.user_profile import ProfileUpdater

class PostSessionKnowledgeHook:
    """
    Runs at session end (HookEvent.STOP).
    Updates both workspace knowledge and user profile from the completed session.
    """
    event = HookEvent.STOP

    def __init__(
        self,
        knowledge_updater: KnowledgeUpdater,
        profile_updater: ProfileUpdater,
        agent: Any,   # reference to the agent for messages + tool_call_count
    ) -> None:
        self._knowledge = knowledge_updater
        self._profile = profile_updater
        self._agent = agent

    async def run(self, payload: dict[str, Any]) -> HookResult:
        """Called when the agent turn ends. Performs post-session learning."""
        try:
            # Collect tool names used this session
            tools_used = getattr(self._agent, "_tools_used_this_session", [])
            cwd = getattr(self._agent, "cwd", ".")
            project = Path(cwd).name

            # Build a 1-line task summary from the first user message
            task_summary = ""
            if self._agent.messages:
                first = self._agent.messages[0]
                text = first.text if hasattr(first, "text") else ""
                task_summary = text[:80].replace("\n", " ")

            # Update workspace knowledge
            self._knowledge.update_from_session(
                messages=[m.to_dict() if hasattr(m, "to_dict") else m
                          for m in self._agent.messages],
                task_summary=task_summary,
                cwd=cwd,
            )

            # Update user profile
            self._profile.update_from_session(
                messages=[m.to_dict() if hasattr(m, "to_dict") else m
                          for m in self._agent.messages],
                project_name=project,
                tool_calls_made=tools_used,
            )
        except Exception as exc:
            # Never let a hook update crash the agent
            pass

        return HookResult.allow()
```

Track tools used in `Agent.run()`:

```python
# agent/agent.py  — track tools used per session

class Agent:
    def __init__(self, ...):
        # ...existing...
        self._tools_used_this_session: list[str] = []

    async def run(self, user_text: str):
        # ...existing...
        for tool_call in response.tool_calls:
            # After executing:
            if tool_call.name not in self._tools_used_this_session:
                self._tools_used_this_session.append(tool_call.name)
```

---

## 6. Update `ContextBuilder` to inject both context layers

```python
# agent/prompts.py  — add workspace and profile sections to ContextBuilder

from agent.workspace_knowledge import WorkspaceKnowledge
from agent.user_profile import UserProfile

class ContextBuilder:
    # ...existing...

    def add_workspace_knowledge(self, knowledge: WorkspaceKnowledge) -> "ContextBuilder":
        """Inject workspace knowledge into the system prompt."""
        section = knowledge.to_prompt_section(max_chars=1500)
        if section:
            self._blocks.append(("workspace", section))
        return self

    def add_user_profile(self, profile: UserProfile) -> "ContextBuilder":
        """Inject user profile preferences into the system prompt."""
        section = profile.to_prompt_section(max_chars=600)
        if section:
            self._blocks.append(("user_profile", section))
        return self

    def build(self) -> str:
        # ...existing build logic...
        # Add these sections between "base_prompt" and "memory":
        SECTION_ORDER = [
            "base_prompt",
            "user_profile",    # ← new: user preferences first
            "workspace",       # ← new: project context
            "mode_guidance",
            "skills",
            "memory",
            "recent_files",
            "task_context",
        ]
```

---

## 7. Update `main.py` to wire it all together

```python
# main.py  — updated build_agent() with .agent/ directory

from agent.agent_dir import resolve_agent_dirs, AgentDirs
from agent.workspace_knowledge import WorkspaceKnowledge, KnowledgeUpdater
from agent.user_profile import UserProfile, ProfileUpdater
from agent.hooks import PostSessionKnowledgeHook

def build_agent(config: AgentConfig, mode_override: str | None = None) -> Agent:
    import os

    # ── Resolve .agent/ directories ────────────────────────────────
    dirs = resolve_agent_dirs(
        cwd=os.getcwd(),
        global_dir=config.agent.global_dir if hasattr(config, "agent") else None,
    )
    dirs.ensure()
    print(f"  {dirs.summary()}")

    # ── Point sessions and memory at .agent/ ───────────────────────
    # Override config paths to use .agent/ subdirs
    session_root = dirs.sessions
    memory_root = dirs.memory

    # ── Load existing knowledge and profile for this session ───────
    knowledge = WorkspaceKnowledge.load_with_json(dirs.knowledge_file)
    profile = UserProfile.load(dirs.profile_file)

    if knowledge.project_name:
        print(f"  [knowledge] {knowledge.project_name} | {knowledge.session_count} sessions")
    if profile.total_sessions:
        print(f"  [profile] {profile.total_sessions} sessions | tools: {', '.join(profile.preferred_tools[:3])}")

    # ── Build core components ───────────────────────────────────────
    memory_store = MemoryStore(root=memory_root)
    registry = default_registry(memory_store=memory_store)

    # Build context with both layers
    def build_system_prompt(user_text: str = "") -> str:
        builder = ContextBuilder(base_prompt=DEFAULT_BASE_PROMPT)
        builder.add_user_profile(profile)
        builder.add_workspace_knowledge(knowledge)
        # ...existing context building...
        return builder.build()

    executor = HookExecutor()
    # ...existing hooks...

    # Create updaters for post-session learning
    knowledge_updater = KnowledgeUpdater(
        knowledge_path=dirs.knowledge_file,
        facts_path=dirs.facts_file,
    )
    profile_updater = ProfileUpdater(
        profile_path=dirs.profile_file,
        workspaces_path=dirs.workspaces_file,
    )

    # Build agent first, then add the knowledge hook that references it
    agent = Agent(
        model_client=_build_model_client(config),
        tool_registry=registry,
        cwd=os.getcwd(),
        model_name=config.model.name,
        hook_executor=executor,
        permission_checker=PermissionChecker(policy=policy),
        memory_store=memory_store,
        mode=ExecutionMode(mode_override or config.mode.default),
        context_window=config.model.context_window,
    )

    # Now register the post-session learning hook
    executor.register(PostSessionKnowledgeHook(
        knowledge_updater=knowledge_updater,
        profile_updater=profile_updater,
        agent=agent,
    ))

    return agent
```

---

## 8. Update `agent.toml`

```toml
# agent.toml  — add agent directory settings

[agent]
# Local workspace directory (relative to CWD)
local_dir   = ".agent"

# Global user directory (absolute path, ~ is expanded)
global_dir  = "~/.agent"

# Whether to auto-commit knowledge.md to git after each session
# Useful for project wikis. Requires git to be installed.
git_commit_knowledge = false

[session]
# Now points into .agent/ (override if using custom layout)
root = ".agent/sessions"

[memory]
root = ".agent/memory"
```

---

## 9. View and edit the knowledge files directly

Because both files are plain Markdown, the user can read and edit them:

```bash
# See what the agent knows about the current project
cat .agent/knowledge.md

# Edit project description manually
$EDITOR .agent/knowledge.md

# See the user profile
cat ~/.agent/profile.md

# See all known workspaces
cat ~/.agent/workspaces.json
```

This is intentional. The agent learns, but the user has full transparency and control over what was learned.

---

## 10. What a session lifecycle looks like

```
Session start
    │
    ▼
resolve_agent_dirs()                     .agent/ and ~/.agent/ created if missing
    │
    ├─ WorkspaceKnowledge.load_with_json()  loads project context
    └─ UserProfile.load()                   loads user preferences
    │
    ▼
ContextBuilder
    ├─ add_user_profile(profile)            "User prefers terse answers; uses type hints"
    ├─ add_workspace_knowledge(knowledge)   "Project: FastAPI service; Python; pytest"
    └─ add_memory(...)                      per-turn relevant entries
    │
    ▼
Agent turns run...
    │
    ▼
Session end (HookEvent.STOP fires)
    │
    ├─ KnowledgeUpdater.update_from_session()
    │       extract_facts_from_text()         new conda env, API host, etc.
    │       _infer_language(), _infer_stack()  scan CWD for file types
    │       save .agent/knowledge.md            merged and updated
    │
    └─ ProfileUpdater.update_from_session()
            _detect_coding_style()             "uses type annotations"
            _detect_workflow_patterns()        "reads before writing"
            _update_workspaces_registry()      adds project to ~/.agent/workspaces.json
            save ~/.agent/profile.md           merged and updated
```

---

## 11. What NOT to store in these files

| Don't store | Why | Use instead |
|---|---|---|
| API keys, tokens | Security risk — file is human-readable | `.env` / env vars |
| Full tool output | Too large, stale quickly | Per-turn memory with TTL |
| Raw conversation transcripts | Already in session JSON | `SessionStore` |
| Process state (PIDs, handles) | Dies on restart | Rebuild fresh each run |
| Highly volatile data | Will be wrong before next read | Carry-over in snapshot |

---

## 12. Gitignore recommendations

```gitignore
# .gitignore

# Session history and audio (personal)
.agent/sessions/
.agent/audit-trail.jsonl
.agent/facts.json

# Commit these (useful for team shared context):
# .agent/knowledge.md     ← project knowledge is team-shareable
# .agent/memory/          ← careful: may contain personal facts

# Global user profile (never commit)
~/.agent/
```

Committing `.agent/knowledge.md` to the project repo is optional but valuable in team settings — it gives every team member's agent the same project context on first run.

---

## 13. Two-tier update cadence reference

| File | Updated when | Mechanism | Cost |
|---|---|---|---|
| `.agent/knowledge.md` | End of every session | `PostSessionKnowledgeHook` | ~100ms (heuristics) |
| `.agent/memory/*.md` | Any turn (explicit save) | `SaveMemoryTool` called by model | ~10ms |
| `~/.agent/profile.md` | End of every session | `PostSessionKnowledgeHook` | ~50ms |
| `~/.agent/workspaces.json` | End of every session | `ProfileUpdater` | ~5ms |
| `.agent/sessions/*.json` | End of every session | `SessionStore.save()` | ~20ms |

---

## 14. Exercises

**Exercise A — Manual profile editing tool**

Add an `EditProfileTool` that lets the model update the user profile interactively:
```python
you> my name is Alex and I prefer very terse responses
→ model calls edit_profile(field="preferred_name", value="Alex")
→ model calls edit_profile(field="response_length", value="terse")
```
This gives the user direct control alongside the passive observation.

**Exercise B — Workspace briefing on session start**

At the very start of a new session (before the user types anything), emit a `StatusEvent` that summarizes what was loaded:
```
[knowledge] FastAPI service | Python/pytest | 12 sessions | last: 2026-04-24
[profile] Alex | terse | tools: read_file, write_file, glob
```

**Exercise C — Git-committed knowledge**

When `agent.git_commit_knowledge = true`, after saving `.agent/knowledge.md`, run:
```bash
git add .agent/knowledge.md && git commit -m "chore: update agent knowledge (session $(date +%Y%m%d))"
```
Make this optional and guarded by `docker_available()`-style check that git exists and the directory is a git repo.

**Exercise D — Knowledge diff**

Before updating `.agent/knowledge.md`, compute a diff:
- new_facts that were not previously known
- new_tasks added to recent_tasks
Emit a compact `StatusEvent` showing what was learned: `"Learned: conda env 'ml', 2 new facts"`.

---

## 15. Checklist before moving on

- [ ] `AgentDirs` resolves local `.agent/` and global `~/.agent/` directories
- [ ] `AgentDirs.ensure()` creates both directories and subdirectories if missing
- [ ] `agent.toml` has `[agent]` section with `local_dir` and `global_dir`
- [ ] `WorkspaceKnowledge` is stored as Markdown with embedded JSON front-matter
- [ ] `WorkspaceKnowledge.load_with_json()` / `save_with_json()` round-trip losslessly
- [ ] `KnowledgeUpdater.update_from_session()` runs at session end, not per turn
- [ ] `extract_facts_from_text()` extracts conda envs, git remotes, API hosts, ports
- [ ] `_infer_language()` scans CWD file extensions to detect primary language
- [ ] `UserProfile` is stored in `~/.agent/profile.md` with embedded JSON front-matter
- [ ] `ProfileUpdater` is heuristic-only — no LLM call required for basic patterns
- [ ] `PostSessionKnowledgeHook` is registered on `HookEvent.STOP`
- [ ] `ContextBuilder.add_workspace_knowledge()` and `add_user_profile()` exist
- [ ] User profile section appears before workspace section in the system prompt
- [ ] `.gitignore` recommends committing `knowledge.md` but not `sessions/` or `facts.json`
- [ ] Both files are human-readable and the user can edit them directly

---

*You now have a self-improving agent that gets smarter about the project and more aligned with the user across every session.*

Next: continue to [16-advanced-logging-and-observability.md](16-advanced-logging-and-observability.md) to make the harness observable, cost-aware, and traceable in production-like environments.

