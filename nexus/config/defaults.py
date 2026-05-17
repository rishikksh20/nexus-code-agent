from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AgentConfig:
    config_version: int = 2
    provider: str = "openai-compatible"
    model_name: str = "mistral-medium-latest"
    api_base_url: str = "https://api.mistral.ai/v1"
    api_key: str = ""       # Set via API_KEY in .env or api_key in config.toml
    max_output_tokens: int = 4096
    temperature: float = 0.0
    compaction_soft_limit: int = 10_000
    compaction_hard_limit: int = 14_000
    compaction_keep_recent: int = 12
    default_mode: str = "default"
    auto_confirm_read_only: bool = True
    max_loop_iterations: int = 8
    max_tool_calls_per_turn: int = 30
    stream_output: bool = True
    show_tool_calls: bool = True
    show_thinking_indicator: bool = True
    color_output: bool = True
    max_sessions_retained: int = 50
    save_on_every_turn: bool = True
    skills_dir: Path = field(default_factory=Path)
    plugins_dir: Path = field(default_factory=Path)
    memory_dir: Path = field(default_factory=Path)
    session_dir: Path = field(default_factory=Path)
    knowledge_file: Path = field(default_factory=Path)
    log_level: str = "WARNING"
    log_format: str = "text"
    log_dir: Path = field(default_factory=Path)
    project_name: str = ""
    project_description: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    approval_policy: str = "on-request"
    allow_hidden_paths: bool = False
    developer_instructions: str = ""
    user_instructions: str = ""
    context_prune_enabled: bool = True
    context_prune_protect_tokens: int = 40_000
    context_prune_minimum_tokens: int = 20_000
    agent_mode: str = "basic"
    delegation_subagents: list[dict[str, Any]] = field(default_factory=list)
    sandbox_commands: bool = False
    sandbox_image: str = "nexus-sandbox:latest"
    sandbox_timeout_seconds: int = 30
    sandbox_memory_limit: str = "256m"
    sandbox_network: str = "none"
    sandbox_read_only_workspace: bool = True
    sandbox_tmp_size: str = "64m"
    workspace_root: Path = field(default_factory=Path.cwd)
    global_root: Path = field(default_factory=lambda: Path.home() / ".nexus")
    local_root: Path = field(default_factory=Path)
    global_config_file: Path = field(default_factory=Path)
    local_config_file: Path = field(default_factory=Path)


def build_default_config(workspace_root: Path, global_root: Path | None = None) -> AgentConfig:
    resolved_workspace = workspace_root.resolve()
    resolved_global = (global_root or (Path.home() / ".nexus")).expanduser().resolve()
    local_root = resolved_workspace / ".nexus"
    return AgentConfig(
        skills_dir=resolved_global / "skills",
        plugins_dir=resolved_global / "plugins",
        memory_dir=local_root / "memory",
        session_dir=local_root / "sessions",
        knowledge_file=local_root / "knowledge.md",
        log_dir=resolved_global / "logs",
        project_name=resolved_workspace.name,
        workspace_root=resolved_workspace,
        global_root=resolved_global,
        local_root=local_root,
        global_config_file=resolved_global / "config.toml",
        local_config_file=local_root / "config.toml",
    )


def config_to_plain_dict(config: AgentConfig) -> dict[str, Any]:
    plain: dict[str, Any] = {}
    for item in fields(config):
        value = getattr(config, item.name)
        if isinstance(value, Path):
            plain[item.name] = str(value)
        else:
            plain[item.name] = value
    return plain
