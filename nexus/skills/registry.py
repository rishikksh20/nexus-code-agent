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
        lines = ["Available skills:"]
        for skill in self.all():
            lines.append(
                f"- {skill.name}: {skill.description}"
                + (" (active)" if skill.name in active else "")
            )
        active_skills = [skill for skill in self.all() if skill.name in active]
        if active_skills:
            lines.extend(["", "Active skill instructions:"])
            for skill in active_skills:
                lines.extend([f"## {skill.name}", skill.content.strip()])
        return "\n".join(lines)
