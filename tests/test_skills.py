from __future__ import annotations

from nexus.skills import BUILTIN_SKILLS_DIR, load_skill_registry


def test_builtin_skills_dir_exists():
    assert BUILTIN_SKILLS_DIR.exists()
    assert BUILTIN_SKILLS_DIR.is_dir()


def test_nexus_agent_skill_is_discoverable():
    skill_file = BUILTIN_SKILLS_DIR / "nexus-agent" / "SKILL.md"
    assert skill_file.exists(), "nexus-agent/SKILL.md must exist in builtin_skills"


def test_nexus_agent_skill_loads_into_registry():
    registry = load_skill_registry(BUILTIN_SKILLS_DIR)
    skill = registry.get("nexus-agent")
    assert skill is not None
    assert skill.name == "nexus-agent"
    assert len(skill.content) > 100


def test_nexus_agent_skill_has_meaningful_description():
    registry = load_skill_registry(BUILTIN_SKILLS_DIR)
    skill = registry.get("nexus-agent")
    assert skill is not None
    # Description is derived from first non-empty line (the heading)
    assert "Nexus" in skill.description


def test_load_skill_registry_with_no_roots_returns_empty():
    registry = load_skill_registry()
    assert registry.all() == []


def test_load_skill_registry_skips_nonexistent_root(tmp_path):
    registry = load_skill_registry(tmp_path / "does-not-exist", BUILTIN_SKILLS_DIR)
    assert registry.get("nexus-agent") is not None


def test_local_skill_overrides_builtin(tmp_path):
    """A skill with the same name in a later root should override the builtin."""
    skill_dir = tmp_path / "nexus-agent"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Override\nLocal version.", encoding="utf-8")

    registry = load_skill_registry(BUILTIN_SKILLS_DIR, tmp_path)
    skill = registry.get("nexus-agent")
    assert skill is not None
    assert "Local version." in skill.content
