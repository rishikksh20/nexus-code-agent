from __future__ import annotations

from nexus.skills.models import Skill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda item: item.name)

    def summary(self, *, active: set[str] | None = None) -> str:
        if not self._skills:
            return ""
        active = active or set()
        lines = [
            "Available skills metadata:",
            "Use this catalogue to identify relevant skills. If full instructions are needed, read the SKILL.md path; the system prompt includes metadata only.",
        ]
        for skill in self.all():
            parts = [
                f"name={skill.name}",
                f"description={skill.description}",
                f"source={skill.source}",
                f"active={'yes' if skill.name in active else 'no'}",
            ]
            if skill.skill_path is not None:
                parts.append(f"path={skill.skill_path}")
            if skill.license:
                parts.append(f"license={skill.license}")
            if skill.compatibility:
                parts.append(f"compatibility={skill.compatibility}")
            if skill.metadata:
                metadata = ", ".join(
                    f"{key}={value}" for key, value in sorted(skill.metadata.items())
                )
                parts.append(f"metadata={metadata}")
            if skill.allowed_tools:
                parts.append(f"allowed-tools={' '.join(skill.allowed_tools)}")
            lines.append("- " + "; ".join(parts))
        return "\n".join(lines)
