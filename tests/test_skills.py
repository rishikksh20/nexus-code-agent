from __future__ import annotations

from rich.console import Console

from nexus.app import RuntimeResources
from nexus.cli.init import init_workspace
from nexus.config import load_config
from nexus.runtime.runtime_session import RuntimeSession
from nexus.skills import (
    BUILTIN_SKILLS_DIR,
    SkillParseError,
    get_skill_roots,
    load_skill_registry,
    parse_skill_markdown,
    resolve_active_skill_names,
)
from nexus.tools.base import ToolRegistry


def _write_skill(root, name: str, description: str = "A useful skill.", body: str = "Use it well.") -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "license: MIT\n"
        "metadata:\n"
        "  owner: tests\n"
        "allowed-tools: read_file grep\n"
        "---\n\n"
        f"# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def test_builtin_skills_dir_exists():
    assert BUILTIN_SKILLS_DIR.exists()
    assert BUILTIN_SKILLS_DIR.is_dir()


def test_parse_skill_markdown_requires_frontmatter():
    try:
        parse_skill_markdown("# Missing metadata")
    except SkillParseError as exc:
        assert "frontmatter" in str(exc)
    else:
        raise AssertionError("Expected SkillParseError")


def test_parse_skill_markdown_reads_yaml_frontmatter():
    frontmatter, body = parse_skill_markdown(
        "---\n"
        "name: code-review\n"
        "description: Review code changes.\n"
        "metadata:\n"
        "  version: \"1.0\"\n"
        "allowed-tools: read_file grep\n"
        "---\n\n"
        "Review carefully."
    )

    assert frontmatter["name"] == "code-review"
    assert frontmatter["metadata"]["version"] == "1.0"
    assert frontmatter["allowed-tools"] == "read_file grep"
    assert "Review carefully." in body


def test_nexus_agent_skill_loads_into_registry():
    registry = load_skill_registry(BUILTIN_SKILLS_DIR)
    skill = registry.get("nexus-agent")
    assert skill is not None
    assert skill.name == "nexus-agent"
    assert skill.source == "builtin"
    assert "Nexus" in skill.description
    assert skill.skill_path == (BUILTIN_SKILLS_DIR / "nexus-agent" / "SKILL.md").resolve()
    assert len(skill.content) > 100


def test_load_skill_registry_with_no_roots_returns_empty():
    registry = load_skill_registry()
    assert registry.all() == []


def test_load_skill_registry_skips_nonexistent_root(tmp_path):
    registry = load_skill_registry(tmp_path / "does-not-exist", BUILTIN_SKILLS_DIR)
    assert registry.get("nexus-agent") is not None


def test_later_skill_root_overrides_earlier_root(tmp_path):
    global_root = tmp_path / "global"
    local_root = tmp_path / "local"
    _write_skill(global_root, "review", body="Global version.")
    _write_skill(local_root, "review", body="Local version.")

    registry = load_skill_registry(global_root, local_root)
    skill = registry.get("review")

    assert skill is not None
    assert "Local version." in skill.content


def test_load_skill_registry_skips_invalid_skill(tmp_path):
    invalid_dir = tmp_path / "bad"
    invalid_dir.mkdir()
    (invalid_dir / "SKILL.md").write_text("# no frontmatter", encoding="utf-8")

    registry = load_skill_registry(tmp_path)

    assert registry.get("bad") is None


def test_load_skill_registry_rejects_name_directory_mismatch(tmp_path):
    skill_dir = tmp_path / "wrong-dir"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: right-name\ndescription: Mismatch.\n---\n",
        encoding="utf-8",
    )

    registry = load_skill_registry(tmp_path)

    assert registry.get("right-name") is None


def test_skill_metadata_fields_are_loaded(tmp_path):
    _write_skill(tmp_path, "code-review")

    skill = load_skill_registry(tmp_path).get("code-review")

    assert skill is not None
    assert skill.license == "MIT"
    assert skill.metadata == {"owner": "tests"}
    assert skill.allowed_tools == ("read_file", "grep")


def test_get_skill_roots_discovers_skill_paths_and_agents_standard(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    extra = tmp_path / "extra"
    config.skill_paths = [extra]
    _write_skill(extra, "extra-skill")
    agents_root = tmp_path / ".agents" / "skills"
    _write_skill(agents_root, "agents-skill")

    registry = load_skill_registry(*get_skill_roots(config), config=config)

    assert registry.get("extra-skill") is not None
    agents_skill = registry.get("agents-skill")
    assert agents_skill is not None
    assert agents_skill.source == "agent-standard"


def test_workspace_init_makes_builtin_skill_path_tool_readable(tmp_path):
    init_workspace(tmp_path, global_root=tmp_path / "global", project_name="workspace")
    config = load_config(tmp_path, global_root=tmp_path / "global")

    skill = load_skill_registry(*get_skill_roots(config), config=config).get("nexus-agent")

    assert skill is not None
    assert skill.source == "agent-standard"
    assert skill.skill_path == (tmp_path / ".agents" / "skills" / "nexus-agent" / "SKILL.md").resolve()


def test_resolve_active_skill_names_supports_glob_and_regex(tmp_path):
    _write_skill(tmp_path, "code-review")
    _write_skill(tmp_path, "code-search")
    _write_skill(tmp_path, "writing")
    registry = load_skill_registry(tmp_path)
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.enabled_skills = ["code-*", "re:writing"]
    config.disabled_skills = ["code-search"]

    active = resolve_active_skill_names(registry, config)

    assert active == ["code-review", "writing"]


def test_builtin_nexus_agent_is_discoverable_but_not_auto_active(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")

    runtime_session = RuntimeSession.create(
        config=config,
        console=Console(record=True, no_color=True),
        params={
            "no_session": True,
            "session": None,
            "resume_last": False,
            "no_skills": False,
            "skills": (),
            "deny_mutating": False,
        },
        tool_registry=ToolRegistry(),
        hooks=None,
        resources=RuntimeResources(),
    )

    assert runtime_session.state.skill_registry.get("nexus-agent") is not None
    assert runtime_session.state.active_skills == []


def test_cli_skill_is_run_only_active(tmp_path):
    skills_root = tmp_path / "global" / "skills"
    _write_skill(skills_root, "review")
    config = load_config(tmp_path, global_root=tmp_path / "global")

    runtime_session = RuntimeSession.create(
        config=config,
        console=Console(record=True, no_color=True),
        params={
            "no_session": True,
            "session": None,
            "resume_last": False,
            "no_skills": False,
            "skills": ("review",),
            "deny_mutating": False,
        },
        tool_registry=ToolRegistry(),
        hooks=None,
        resources=RuntimeResources(),
    )

    assert runtime_session.state.active_skills == ["review"]
    assert runtime_session.state.run_skills == ["review"]
    assert config.enabled_skills == []


def test_cli_skill_override_is_not_persisted_or_blocked_by_disabled_pattern(tmp_path):
    skills_root = tmp_path / "global" / "skills"
    _write_skill(skills_root, "review")
    (tmp_path / ".nexus" / "config.toml").parent.mkdir(parents=True)
    (tmp_path / ".nexus" / "config.toml").write_text(
        'disabled_skills = ["review"]\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path, global_root=tmp_path / "global")

    runtime_session = RuntimeSession.create(
        config=config,
        console=Console(record=True, no_color=True),
        params={
            "no_session": True,
            "session": None,
            "resume_last": False,
            "no_skills": False,
            "skills": ("review",),
            "deny_mutating": False,
        },
        tool_registry=ToolRegistry(),
        hooks=None,
        resources=RuntimeResources(),
    )

    assert runtime_session.state.active_skills == ["review"]
    assert config.disabled_skills == ["review"]
