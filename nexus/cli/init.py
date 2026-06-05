from __future__ import annotations

import shutil
from pathlib import Path

from nexus.memory.workspace import AgentDirs, bootstrap_workspace_knowledge
from nexus.skills import BUILTIN_SKILLS_DIR
from nexus.tools.mcp import mcp_server_example_for_workspace
from nexus.config.upgrade import CURRENT_CONFIG_VERSION


def init_workspace(
    workspace_root: Path,
    *,
    global_root: Path,
    project_name: str,
    project_description: str = "",
    force: bool = False,
) -> list[Path]:
    dirs = AgentDirs(workspace_root=workspace_root.resolve(), global_root=global_root.resolve())
    dirs.ensure()

    created: list[Path] = []
    if force or not dirs.global_config_file.exists():
        dirs.global_config_file.write_text(_global_config_toml(), encoding="utf-8")
        created.append(dirs.global_config_file)
    if force or not dirs.local_config_file.exists():
        dirs.local_config_file.write_text(
            _local_config_toml(
                workspace_root=workspace_root,
                project_name=project_name,
                project_description=project_description,
            ),
            encoding="utf-8",
        )
        created.append(dirs.local_config_file)
    if force or not dirs.knowledge_file.exists():
        bootstrap_workspace_knowledge(
            dirs.knowledge_file,
            project_name=project_name,
            description=project_description,
        )
        created.append(dirs.knowledge_file)
    created.extend(_copy_builtin_skills_to_workspace(workspace_root, force=force))
    return created


def _copy_builtin_skills_to_workspace(workspace_root: Path, *, force: bool) -> list[Path]:
    """Copy packaged skills into the standard workspace-visible skills root."""
    workspace_skills_dir = workspace_root.resolve() / ".agents" / "skills"
    copied: list[Path] = []
    for source_dir in sorted(BUILTIN_SKILLS_DIR.iterdir()):
        if not source_dir.is_dir() or not (source_dir / "SKILL.md").is_file():
            continue
        destination_dir = workspace_skills_dir / source_dir.name
        if destination_dir.exists() and not force:
            continue
        shutil.copytree(source_dir, destination_dir, dirs_exist_ok=force)
        copied.append(destination_dir)
    return copied


def _global_config_toml() -> str:
    return "\n".join(
        [
            'provider = "openai-compatible"',
            'model_name = "mistral-medium-latest"',
            '# Model and provider can be overridden via the workspace .env file:',
            '#   PROVIDER=mistral',
            '#   MODEL=mistral-medium-latest',
            '# API key and base URL are also loaded from the workspace .env file:',
            '#   API_KEY=your_key_here',
            '#   BASE_URL=https://api.mistral.ai/v1',
            '# You can also use provider-specific env vars:',
            '#   MISTRAL_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, COHERE_API_KEY, GEMINI_API_KEY, NEXUS_API_KEY',
            '# Or override any value directly in this file.',
            '# Switch to provider = "fake" for local offline use (no API key required).',
            '# Thinking-mode controls for compatible providers: auto, enabled, disabled.',
            'llm_thinking_mode = "auto"',
            'llm_reasoning_effort = "high"',
            f'config_version = {CURRENT_CONFIG_VERSION}',
            'default_mode = "default"',
            'ask_user_max_questions_per_turn = 3',
            'stream_output = true',
            'show_tool_calls = true',
            'color_output = true',
            'textual_transcript_max_lines = 5000',
            'prompt_history_max_entries = 200',
            'tool_output_max_chars = 102400',
            'shell_inherit_environment = false',
            'sandbox_image = "nexus-sandbox:latest"',
            'sandbox_timeout_seconds = 30',
            'sandbox_memory_limit = "256m"',
            'sandbox_network = "none"',
            'sandbox_read_only_workspace = true',
            'sandbox_tmp_size = "64m"',
            '',
            '[providers.openai-compatible]',
            'enabled = true',
            'api_key_env = "API_KEY"',
            'base_url = "https://api.mistral.ai/v1"',
            'timeout_seconds = 120',
            'max_retries = 3',
            '',
            '[models.default-mistral-compatible]',
            'provider = "openai-compatible"',
            'model_name = "mistral-medium-latest"',
            'context_length = 200000',
            'max_output_tokens = 4096',
            'reserved_output_tokens = 4096',
            'temperature = 0.0',
            'top_p = 1.0',
            'supports_tools = true',
            'supports_streaming = true',
            'supports_reasoning = false',
            '',
            '[models.default-mistral-compatible.thinking]',
            'enabled = false',
            'mode = "provider_default"',
            '',
        ]
    )


def _local_config_toml(*, workspace_root: Path, project_name: str, project_description: str) -> str:
    return "\n".join(
        [
            f'project_name = "{project_name}"',
            f'project_description = "{project_description}"',
            f'config_version = {CURRENT_CONFIG_VERSION}',
            '# Select a reusable model profile for this workspace:',
            '# active_model_profile = "default-mistral-compatible"',
            '# Allowlist of tools available in this workspace.',
            '# Remove this key entirely (or set to []) to allow ALL registered tools.',
            '# Builtin tool names: get_time, read_file, write_file, edit,',
            '#   insert_edit_into_file, apply_patch, glob, grep,',
            '#   list_dir, lsp, code_index, semantic_search,',
            '#   git_status, git_diff, run_tests, run_python_check,',
            '#   run_formatter, bash, memory, todos, web_fetch, web_search, ask_user',
            '# Advanced cognitive tool names: subagent_explorer,',
            '#   subagent_coding, subagent_code_reviewer, subagent_impact_analyzer',
            '# Add plugin or sandboxed command tool names here when enabling them.',
            '# MCP tool names are discovered dynamically from active MCP servers;',
            '# activate/deactivate MCP servers by name instead of adding MCP tool names here.',
            'allowed_tools = ["get_time", "read_file", "write_file", "edit", "insert_edit_into_file", "apply_patch", "glob", "grep", "list_dir", "lsp", "code_index", "semantic_search", "git_status", "git_diff", "run_tests", "run_python_check", "run_formatter", "bash", "memory", "todos", "web_fetch", "web_search", "ask_user", "subagent_explorer", "subagent_coding", "subagent_code_reviewer", "subagent_impact_analyzer"]',
            'denied_tools = []',
            'ask_user_max_questions_per_turn = 3',
            '# Hidden/private dot-path reads are blocked by default. .agents/skills and',
            '# .agents/tools stay readable so agents can inspect project resources.',
            '# Set this to true to allow other hidden/private paths. .nexus stays blocked.',
            'allow_hidden_paths = false',
            'textual_transcript_max_lines = 5000',
            'prompt_history_max_entries = 200',
            'tool_output_max_chars = 102400',
            'shell_inherit_environment = false',
            '# Sentry remote monitoring. Local JSONL logs and audit trail remain separate.',
            'sentry_enabled = false',
            'sentry_dsn = ""',
            'sentry_environment = "development"',
            'sentry_release = ""',
            'sentry_sample_rate = 1.0',
            'sentry_traces_sample_rate = 0.1',
            'sentry_profiles_sample_rate = 0.0',
            'sentry_profile_session_sample_rate = 0.0',
            'sentry_enable_logs = true',
            'sentry_send_default_pii = false',
            'sentry_include_prompts = false',
            'sentry_include_tool_outputs = false',
            'sentry_capture_tool_errors = false',
            'sentry_capture_provider_errors = true',
            'sentry_capture_mcp_errors = true',
            '# Vendor-neutral tracing. Writes spans to ~/.nexus/logs/traces.jsonl and can export OTLP.',
            'otel_enabled = false',
            'otel_endpoint = ""',
            'otel_headers = ""',
            'otel_service_name = "nexus"',
            'otel_environment = "development"',
            'otel_release = ""',
            'otel_trace_content = true',
            'otel_trace_tool_outputs = true',
            'otel_prompt_name = "nexus-system-prompt"',
            'otel_prompt_version = ""',
            'otel_jsonl_enabled = true',
            'otel_flush_timeout_seconds = 2.0',
            '# Langfuse compatibility keys. Nexus keeps prompts local and only derives OTLP auth/endpoint.',
            'langfuse_enabled = false',
            'langfuse_public_key = ""',
            'langfuse_secret_key = ""',
            'langfuse_base_url = "https://cloud.langfuse.com"',
            'langfuse_environment = "development"',
            'langfuse_release = ""',
            'langfuse_trace_content = true',
            'langfuse_trace_tool_outputs = true',
            'langfuse_prompt_name = "nexus-system-prompt"',
            'langfuse_prompt_version = ""',
            'langfuse_flush_timeout_seconds = 2.0',
            '# Run `nexus doctor --output-format json` before wider rollout to verify production gates.',
            'mcp_servers = []',
            'enabled_mcp_servers = []',
            'disabled_mcp_servers = []',
            f'# Example: mcp_servers = [{mcp_server_example_for_workspace(workspace_root)}]',
            '# Global MCP servers from ~/.nexus/config.toml can be enabled here by name,',
            '# or interactively with /mcp activate <name> and /mcp deactivate <name>.',
            '# Skills use Agent Skills directories with SKILL.md frontmatter.',
            'skill_paths = []',
            'enabled_skills = []',
            'disabled_skills = []',
            '# Agent mode profile:',
            '#   basic = single LLM execution, no cognitive sub-agent tools',
            '#   advanced = supervisor LLM with all built-in cognitive sub-agent tools',
            '# Listing names under [[sub-agents]] also loads those sub-agent tools.',
            'agent_mode = "basic"',
            'delegation_subagents = [] # Custom cognitive sub-agent definitions.',
            '# Optional specialists for advanced mode:',
            '# delegation_subagents = [',
            '#   { name = "explorer", description = "Summarize a bounded codebase slice.", goal_prompt = "Read-only explorer. Stop once you have enough evidence to answer.", allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"] },',
            '#   { name = "impact_analyzer", description = "Scope blast radius and verification targets.", goal_prompt = "Read-only impact analyzer. Return affected files, risks, and candidate tests.", allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"] },',
            '# ]',
            '# Example: delegation_subagents = [{ name = "explore", description = "Investigate a focused codebase question.", goal_prompt = "Read the relevant code and summarize the answer.", allowed_tools = ["read_file", "glob", "grep"], max_turns = 12, timeout_seconds = 300 }]',
            'sandbox_commands = false',
            '',
            '# Optional main/supervisor agent resource scope.',
            '# Global MCP/skill activation still happens through /mcp and /skills.',
            '# Empty allowed_* lists preserve the default behavior.',
            '# In advanced mode, empty allowed_* means delegate through sub-agents by default.',
            '# Set allowed_tools/allowed_skills/allowed_mcp_servers to "all" to inherit',
            '# all normal tools, active skills, or active MCP servers for that scope.',
            '[agents]',
            'allowed_tools = ["bash", "read_file", "ask_user"]',
            'allowed_skills = []',
            'allowed_mcp_servers = []',
            '',
            '# Optional per-sub-agent resource scopes.',
            '[[sub-agents]]',
            'name = "explorer"',
            'allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"]',
            'allowed_mcps = []',
            'allowed_skills = []',
            '',
            '[[sub-agents]]',
            'name = "coding"',
            'allowed_tools = ["read_file", "write_file", "edit", "insert_edit_into_file", "apply_patch", "glob", "grep", "list_dir", "lsp", "git_status", "git_diff", "run_python_check", "run_formatter"]',
            'allowed_mcps = []',
            'allowed_skills = []',
            '',
            '[[sub-agents]]',
            'name = "code_reviewer"',
            'allowed_tools = ["git_diff", "read_file", "grep", "lsp", "git_status", "run_tests", "run_python_check"]',
            'allowed_mcps = []',
            'allowed_skills = []',
            '',
            '[[sub-agents]]',
            'name = "impact_analyzer"',
            'allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"]',
            'allowed_mcps = []',
            'allowed_skills = []',
            '',
        ]
    )
