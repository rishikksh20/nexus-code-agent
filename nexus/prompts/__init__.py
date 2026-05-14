"""nexus.prompts — system-prompt construction for the Nexus agent.

Public surface
--------------
- :func:`build_context_sections` — main entry point; returns a
  :class:`~nexus.context.ContextSections` from config + tool/skill registries.
- :func:`_current_utc_time` — exposed at package level so tests can monkeypatch it.
- :mod:`nexus.prompts.system` — static base-instruction sections (including
  :func:`~nexus.prompts.system.create_loop_breaker_prompt`).
- :mod:`nexus.prompts.compression` — LLM-based compaction prompt.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.config.defaults import AgentConfig
from nexus.context.builder import ContextSections
from nexus.context.compactor import CarryOverState
from nexus.prompts.system import build_base_instruction
from nexus.tools.base import ToolRegistry

if TYPE_CHECKING:
    from nexus.skills import SkillRegistry


def _current_utc_time() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Defined at package level so tests can monkeypatch ``nexus.prompts._current_utc_time``.
    """
    return datetime.now(UTC).isoformat()


def build_context_sections(
    config: AgentConfig,
    tool_registry: ToolRegistry,
    *,
    task_input: str,
    execution_mode: str | None = None,
    skill_registry: "SkillRegistry | None" = None,
    active_skills: list[str] | None = None,
    carry_over: CarryOverState | None = None,
    current_working_directory: Path | None = None,
    user_instructions: str = "",
    memory_entries: list[str] | None = None,
) -> ContextSections:
    """Assemble a :class:`~nexus.context.ContextSections` from runtime state.

    Parameters
    ----------
    config:
        Active :class:`~nexus.config.defaults.AgentConfig`.
    tool_registry:
        Registry of all available tools.
    task_input:
        The current user task description (injected as ``task_focus``).
    execution_mode:
        Override for the configured default mode (e.g. ``"plan"``, ``"auto"``).
    skill_registry:
        Optional skill registry; used to populate the ``skills`` section.
    active_skills:
        Names of skills that are currently active.
    carry_over:
        Compacted context carried over from previous compaction rounds.
    current_working_directory:
        Override for the agent's effective CWD (defaults to ``Path.cwd()``).
    user_instructions:
        Optional user-level custom instructions forwarded into the base instruction.
    memory_entries:
        Pre-formatted memory strings (``"key: content"`` lines) loaded from the
        :class:`~nexus.memory.store.MemoryStore`.  When supplied they are
        surfaced in the ``## Persistent Memory`` section of the system prompt so
        the agent always has access to stored user/context knowledge regardless
        of the current query.

    Returns
    -------
    ContextSections
        Structured prompt sections ready to be rendered by
        :class:`~nexus.context.ContextBuilder`.
    """
    # --- project notes ---
    project_notes = [f"Project: {config.project_name}"]
    if config.project_description:
        project_notes.append(config.project_description)
    if config.knowledge_file.exists():
        project_notes.extend(_read_knowledge_summary(config.knowledge_file))

    # --- skills ---
    skills: list[str] = []
    if skill_registry is not None and skill_registry.all():
        active = set(active_skills or [])
        skills.append(skill_registry.summary(active=active))

    # --- carry-over notes ---
    carry_over_notes: list[str] = []
    if carry_over is not None:
        carry_over_notes.extend(carry_over.pinned_facts[-5:])
        carry_over_notes.extend(carry_over.summarized_history[-3:])
        carry_over_notes.extend(carry_over.active_constraints[-3:])

    cwd = (current_working_directory or Path.cwd()).resolve()
    _ui = user_instructions or config.user_instructions

    return ContextSections(
        base_instruction=build_base_instruction(
            config,
            tool_registry,
            user_instructions=_ui,
        ),
        environment=[
            f"Current UTC time: {_current_utc_time()}",
            f"Current date: {datetime.now(UTC).strftime('%A, %B %d, %Y')}",
            f"Current working directory: {cwd}",
            f"Workspace: {config.workspace_root}",
            f"Mode: {execution_mode or config.default_mode}",
            f"Provider adapter: {config.provider}",
            f"Model: {config.model_name}",
        ],
        tools=[],
        skills=skills,
        project_notes=project_notes,
        memory=list(memory_entries) if memory_entries else [],
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
