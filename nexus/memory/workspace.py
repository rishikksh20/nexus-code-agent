from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True, frozen=True)
class AgentDirs:
    workspace_root: Path
    global_root: Path

    @property
    def local_root(self) -> Path:
        return self.workspace_root / ".nexus"

    @property
    def local_config_file(self) -> Path:
        return self.local_root / "config.toml"

    @property
    def local_skills_dir(self) -> Path:
        return self.local_root / "skills"

    @property
    def global_config_file(self) -> Path:
        return self.global_root / "config.toml"

    @property
    def sessions_dir(self) -> Path:
        return self.local_root / "sessions"

    @property
    def memory_dir(self) -> Path:
        return self.local_root / "memory"

    @property
    def knowledge_file(self) -> Path:
        return self.local_root / "knowledge.md"

    @property
    def facts_file(self) -> Path:
        return self.local_root / "facts.json"

    @property
    def audit_trail_file(self) -> Path:
        return self.local_root / "audit-trail.jsonl"

    @property
    def profile_file(self) -> Path:
        return self.global_root / "profile.md"

    @property
    def workspaces_file(self) -> Path:
        return self.global_root / "workspaces.json"

    @property
    def tools_file(self) -> Path:
        return self.global_root / "tools.md"

    def ensure(self) -> None:
        for path in (self.global_root, self.local_root, self.sessions_dir, self.memory_dir, self.local_skills_dir):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class WorkspaceKnowledge:
    project_name: str
    description: str = ""
    tech_stack: list[str] = field(default_factory=list)
    conventions: list[str] = field(default_factory=list)
    source_of_truth: list[str] = field(default_factory=list)
    key_files: dict[str, str] = field(default_factory=dict)
    next_steps: list[str] = field(default_factory=list)
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
        if self.source_of_truth:
            lines.append("## Source Of Truth")
            lines.extend(f"- {item}" for item in self.source_of_truth)
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
        if self.facts:
            lines.append("## Facts")
            lines.extend(f"- {item['type']}: {item['value']}" for item in self.facts[-20:])
            lines.append("")
        lines.append(f"Session Count: {self.session_count}")
        lines.append("")
        if self.next_steps:
            lines.append("## Initial Next Steps")
            lines.extend(f"- {item}" for item in self.next_steps)
            lines.append("")
        return "\n".join(lines).strip() + "\n"


def bootstrap_workspace_knowledge(
    knowledge_file: Path,
    *,
    project_name: str,
    description: str = "",
) -> None:
    if knowledge_file.exists():
        return
    knowledge = WorkspaceKnowledge(
        project_name=project_name,
        description=description or "CLI-first Python agent harness workspace.",
        source_of_truth=["docs/action-plan", "docs/openai-code-tutorial"],
        conventions=[
            "Use Python 3.11+ with modular package boundaries.",
            "Keep runtime provider-neutral and OpenAI-compatible at the adapter boundary.",
            "Prefer explicit tool schemas and JSON-friendly outputs.",
        ],
        next_steps=[
            "Build from action-plan foundations first.",
            "Keep interactive and headless execution aligned.",
        ],
    )
    _atomic_write(knowledge_file, knowledge.to_markdown())


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
