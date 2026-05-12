from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.config.defaults import AgentConfig
from nexus.runtime.context import CarryOverState, ContextSections
from nexus.tools.base import ToolRegistry

if TYPE_CHECKING:
    from nexus.skills import SkillRegistry


DEFAULT_BASE_INSTRUCTION = (
    "You are Nexus, an AI coding agent and terminal-based coding assistant. Keep provider-specific wire formats "
    "outside the runtime boundary, use tool calls explicitly, and prefer concise JSON-friendly "
    "responses when structured output helps engineering tasks."
)


def build_context_sections(
    config: AgentConfig,
    tool_registry: ToolRegistry,
    *,
    task_input: str,
    execution_mode: str | None = None,
    skill_registry: SkillRegistry | None = None,
    active_skills: list[str] | None = None,
    carry_over: CarryOverState | None = None,
    current_working_directory: Path | None = None,
) -> ContextSections:
    project_notes = [f"Project: {config.project_name}"]
    if config.project_description:
        project_notes.append(config.project_description)
    if config.knowledge_file.exists():
        project_notes.extend(_read_knowledge_summary(config.knowledge_file))

    skills: list[str] = []
    if skill_registry is not None and skill_registry.all():
        skills.append(skill_registry.summary())
        for skill_name in active_skills or []:
            skill = skill_registry.get(skill_name)
            if skill is None:
                continue
            skills.append(f"Active Skill: {skill.name}\n\n{skill.content.strip()}")

    carry_over_notes: list[str] = []
    if carry_over is not None:
        carry_over_notes.extend(carry_over.pinned_facts[-5:])
        carry_over_notes.extend(carry_over.summarized_history[-3:])
        carry_over_notes.extend(carry_over.active_constraints[-3:])

    return ContextSections(
        base_instruction=DEFAULT_BASE_INSTRUCTION,
        environment=[
            f"Current UTC time: {_current_utc_time()}",
            f"Current working directory: {(current_working_directory or Path.cwd()).resolve()}",
            f"Workspace: {config.workspace_root}",
            f"Mode: {execution_mode or config.default_mode}",
            f"Provider adapter: {config.provider}",
        ],
        tools=[
            f"[{record.source}] {record.name}: {record.tool.description}"
            for record in tool_registry.records()
        ],
        skills=skills,
        project_notes=project_notes,
        carry_over=carry_over_notes,
        task_focus=[task_input],
    )


def _read_knowledge_summary(path: Path) -> list[str]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    return lines[:5]


def _current_utc_time() -> str:
    return datetime.now(UTC).isoformat()

