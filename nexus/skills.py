from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

BUILTIN_SKILLS_DIR: Path = Path(__file__).parent / "builtin_skills"


@dataclass(slots=True, frozen=True)
class Skill:
    name: str
    description: str
    content: str
    tags: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    source: str = "local"


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return sorted(self._skills.values(), key=lambda item: item.name)

    def summary(self) -> str:
        if not self._skills:
            return ""
        items = [f"- {skill.name}: {skill.description}" for skill in self.all()]
        return "Available skills:\n" + "\n".join(items)


def load_skill_registry(*roots: Path) -> SkillRegistry:
    registry = SkillRegistry()
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            description = _skill_description(content, fallback=path.parent.name)
            registry.register(
                Skill(
                    name=path.parent.name,
                    description=description,
                    content=content,
                    source=str(path),
                )
            )
    return registry


def _skill_description(content: str, *, fallback: str) -> str:
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return f"Skill from {fallback}"