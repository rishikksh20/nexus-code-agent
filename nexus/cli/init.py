from __future__ import annotations

from pathlib import Path

from nexus.memory.workspace import AgentDirs, bootstrap_workspace_knowledge
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
    return created


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
            '#   MISTRAL_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, NEXUS_API_KEY',
            '# Or override any value directly in this file.',
            '# Switch to provider = "fake" for local offline use (no API key required).',
            f'config_version = {CURRENT_CONFIG_VERSION}',
            'default_mode = "default"',
            'stream_output = true',
            'show_tool_calls = true',
            'color_output = true',
            'sandbox_image = "nexus-sandbox:latest"',
            'sandbox_timeout_seconds = 30',
            'sandbox_memory_limit = "256m"',
            'sandbox_network = "none"',
            'sandbox_read_only_workspace = true',
            'sandbox_tmp_size = "64m"',
            '',
        ]
    )


def _local_config_toml(*, workspace_root: Path, project_name: str, project_description: str) -> str:
    return "\n".join(
        [
            f'project_name = "{project_name}"',
            f'project_description = "{project_description}"',
            f'config_version = {CURRENT_CONFIG_VERSION}',
            '# Allowlist of tools available in this workspace.',
            '# Remove this key entirely (or set to []) to allow ALL registered tools.',
            '# Builtin tool names: get_time, read_file, write_file, edit,',
            '#   insert_edit_into_file, apply_patch, glob, grep,',
            '#   list_dir, lsp, find_references, code_index, semantic_search,',
            '#   git_status, git_diff, run_tests, run_linter, run_typecheck,',
            '#   run_formatter, bash, memory, todos, web_fetch, web_search',
            '# Advanced cognitive tool names: subagent_planning_analysis,',
            '#   subagent_execution, subagent_review, subagent_verification',
            '# Add plugin or sandboxed command tool names here when enabling them.',
            '# MCP tool names are discovered dynamically from active MCP servers;',
            '# activate/deactivate MCP servers by name instead of adding MCP tool names here.',
            'allowed_tools = ["get_time", "read_file", "write_file", "edit", "insert_edit_into_file", "apply_patch", "glob", "grep", "list_dir", "lsp", "find_references", "code_index", "semantic_search", "git_status", "git_diff", "run_tests", "run_linter", "run_typecheck", "run_formatter", "bash", "memory", "todos", "web_fetch", "web_search", "subagent_planning_analysis", "subagent_execution", "subagent_review", "subagent_verification"]',
            'denied_tools = []',
            '# Hidden/private dot-path reads are blocked by default. Set this to true to allow',
            '# reading hidden/private paths other than .nexus for this workspace. .nexus stays blocked.',
            'allow_hidden_paths = false',
            '# Run `nexus doctor --output-format json` before wider rollout to verify production gates.',
            'mcp_servers = []',
            'enabled_mcp_servers = []',
            'disabled_mcp_servers = []',
            f'# Example: mcp_servers = [{mcp_server_example_for_workspace(workspace_root)}]',
            '# Global MCP servers from ~/.nexus/config.toml can be enabled here by name,',
            '# or interactively with /mcp activate <name> and /mcp deactivate <name>.',
            '# Agent mode profile:',
            '#   basic = single LLM execution, no cognitive sub-agent tools',
            '#   advanced = supervisor LLM with cognitive sub-agent tools',
            'agent_mode = "basic"',
            'delegation_subagents = [] # Custom cognitive sub-agent definitions.',
            '# Optional specialists for advanced mode:',
            '# delegation_subagents = [',
            '#   { name = "planning_analysis", description = "Analyze repo structure and produce an implementation plan.", goal_prompt = "Read-only planning and analysis agent. Do not modify files.", allowed_tools = ["read_file", "glob", "grep", "list_dir", "lsp"] },',
            '#   { name = "review", description = "Review code changes for bugs and maintainability.", goal_prompt = "Senior code reviewer. Inspect diffs and report issues only.", allowed_tools = ["git_diff", "read_file", "grep", "lsp"] },',
            '# ]',
            '# Example: delegation_subagents = [{ name = "explore", description = "Investigate a focused codebase question.", goal_prompt = "Read the relevant code and summarize the answer.", allowed_tools = ["read_file", "glob", "grep"], max_turns = 12, timeout_seconds = 300 }]',
            'sandbox_commands = false',
            '',
        ]
    )
