"""System-prompt section builder for nexus.

``ContextSections`` is a typed container for the structured sections that
make up the agent's system prompt.  ``ContextBuilder`` renders them into a
single prompt string that is sent to the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ContextSections:
    """Structured container for the agent's system-prompt content."""

    base_instruction: str
    environment: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    project_notes: list[str] = field(default_factory=list)
    # Persistent memory entries loaded from the MemoryStore at session start.
    # Each entry is a pre-formatted string: "key: content" (possibly multi-line).
    # Present in every turn so the agent always knows what the user has stored.
    memory: list[str] = field(default_factory=list)
    carry_over: list[str] = field(default_factory=list)
    task_focus: list[str] = field(default_factory=list)


class ContextBuilder:
    """Convert a :class:`ContextSections` into a prompt string."""

    def build(self, sections: ContextSections) -> str:
        blocks = [sections.base_instruction]
        if sections.environment:
            blocks.append(
                "## Environment\n"
                + "\n".join(f"- {item}" for item in sections.environment)
            )
        if sections.tools:
            blocks.append(
                "## Available Tools\n"
                + "\n".join(f"- {item}" for item in sections.tools)
            )
        if sections.skills:
            blocks.append("## Skills\n\n" + "\n\n".join(sections.skills))
        if sections.memory:
            blocks.append(
                "## Persistent Memory\n"
                "The following entries were stored in memory across previous sessions "
                "and are always available to you:\n"
                + "\n".join(f"- {item}" for item in sections.memory)
            )
        if sections.project_notes:
            blocks.append(
                "## Project Notes\n"
                + "\n".join(f"- {item}" for item in sections.project_notes)
            )
        if sections.carry_over:
            blocks.append(
                "## Carry-Over Context\n"
                + "\n".join(f"- {item}" for item in sections.carry_over)
            )
        if sections.task_focus:
            blocks.append(
                "## Current Task\n"
                + "\n".join(f"- {item}" for item in sections.task_focus)
            )
        return "\n\n".join(blocks)
