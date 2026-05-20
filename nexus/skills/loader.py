from __future__ import annotations

from fnmatch import fnmatch
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING

from nexus.skills.models import Skill
from nexus.skills.parser import SkillParseError, parse_skill_file
from nexus.skills.registry import SkillRegistry

if TYPE_CHECKING:
    from nexus.config.defaults import AgentConfig


logger = logging.getLogger(__name__)
BUILTIN_SKILLS_DIR: Path = Path(__file__).resolve().parent.parent / "builtin_skills"


def get_skill_roots(config: "AgentConfig") -> tuple[Path, ...]:
    roots: list[Path] = [BUILTIN_SKILLS_DIR]
    roots.extend(_path_list(getattr(config, "skill_paths", []), base=config.workspace_root))
    roots.extend(
        [
            config.skills_dir,
            config.local_root / "skills",
            config.workspace_root / ".agents" / "skills",
        ]
    )
    return tuple(_unique_existing_order(roots))


def load_skill_registry(*roots: Path, config: "AgentConfig | None" = None) -> SkillRegistry:
    registry = SkillRegistry()
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            skill = _load_skill(path, root=root, config=config)
            if skill is not None:
                registry.register(skill)
    return registry


def resolve_active_skill_names(
    registry: SkillRegistry,
    config: "AgentConfig",
    *,
    extra: tuple[str, ...] | list[str] = (),
) -> list[str]:
    names = [skill.name for skill in registry.all()]
    enabled = list(getattr(config, "enabled_skills", []) or [])
    disabled = list(getattr(config, "disabled_skills", []) or [])

    active: set[str] = set()
    for pattern in enabled:
        active.update(name for name in names if name_matches(name, [pattern]))
    if disabled:
        active = {
            name
            for name in active
            if not any(name_matches(name, [pattern]) for pattern in disabled)
        }
    for name in extra:
        if registry.get(name) is not None:
            active.add(name)
    return sorted(active)


def name_matches(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    for raw in patterns:
        pattern = str(raw or "").strip()
        if not pattern:
            continue
        if pattern.startswith("re:"):
            try:
                if re.fullmatch(pattern.removeprefix("re:"), name, flags=re.IGNORECASE):
                    return True
            except re.error:
                continue
        elif fnmatch(lowered, pattern.lower()):
            return True
    return False


def _load_skill(path: Path, *, root: Path, config: "AgentConfig | None") -> Skill | None:
    try:
        metadata, body = parse_skill_file(path)
    except SkillParseError as exc:
        logger.warning("Skipping invalid skill at %s: %s", path, exc)
        return None
    return Skill(
        name=metadata.name,
        description=metadata.description,
        content=body,
        root_dir=root.resolve(),
        skill_path=path.resolve(),
        source=_source_label(root, config),
        license=metadata.license,
        compatibility=metadata.compatibility,
        metadata=metadata.metadata,
        allowed_tools=metadata.allowed_tools,
    )


def _source_label(root: Path, config: "AgentConfig | None") -> str:
    resolved = root.resolve()
    if resolved == BUILTIN_SKILLS_DIR.resolve():
        return "builtin"
    if config is not None and resolved == config.skills_dir.resolve():
        return "global"
    if config is not None and resolved == (config.local_root / "skills").resolve():
        return "local"
    if config is not None and resolved == (config.workspace_root / ".agents" / "skills").resolve():
        return "agent-standard"
    return "custom"


def _path_list(values: object, *, base: Path) -> list[Path]:
    if not isinstance(values, list):
        return []
    paths: list[Path] = []
    for value in values:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = base / path
        paths.append(path)
    return paths


def _unique_existing_order(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def skill_template(name: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: Describe what {name} helps with and when Nexus should use it.\n"
        "---\n\n"
        f"# {name}\n\n"
        "Add step-by-step instructions, examples, and edge cases here.\n"
    )
