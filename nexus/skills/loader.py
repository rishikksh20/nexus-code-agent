from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nexus.skills.models import Skill
from nexus.skills.registry import SkillRegistry

if TYPE_CHECKING:
    from nexus.config.defaults import AgentConfig


BUILTIN_SKILLS_DIR: Path = Path(__file__).resolve().parent.parent / "builtin_skills"


def get_skill_roots(config: "AgentConfig") -> tuple[Path, ...]:
    return (
        BUILTIN_SKILLS_DIR,
        config.skills_dir,
        config.local_root / "skills",
    )


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