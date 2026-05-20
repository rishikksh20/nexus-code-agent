"""Skill discovery and registry APIs for Nexus."""

from nexus.skills.loader import (
    BUILTIN_SKILLS_DIR,
    get_skill_roots,
    load_skill_registry,
    name_matches,
    resolve_active_skill_names,
    skill_template,
)
from nexus.skills.models import Skill
from nexus.skills.parser import SkillMetadata, SkillParseError, parse_skill_markdown
from nexus.skills.registry import SkillRegistry

__all__ = [
    "BUILTIN_SKILLS_DIR",
    "Skill",
    "SkillMetadata",
    "SkillParseError",
    "SkillRegistry",
    "get_skill_roots",
    "load_skill_registry",
    "name_matches",
    "parse_skill_markdown",
    "resolve_active_skill_names",
    "skill_template",
]
