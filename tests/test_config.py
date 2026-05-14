from __future__ import annotations

from nexus.cli.init import init_workspace
from nexus.config import load_config
from nexus.config.loader import ConfigError


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


def test_config_accepts_structured_mcp_servers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'mcp_servers = [{ name = "filesystem", command = ["uvx", "mcp-server-filesystem", "."], prefix = "fs_" }]\n',
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


def test_config_rejects_empty_delegation_workers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'delegation_workers = []\n',
        encoding="utf-8",
    )

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "delegation_workers" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for empty delegation_workers")


def test_config_rejects_overlapping_tool_filters(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    global_root = tmp_path / "global"
    init_workspace(workspace, global_root=global_root, project_name="workspace")
    (workspace / ".nexus" / "config.toml").write_text(
        'allowed_tools = ["get_time", "write_note"]\ndenied_tools = ["write_note"]\n',
        encoding="utf-8",
    )

    try:
        load_config(workspace, global_root=global_root)
    except ConfigError as exc:
        assert "must not overlap" in str(exc)
    else:
        raise AssertionError("Expected ConfigError for overlapping tool filters")


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
