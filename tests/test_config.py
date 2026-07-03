from __future__ import annotations

import os
import tomllib

import pytest

from nexus.cli.init import init_workspace
from nexus.config import load_config
from nexus.config.loader import ConfigError
from nexus.config.editor import update_dotenv_value, update_model_profile_fields
from nexus.config.model_catalog import get_model_context_limit
from nexus.config.upgrade import inspect_config_upgrade, upgrade_config_file
from nexus.skills import BUILTIN_SKILLS_DIR


def test_model_profiles_deep_merge_global_and_local_catalogs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    global_root.mkdir()
    (global_root / "config.toml").write_text(
        '[models.fast]\n'
        'provider = "fake"\n'
        'model_name = "fake-global"\n'
        'context_length = 32000\n'
        'max_output_tokens = 4000\n'
        'reserved_output_tokens = 5000\n'
        '[models.fast.thinking]\n'
        'enabled = false\n'
        'mode = "provider_default"\n',
        encoding="utf-8",
    )
    local = workspace / ".nexus" / "config.toml"
    local.parent.mkdir()
    local.write_text(
        'active_model_profile = "fast"\n'
        '[models.fast]\n'
        'model_name = "fake-local"\n'
        'temperature = 0.25\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.active_model_profile == "fast"
    assert config.provider == "fake"
    assert config.model_name == "fake-local"
    assert config.context_length == 32000
    assert config.max_output_tokens == 4000
    assert config.reserved_output_tokens == 5000
    assert config.temperature == 0.25


def test_config_synthesizes_legacy_current_profile_without_selection(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")

    assert config.active_model_profile == "legacy-current"
    assert config.active_model_profile_legacy is True
    assert config.context_length == get_model_context_limit(config.model_name)
    assert config.models["legacy-current"]["model_name"] == config.model_name
    assert config.models["legacy-current"]["context_length"] == get_model_context_limit(config.model_name)


@pytest.mark.parametrize(
    ("profile_body", "message"),
    [
        ("context_length = 5000\nmax_output_tokens = 5000\nreserved_output_tokens = 5000\n", "token budgets"),
        ("context_length = 32000\nmax_output_tokens = 4000\nreserved_output_tokens = 4000\nsupports_tools = false\n", "supports_tools"),
        ("context_length = 32000\nmax_output_tokens = 4000\nreserved_output_tokens = 4000\nsupports_reasoning = false\nthinking = { enabled = true, mode = \"provider_default\" }\n", "supports_reasoning"),
    ],
)
def test_config_rejects_invalid_active_model_profiles(tmp_path, profile_body, message):
    config_file = tmp_path / ".nexus" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text(
        'active_model_profile = "bad"\n'
        '[models.bad]\n'
        'provider = "fake"\n'
        'model_name = "fake"\n'
        f"{profile_body}",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path, global_root=tmp_path / "global")


def test_profile_editor_preserves_comments_and_nested_thinking(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "# keep me\n"
        "[models.fast]\n"
        'provider = "fake"\n'
        '[models.fast.thinking]\n'
        'enabled = false\n',
        encoding="utf-8",
    )

    update_model_profile_fields(config_file, "fast", {"thinking": {"mode": "provider_default"}})

    content = config_file.read_text(encoding="utf-8")
    assert "# keep me" in content
    parsed = tomllib.loads(content)
    assert parsed["models"]["fast"]["thinking"] == {"enabled": False, "mode": "provider_default"}


def test_dotenv_editor_updates_existing_key_and_preserves_other_lines(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# credentials\n"
        "OPENAI_API_KEY=old\n"
        "OTHER=value\n"
        "OPENAI_API_KEY=duplicate\n",
        encoding="utf-8",
    )

    update_dotenv_value(dotenv, "OPENAI_API_KEY", "new-key")

    assert dotenv.read_text(encoding="utf-8") == "# credentials\nOPENAI_API_KEY=new-key\nOTHER=value\n"


def test_builtin_model_catalog_includes_big_pickle_openai_compatible():
    from nexus.config.model_catalog import builtin_model, builtin_models_for_provider
    from nexus.config.model_catalog import get_model_context_limit

    profile = builtin_model("openai-compatible", "big-pickle")

    assert profile is not None
    assert profile.openai_compatible is True
    assert profile.context_length == 200_000
    assert profile.max_output_tokens == 32_000
    assert profile.reserved_output_tokens == 32_000
    assert profile.supports_reasoning is True
    assert profile.thinking_mode == "budget_tokens"
    assert profile.thinking_budget_tokens == 4096
    assert profile.base_url_env == "BASE_URL"
    assert profile.api_key_env == "API_KEY"
    assert profile in builtin_models_for_provider("openai-compatible")
    assert get_model_context_limit("big-pickle") == 200_000


def test_global_config_upgrade_adds_non_destructive_legacy_profile(tmp_path):
    from nexus.cli.init import _global_config_toml

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'provider = "fake"\n'
        'model_name = "old-model"\n'
        'max_output_tokens = 2048\n',
        encoding="utf-8",
    )

    upgrade_config_file(config_file, _global_config_toml())

    parsed = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert parsed["provider"] == "fake"
    assert parsed["model_name"] == "old-model"
    assert parsed["models"]["legacy-current"]["provider"] == "fake"
    assert parsed["models"]["legacy-current"]["model_name"] == "old-model"
    assert parsed["models"]["legacy-current"]["max_output_tokens"] == 2048
    assert "active_model_profile" not in parsed


def test_config_rejects_activation_of_profile_with_disabled_provider(tmp_path):
    config_file = tmp_path / ".nexus" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text(
        'active_model_profile = "offline"\n'
        '[providers.fake]\n'
        'enabled = false\n'
        '[models.offline]\n'
        'provider = "fake"\n'
        'model_name = "fake"\n'
        'context_length = 32000\n'
        'max_output_tokens = 4000\n'
        'reserved_output_tokens = 4000\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="disabled provider"):
        load_config(tmp_path, global_root=tmp_path / "global")


def test_config_merges_local_overrides(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (global_root / "config.toml").write_text('model_name = "global-model"\n', encoding="utf-8")
    (workspace / ".nexus" / "config.toml").write_text('model_name = "local-model"\nstream_output = false\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_DEFAULT_MODE", "auto")
    monkeypatch.setenv("BASE_URL", "https://api.example.com/v1")

    config = load_config(workspace, global_root=global_root)

    assert config.model_name == "local-model"
    assert config.default_mode == "auto"
    assert config.stream_output is False


def test_init_creates_knowledge_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"

    created = init_workspace(workspace, global_root=global_root, project_name="workspace")

    assert any(path.name == "knowledge.md" for path in created)
    assert (workspace / ".nexus" / "knowledge.md").exists()


def test_init_is_workspace_level_and_does_not_create_global_config(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"

    created = init_workspace(workspace, global_root=global_root, project_name="workspace")

    assert (workspace / ".nexus" / "config.toml").exists()
    assert not (global_root / "config.toml").exists()
    assert global_root / "config.toml" not in created


def test_init_copies_builtin_skills_without_overwriting_workspace_edits(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"

    init_workspace(workspace, global_root=global_root, project_name="workspace")

    workspace_skills = workspace / ".agents" / "skills"
    builtin_names = {
        path.name
        for path in BUILTIN_SKILLS_DIR.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    assert {path.name for path in workspace_skills.iterdir()} == builtin_names
    local_skill = workspace_skills / "nexus-agent" / "SKILL.md"
    local_skill.write_text("workspace edit\n", encoding="utf-8")

    init_workspace(workspace, global_root=global_root, project_name="workspace")

    assert local_skill.read_text(encoding="utf-8") == "workspace edit\n"

    init_workspace(workspace, global_root=global_root, project_name="workspace", force=True)

    assert local_skill.read_text(encoding="utf-8") == (
        BUILTIN_SKILLS_DIR / "nexus-agent" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_config_rejects_invalid_mode(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('default_mode = "unsafe"\n', encoding="utf-8")

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "Invalid default_mode" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for invalid default_mode")


def test_config_accepts_advanced_agent_defaults(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")

    config = load_config(workspace, global_root=global_root)

    assert config.agent_mode == "basic"
    assert config.delegation_subagents == []
    assert config.config_version == 5
    assert config.textual_transcript_max_lines == 5000
    assert config.prompt_history_max_entries == 200
    assert config.tool_output_max_chars == 102400
    assert config.shell_inherit_environment is False
    assert config.parallel_tools is True
    assert config.parallel_tool_window == 4
    assert config.agent_allowed_tools == []
    assert config.agent_allowed_skills == []
    assert config.agent_allowed_mcp_servers == []
    assert config.agent_add_tools == []
    assert config.agent_remove_tools == []
    assert config.agent_add_skills == []
    assert config.agent_remove_skills == []
    assert config.agent_add_mcp_servers == []
    assert config.agent_remove_mcp_servers == []
    assert not hasattr(config, "agent_attached_tools")
    assert not hasattr(config, "agent_detached_tools")
    assert config.subagent_profiles == []
    content = config.local_config_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert "[agents]" not in lines
    assert "[[sub-agents]]" not in lines


def test_config_rejects_parallel_tool_window_above_eight(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text("parallel_tool_window = 9\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="parallel_tool_window"):
        load_config(workspace, global_root=global_root)


def test_config_accepts_agent_scope_fields(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'agent_allowed_tools = ["subagent_execution"]\n'
        'agent_attached_tools = ["read_file"]\n'
        'agent_detached_tools = ["bash"]\n'
        'agent_allowed_skills = ["nexus-agent"]\n'
        'agent_attached_skills = ["review"]\n'
        'agent_detached_skills = ["notes"]\n'
        'agent_allowed_mcp_servers = ["search"]\n'
        'agent_attached_mcp_servers = ["filesystem"]\n'
        'agent_detached_mcp_servers = ["git"]\n'
        'subagent_profiles = [{ name = "execution", allowed_tools = ["grep"], attached_tools = ["read_file"], allowed_skills = ["review"], allowed_mcp_servers = ["filesystem"], detached_mcp_servers = ["git"] }]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.agent_allowed_tools == ["subagent_coding"]
    assert config.agent_allowed_skills == ["nexus-agent"]
    assert config.agent_allowed_mcp_servers == ["search"]
    assert not hasattr(config, "agent_attached_tools")
    assert config.subagent_profiles[0]["name"] == "execution"
    assert config.subagent_profiles[0]["allowed_tools"] == ["grep"]
    assert "attached_tools" not in config.subagent_profiles[0]
    assert "detached_mcp_servers" not in config.subagent_profiles[0]


def test_config_accepts_agents_and_sub_agents_sections(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        "[agents]\n"
        'allowed_tools = ["subagent_execution"]\n'
        'attached_tools = ["read_file"]\n'
        'detached_tools = ["bash"]\n'
        'allowed_skills = ["nexus-agent"]\n'
        'attached_skills = ["review"]\n'
        'detached_skills = ["notes"]\n'
        'allowed_mcp_servers = ["search"]\n'
        'attached_mcp_servers = ["filesystem"]\n'
        'detached_mcp_servers = ["git"]\n'
        "\n"
        "[[sub-agents]]\n"
        'name = "execution"\n'
        'allowed_tools = ["grep"]\n'
        'attached_tools = ["read_file"]\n'
        'allowed_skills = ["review"]\n'
        'allowed_mcps = ["filesystem"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.agent_allowed_tools == ["subagent_coding"]
    assert config.agent_allowed_skills == ["nexus-agent"]
    assert config.agent_allowed_mcp_servers == ["search"]
    assert config.subagent_profiles[0]["name"] == "execution"
    assert config.subagent_profiles[0]["allowed_tools"] == ["grep"]
    assert config.subagent_profiles[0]["allowed_mcp_servers"] == ["filesystem"]
    assert "attached_tools" not in config.subagent_profiles[0]


def test_config_accepts_all_scope_sentinel(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        "[agents]\n"
        'allowed_tools = "all"\n'
        'allowed_skills = "all"\n'
        'allowed_mcps = "all"\n'
        "\n"
        "[[sub-agents]]\n"
        'name = "execution"\n'
        'allowed_tools = "all"\n'
        'allowed_skills = "all"\n'
        'allowed_mcps = "all"\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.agent_allowed_tools == ["all"]
    assert config.agent_allowed_skills == ["all"]
    assert config.agent_allowed_mcp_servers == ["all"]
    assert config.subagent_profiles[0]["allowed_tools"] == ["all"]
    assert config.subagent_profiles[0]["allowed_skills"] == ["all"]
    assert config.subagent_profiles[0]["allowed_mcp_servers"] == ["all"]


def test_config_accepts_sub_agents_named_tables(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        "[sub-agents.execution]\n"
        'allowed_tools = ["read_file"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.subagent_profiles == [{"allowed_tools": ["read_file"], "name": "execution"}]


def test_config_upgrade_migrates_legacy_agent_scope_to_new_tables(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    local_config.write_text(
        'project_name = "workspace"\n'
        'agent_allowed_tools = ["subagent_execution"]\n'
        'agent_attached_tools = ["read_file"]\n'
        'agent_detached_mcp_servers = ["git"]\n'
        'subagent_profiles = [{ name = "execution", allowed_tools = ["grep"], allowed_mcp_servers = ["filesystem"], attached_tools = ["read_file"] }]\n',
        encoding="utf-8",
    )

    report = upgrade_config_file(
        local_config,
        __import__("nexus.cli.init", fromlist=["_local_config_toml"])._local_config_toml(
            workspace_root=workspace,
            project_name="workspace",
            project_description="",
        ),
    )
    content = local_config.read_text(encoding="utf-8")
    config = load_config(workspace, global_root=global_root)

    assert report.agent_scope_migrated is True
    assert report.subagent_scope_migrated is True
    assert "[agents]" in content
    assert 'remove_tools = ["bash", "read_file", "ask_user"]' in content
    assert 'attached_tools = ["read_file"]' not in content
    assert 'detached_mcp_servers = ["git"]' not in content
    assert "[[sub-agents]]" in content
    assert 'name = "coding"' in content
    assert 'add_mcps = ["filesystem"]' in content
    assert "agent_allowed_tools" not in content
    assert "subagent_profiles" not in content
    assert config.agent_allowed_tools == []
    assert config.agent_remove_tools == ["bash", "read_file", "ask_user"]
    assert config.subagent_profiles[0]["name"] == "coding"
    assert config.subagent_profiles[0]["remove_tools"] == [
        "read_file",
        "write_file",
        "edit",
        "insert_edit_into_file",
        "apply_patch",
        "glob",
        "list_dir",
        "lsp",
        "git_status",
        "git_diff",
        "run_python_check",
        "run_formatter",
    ]
    assert config.subagent_profiles[0]["add_mcp_servers"] == ["filesystem"]
    assert "attached_tools" not in config.subagent_profiles[0]


def test_config_upgrade_merges_legacy_agent_scope_into_existing_agents_table(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    local_config.write_text(
        'project_name = "workspace"\n'
        'agent_attached_tools = ["read_file"]\n'
        "[agents]\n"
        'allowed_tools = ["subagent_execution"]\n',
        encoding="utf-8",
    )

    report = upgrade_config_file(
        local_config,
        __import__("nexus.cli.init", fromlist=["_local_config_toml"])._local_config_toml(
            workspace_root=workspace,
            project_name="workspace",
            project_description="",
        ),
    )
    content = local_config.read_text(encoding="utf-8")
    config = load_config(workspace, global_root=global_root)

    assert report.agent_scope_migrated is True
    assert content.count("[agents]") == 1
    assert "agent_attached_tools" not in content
    assert config.agent_allowed_tools == []
    assert config.agent_remove_tools == ["bash", "read_file", "ask_user"]
    assert not hasattr(config, "agent_attached_tools")


def test_config_upgrade_normalizes_existing_sub_agents_and_delegation_names(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    local_config.write_text(
        'project_name = "workspace"\n'
        'delegation_subagents = [{ name = "verification", description = "Verify.", goal_prompt = "Run checks.", allowed_tools = ["subagent_review"] }]\n'
        "\n"
        "[agents]\n"
        'allowed_tools = ["subagent_execution"]\n'
        "\n"
        "[[sub-agents]]\n"
        'name = "execution"\n'
        'allowed_tools = ["grep"]\n'
        'allowed_mcp_servers = ["filesystem"]\n',
        encoding="utf-8",
    )

    report = upgrade_config_file(
        local_config,
        __import__("nexus.cli.init", fromlist=["_local_config_toml"])._local_config_toml(
            workspace_root=workspace,
            project_name="workspace",
            project_description="",
        ),
    )
    content = local_config.read_text(encoding="utf-8")
    config = load_config(workspace, global_root=global_root)

    assert report.legacy_subagent_names_migrated is True
    assert 'remove_tools = ["bash", "read_file", "ask_user"]' in content
    assert 'name = "coding"' in content
    assert 'add_mcps = ["filesystem"]' in content
    assert 'name = "impact_analyzer"' in content
    assert 'subagent_code_reviewer' in content
    assert config.agent_allowed_tools == []
    assert config.agent_remove_tools == ["bash", "read_file", "ask_user"]
    assert config.subagent_profiles[0]["name"] == "coding"
    assert config.delegation_subagents[0]["name"] == "impact_analyzer"
    assert config.delegation_subagents[0]["allowed_tools"] == ["subagent_code_reviewer"]


def test_config_upgrade_rehomes_config_version_before_existing_tables(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    template = __import__("nexus.cli.init", fromlist=["_local_config_toml"])._local_config_toml(
        workspace_root=workspace,
        project_name="workspace",
        project_description="",
    )
    lines = template.splitlines()
    version_line = next(line for line in lines if line.startswith("config_version = "))
    lines = [line for line in lines if line != version_line]
    broken_content = "\n".join(
        [
            *lines,
            "[agents]",
            "add_tools = []",
            "# Added by Nexus config upgrade",
            version_line,
        ]
    )
    local_config.write_text(broken_content + "\n", encoding="utf-8")

    before = inspect_config_upgrade(local_config, template)

    assert before.missing_keys == ("config_version",)
    assert before.subagent_scope_migrated is False

    upgrade_config_file(local_config, template)

    content = local_config.read_text(encoding="utf-8")
    parsed = tomllib.loads(content)
    after = inspect_config_upgrade(local_config, template)

    assert content.count("# Added by Nexus config upgrade") == 1
    assert content.splitlines().count("[[sub-agents]]") == 0
    assert parsed["config_version"] == 5
    assert "agents" not in parsed or "config_version" not in parsed["agents"]
    assert after.needs_upgrade is False


def test_config_upgrade_repairs_exact_duplicate_sub_agent_tables(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    local_config.write_text(
        'project_name = "workspace"\n'
        "config_version = 3\n"
        "\n"
        "[[sub-agents]]\n"
        'name = "planning_analysis"\n'
        'allowed_tools = ["read_file", "grep"]\n'
        "\n"
        "[[sub-agents]]\n"
        'name = "planning_analysis"\n'
        'allowed_tools = ["read_file", "grep"]\n',
        encoding="utf-8",
    )
    template = __import__("nexus.cli.init", fromlist=["_local_config_toml"])._local_config_toml(
        workspace_root=workspace,
        project_name="workspace",
        project_description="",
    )

    before = inspect_config_upgrade(local_config, template)

    assert before.subagent_scope_migrated is True

    upgrade_config_file(local_config, template)

    content = local_config.read_text(encoding="utf-8")
    config = load_config(workspace, global_root=global_root)

    assert content.splitlines().count("[[sub-agents]]") == 1
    assert config.subagent_profiles == [
        {"name": "explorer", "remove_tools": ["glob", "list_dir", "lsp", "git_diff", "git_status"]}
    ]


def test_config_non_strict_uses_defaults_and_warning_when_toml_is_corrupt(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text("project_name = [\n", encoding="utf-8")

    config = load_config(workspace, global_root=global_root, strict=False)

    assert config.project_name == "workspace"
    assert config.config_warnings
    assert "using defaults" in config.config_warnings[0]


def test_config_non_strict_still_applies_dotenv_when_toml_is_corrupt(tmp_path, monkeypatch):
    monkeypatch.delenv("PROVIDER", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.delenv("BASE_URL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text("project_name = [\n", encoding="utf-8")
    (workspace / ".env").write_text(
        "PROVIDER=openai-compatible\n"
        "MODEL=dotenv-model\n"
        "BASE_URL=https://example.test/v1\n"
        "API_KEY=dotenv-key\n",
        encoding="utf-8",
    )

    try:
        config = load_config(workspace, global_root=global_root, strict=False)

        assert config.provider == "openai-compatible"
        assert config.model_name == "dotenv-model"
        assert config.api_base_url == "https://example.test/v1"
        assert config.api_key == "dotenv-key"
        assert config.config_warnings
    finally:
        for key in ("PROVIDER", "MODEL", "BASE_URL", "API_KEY"):
            os.environ.pop(key, None)


def test_config_upgrade_rewrites_corrupt_toml_instead_of_appending_template(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    local_config = workspace / ".nexus" / "config.toml"
    local_config.write_text(
        'project_name = "workspace"\n'
        "[agents]\n"
        "project_name = \"nested-duplicate\"\n"
        "[agents]\n"
        "allowed_tools = []\n",
        encoding="utf-8",
    )

    report = upgrade_config_file(
        local_config,
        __import__("nexus.cli.init", fromlist=["_local_config_toml"])._local_config_toml(
            workspace_root=workspace,
            project_name="workspace",
            project_description="",
        ),
    )
    config = load_config(workspace, global_root=global_root)
    content = local_config.read_text(encoding="utf-8")

    assert report.needs_upgrade is True
    assert config.project_name == "workspace"
    assert content.count("project_name") == 1
    assert content.count("[agents]") == 1


def test_config_ignores_obsolete_subagent_attach_detach_fields(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'subagent_profiles = [{ name = "execution", attached_tools = "read_file" }]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.subagent_profiles == [{"name": "execution"}]


def test_config_accepts_legacy_multi_agent_mode_without_reintroducing_old_fields(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('multi_agent_mode = "always"\n', encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    assert config.agent_mode == "advanced"
    assert not hasattr(config, "multi_agent_mode")


def test_config_normalizes_legacy_subagent_tool_names(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'agent_mode = "advanced"\n'
        'allowed_tools = ["subagent_research", "subagent_review", "subagent_test"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert "subagent_research" not in config.allowed_tools
    assert "subagent_test" not in config.allowed_tools
    assert "subagent_explorer" in config.allowed_tools
    assert "subagent_coding" in config.allowed_tools
    assert "subagent_code_reviewer" in config.allowed_tools
    assert "subagent_impact_analyzer" in config.allowed_tools
    assert "run_tests" in config.allowed_tools
    assert "run_python_check" in config.allowed_tools
    assert "run_formatter" in config.allowed_tools
    assert "git_status" in config.allowed_tools
    assert "bash" in config.allowed_tools


def test_config_agent_mode_advanced_activates_multi_agent_profile(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('agent_mode = "advanced"\n', encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    assert config.agent_mode == "advanced"
    assert config.delegation_subagents == []


def test_config_agent_mode_advanced_adds_cognitive_tools_to_legacy_allowlist(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'agent_mode = "advanced"\nallowed_tools = ["get_time", "read_file"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert "get_time" in config.allowed_tools
    assert "read_file" in config.allowed_tools
    assert "subagent_explorer" in config.allowed_tools
    assert "subagent_coding" in config.allowed_tools
    assert "subagent_code_reviewer" in config.allowed_tools
    assert "subagent_impact_analyzer" in config.allowed_tools


def test_config_agent_mode_basic_keeps_single_agent_profile(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('agent_mode = "basic"\n', encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    assert config.agent_mode == "basic"
    assert config.delegation_subagents == []


def test_config_rejects_invalid_agent_mode(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('agent_mode = "busy"\n', encoding="utf-8")

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "Invalid agent_mode" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for invalid agent_mode")


def test_config_rejects_invalid_provider(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('provider = "bad-provider"\n', encoding="utf-8")

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "Invalid provider" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for invalid provider")


def test_config_accepts_llm_thinking_controls(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'llm_thinking_mode = "enabled"\nllm_reasoning_effort = "max"\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.llm_thinking_mode == "enabled"
    assert config.llm_reasoning_effort == "max"


def test_config_rejects_invalid_llm_thinking_mode(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('llm_thinking_mode = "maybe"\n', encoding="utf-8")

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "Invalid llm_thinking_mode" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for invalid llm_thinking_mode")


def test_config_requires_api_base_url_for_live_compatible_provider(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('provider = "openai-compatible"\napi_base_url = ""\n', encoding="utf-8")

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "requires api_base_url" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for missing api_base_url")


def test_config_accepts_mistral_provider_with_default_base_url(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")

    config = load_config(
        workspace,
        global_root=global_root,
        cli_overrides={"provider": "mistral"},
    )

    assert config.provider == "mistral"
    assert config.api_base_url == "https://api.mistral.ai/v1"


def test_config_accepts_cohere_provider_with_default_base_url(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")

    config = load_config(
        workspace,
        global_root=global_root,
        cli_overrides={"provider": "cohere", "api_base_url": ""},
    )

    assert config.provider == "cohere"
    assert config.api_base_url == "https://api.cohere.com"


def test_config_cohere_overrides_builtin_mistral_base_url(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")

    config = load_config(
        workspace,
        global_root=global_root,
        cli_overrides={"provider": "cohere"},
    )

    assert config.provider == "cohere"
    assert config.api_base_url == "https://api.cohere.com"


def test_config_accepts_native_sdk_providers_without_base_url(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")

    anthropic = load_config(
        workspace,
        global_root=global_root,
        cli_overrides={"provider": "anthropic", "api_base_url": ""},
    )
    gemini = load_config(
        workspace,
        global_root=global_root,
        cli_overrides={"provider": "gemini", "api_base_url": ""},
    )

    assert anthropic.api_base_url == ""
    assert gemini.api_base_url == ""


def test_gemini_model_context_limits_are_current():
    assert get_model_context_limit("gemini-2.5-flash") == 1_048_576
    assert get_model_context_limit("gemini-2.5-pro") == 1_048_576


def test_config_uses_mistral_base_url_env_override(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    monkeypatch.setenv("MISTRAL_BASE_URL", "https://mistral.example/v1")

    config = load_config(
        workspace,
        global_root=global_root,
        cli_overrides={"provider": "mistral"},
    )

    assert config.api_base_url == "https://mistral.example/v1"


def test_config_rejects_invalid_compaction_bounds(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'compaction_soft_limit = 100\ncompaction_hard_limit = 50\n',
        encoding="utf-8",
    )

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "compaction_hard_limit" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for invalid compaction limits")


def test_config_activates_enabled_local_mcp_servers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'mcp_servers = [{ name = "filesystem", command = ["uvx", "mcp-server-filesystem", "."], prefix = "fs_" }]\n'
        'enabled_mcp_servers = ["filesystem"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.mcp_servers == [
        {
            "name": "filesystem",
            "command": ["uvx", "mcp-server-filesystem", "."],
            "prefix": "fs_",
        }
    ]


def test_config_keeps_local_mcp_servers_inactive_until_enabled(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'mcp_servers = [{ name = "filesystem", command = ["uvx", "mcp-server-filesystem", "."], prefix = "fs_" }]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.mcp_servers == []


def test_config_accepts_extended_mcp_server_fields(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        "mcp_servers = [{ "
        'name = "filesystem", transport = "stdio", command = ["uvx", "mcp-server-filesystem", "."], '
        'prefix = "fs_", env = { TOKEN = "abc" }, cwd = ".", startup_timeout_seconds = 2.5, '
        'tool_timeout_seconds = 10, disabled = false, disabled_tools = ["write_file"], '
        'read_only_tools = ["read_file"], mutating_tools = ["write_file"]'
        " }]\n"
        'enabled_mcp_servers = ["filesystem"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.mcp_servers[0]["env"] == {"TOKEN": "abc"}
    assert config.mcp_servers[0]["disabled_tools"] == ["write_file"]
    assert config.mcp_servers[0]["read_only_tools"] == ["read_file"]
    assert config.mcp_servers[0]["mutating_tools"] == ["write_file"]
    assert config.mcp_servers[0]["startup_timeout_seconds"] == 2.5


def test_config_rejects_unsupported_mcp_transport(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'mcp_servers = [{ name = "remote", transport = "streamable_http", url = "http://localhost:3333/mcp", disabled = true }]\n'
        'enabled_mcp_servers = ["remote"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="remote.*streamable_http.*Only stdio is supported"):
        load_config(workspace, global_root=global_root)


def test_config_activates_global_mcp_servers_by_workspace_name(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (global_root / "config.toml").write_text(
        'mcp_servers = [{ name = "filesystem", command = ["mcp-server-filesystem", "."], prefix = "fs_" }]\n',
        encoding="utf-8",
    )
    (workspace / ".nexus" / "config.toml").write_text(
        'enabled_mcp_servers = ["filesystem"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.mcp_servers == [
        {"name": "filesystem", "command": ["mcp-server-filesystem", "."], "prefix": "fs_"}
    ]


def test_config_deactivates_local_mcp_server_by_name(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'mcp_servers = [{ name = "filesystem", command = ["mcp-server-filesystem", "."], prefix = "fs_" }]\n'
        'disabled_mcp_servers = ["filesystem"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.mcp_servers == []


def test_config_rejects_duplicate_mcp_servers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'mcp_servers = [{ name = "dup", command = ["cmd"] }, { name = "dup", command = ["cmd"] }]\n',
        encoding="utf-8",
    )

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "Duplicate mcp_servers entry" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for duplicate MCP server")


def test_config_accepts_structured_delegation_subagents(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'delegation_subagents = [{ name = "explore", description = "Investigate a focused codebase question.", goal_prompt = "Read the relevant code and summarize the answer.", allowed_tools = ["read_file", "glob"], max_turns = 12, timeout_seconds = 300 }]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.delegation_subagents == [
        {
            "name": "explore",
            "description": "Investigate a focused codebase question.",
            "goal_prompt": "Read the relevant code and summarize the answer.",
            "allowed_tools": ["read_file", "glob"],
            "max_turns": 12,
            "timeout_seconds": 300,
        }
    ]


def test_config_rejects_duplicate_delegation_subagents(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'delegation_subagents = [{ name = "explore", description = "First", goal_prompt = "One" }, { name = "explore", description = "Second", goal_prompt = "Two" }]\n',
        encoding="utf-8",
    )

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "Duplicate delegation_subagents entry 'explore'" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for duplicate delegation_subagents")


def test_config_ignores_removed_worker_runtime_keys_after_normalization(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('multi_agent_mode = "off"\n', encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    assert config.agent_mode == "basic"


def test_config_rejects_overlapping_tool_filters(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'allowed_tools = ["get_time", "write_file"]\ndenied_tools = ["write_file"]\n',
        encoding="utf-8",
    )

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "must not overlap" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for overlapping tool filters")


def test_config_accepts_skill_activation_and_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'skill_paths = ["extra-skills"]\n'
        'enabled_skills = ["code-*", "re:docs"]\n'
        'disabled_skills = ["code-old"]\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.skill_paths == [(workspace / "extra-skills").resolve()]
    assert config.enabled_skills == ["code-*", "re:docs"]
    assert config.disabled_skills == ["code-old"]


def test_config_dotenv_injects_api_key(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".env").write_text("MISTRAL_API_KEY=from-dotenv\n", encoding="utf-8")

    from nexus.integrations.openai_compatible import resolve_provider_api_key
    import os

    # load_config injects .env into os.environ; resolve_provider_api_key then finds the key
    load_config(workspace, global_root=global_root)
    assert resolve_provider_api_key("mistral") == "from-dotenv"
    # Clean up the injected key so it does not bleed into other tests
    os.environ.pop("MISTRAL_API_KEY", None)


def test_config_dotenv_api_key_set_via_agent_api_key_field(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    # api_key config field can be set directly in .nexus/config.toml
    (workspace / ".nexus" / "config.toml").write_text('api_key = "from-config"\n', encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    assert config.api_key == "from-config"


def test_config_dotenv_overrides_env_for_its_keys(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    # System env sets key first; .env should overwrite it
    monkeypatch.setenv("MISTRAL_API_KEY", "system-value")
    (workspace / ".env").write_text("MISTRAL_API_KEY=dotenv-value\n", encoding="utf-8")

    from nexus.integrations.openai_compatible import resolve_provider_api_key
    import os

    load_config(workspace, global_root=global_root)
    assert resolve_provider_api_key("mistral") == "dotenv-value"
    os.environ.pop("MISTRAL_API_KEY", None)


def test_config_default_provider_is_openai_compatible(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")

    config = load_config(workspace, global_root=global_root)

    assert config.provider == "openai-compatible"
    assert config.model_name == "mistral-medium-latest"
    assert config.api_base_url == "https://api.mistral.ai/v1"


def test_config_can_enable_hidden_path_reads(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'allow_hidden_paths = true\n',
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.allow_hidden_paths is True


def test_config_dotenv_provides_model_name(tmp_path):
    """Test that model_name can be set via .env using the MODEL key."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".env").write_text("MODEL=custom-model-name\n", encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    assert config.model_name == "custom-model-name"


def test_config_dotenv_provides_provider(tmp_path):
    """Test that provider can be set via .env using the PROVIDER key."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".env").write_text("PROVIDER=mistral\n", encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    assert config.provider == "mistral"


def test_config_dotenv_overrides_config_toml_model_name(tmp_path):
    """Test that .env MODEL overrides config.toml model_name."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('model_name = "toml-model"\n', encoding="utf-8")
    (workspace / ".env").write_text("MODEL=env-model\n", encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    # .env (via os.environ) should override config.toml
    assert config.model_name == "env-model"


def test_config_dotenv_overrides_config_toml_provider(tmp_path):
    """Test that .env PROVIDER overrides config.toml provider."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text('provider = "openai-compatible"\n', encoding="utf-8")
    (workspace / ".env").write_text("PROVIDER=mistral\n", encoding="utf-8")

    config = load_config(workspace, global_root=global_root)

    # .env (via os.environ) should override config.toml
    assert config.provider == "mistral"


def test_config_dotenv_provides_both_model_and_provider(tmp_path):
    """Test that both MODEL and PROVIDER can be set together in .env."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".env").write_text(
        "MODEL=claude-3-sonnet\nPROVIDER=openai-compatible\n",
        encoding="utf-8",
    )

    config = load_config(workspace, global_root=global_root)

    assert config.model_name == "claude-3-sonnet"
    assert config.provider == "openai-compatible"
