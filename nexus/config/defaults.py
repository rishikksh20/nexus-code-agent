from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class AgentConfig:
    config_version: int = 4
    provider: str = "openai-compatible"
    model_name: str = "mistral-medium-latest"
    api_base_url: str = "https://api.mistral.ai/v1"
    api_key: str = ""       # Set via API_KEY in .env or api_key in config.toml
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_model_profile: str = ""
    active_model_profile_legacy: bool = True
    context_length: int = 0
    max_output_tokens: int = 4096
    reserved_output_tokens: int = 4096
    temperature: float = 0.0
    top_p: float = 1.0
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_reasoning: bool = False
    llm_thinking_mode: str = "auto"
    llm_reasoning_effort: str = "high"
    compaction_soft_limit: int = 10_000
    compaction_hard_limit: int = 14_000
    compaction_limits_auto: bool = True
    compaction_keep_recent: int = 12
    default_mode: str = "default"
    auto_confirm_read_only: bool = True
    max_loop_iterations: int = 50
    max_tool_calls_per_turn: int = 30
    ask_user_max_questions_per_turn: int = 3
    parallel_tools: bool = True
    parallel_tool_window: int = 4
    stream_output: bool = True
    show_tool_calls: bool = True
    show_thinking_indicator: bool = True
    color_output: bool = True
    textual_ui: bool = True
    textual_transcript_max_lines: int = 5000
    prompt_history_max_entries: int = 200
    tool_output_max_chars: int = 100 * 1024
    shell_inherit_environment: bool = False
    max_sessions_retained: int = 50
    save_on_every_turn: bool = True
    skills_dir: Path = field(default_factory=Path)
    skill_paths: list[Path] = field(default_factory=list)
    plugins_dir: Path = field(default_factory=Path)
    memory_dir: Path = field(default_factory=Path)
    session_dir: Path = field(default_factory=Path)
    knowledge_file: Path = field(default_factory=Path)
    log_level: str = "WARNING"
    log_format: str = "text"
    log_dir: Path = field(default_factory=Path)
    sentry_enabled: bool = False
    sentry_dsn: str = ""
    sentry_environment: str = "development"
    sentry_release: str = ""
    sentry_sample_rate: float = 1.0
    sentry_traces_sample_rate: float = 0.1
    sentry_profiles_sample_rate: float = 0.0
    sentry_profile_session_sample_rate: float = 0.0
    sentry_enable_logs: bool = True
    sentry_send_default_pii: bool = False
    sentry_include_prompts: bool = False
    sentry_include_tool_outputs: bool = False
    sentry_capture_tool_errors: bool = False
    sentry_capture_provider_errors: bool = True
    sentry_capture_mcp_errors: bool = True
    sentry_max_breadcrumbs: int = 100
    sentry_max_value_length: int = 4096
    sentry_flush_timeout_seconds: float = 2.0
    sentry_debug: bool = False
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_environment: str = "development"
    langfuse_release: str = ""
    langfuse_trace_content: bool = True
    langfuse_trace_tool_outputs: bool = True
    langfuse_prompt_name: str = "nexus-system-prompt"
    langfuse_prompt_version: str = ""
    langfuse_flush_timeout_seconds: float = 2.0
    otel_enabled: bool = False
    otel_endpoint: str = ""
    otel_headers: str = ""
    otel_service_name: str = "nexus"
    otel_environment: str = "development"
    otel_release: str = ""
    otel_trace_content: bool = True
    otel_trace_tool_outputs: bool = True
    otel_prompt_name: str = "nexus-system-prompt"
    otel_prompt_version: str = ""
    otel_jsonl_enabled: bool = True
    otel_flush_timeout_seconds: float = 2.0
    project_name: str = ""
    project_description: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    enabled_mcp_servers: list[str] = field(default_factory=list)
    disabled_mcp_servers: list[str] = field(default_factory=list)
    enabled_skills: list[str] = field(default_factory=list)
    disabled_skills: list[str] = field(default_factory=list)
    agent_allowed_tools: list[str] = field(default_factory=list)
    agent_allowed_skills: list[str] = field(default_factory=list)
    agent_allowed_mcp_servers: list[str] = field(default_factory=list)
    approval_policy: str = "on-request"
    allow_hidden_paths: bool = False
    developer_instructions: str = ""
    user_instructions: str = ""
    context_prune_enabled: bool = True
    context_prune_protect_tokens: int = 40_000
    context_prune_minimum_tokens: int = 20_000
    agent_mode: str = "basic"
    delegation_subagents: list[dict[str, Any]] = field(default_factory=list)
    subagent_profiles: list[dict[str, Any]] = field(default_factory=list)
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
    config_warnings: list[str] = field(default_factory=list)


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
        elif isinstance(value, list) and any(isinstance(part, Path) for part in value):
            plain[item.name] = [str(part) if isinstance(part, Path) else part for part in value]
        else:
            plain[item.name] = value
    return plain
