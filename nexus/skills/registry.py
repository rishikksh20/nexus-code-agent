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
        items = [
            f"- {skill.name}: {skill.description}" + (" (active)" if skill.name in active else "")
            for skill in self.all()
        ]
        return "Available skills:\n" + "\n".join(items)
