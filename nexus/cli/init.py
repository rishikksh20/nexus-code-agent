from __future__ import annotations

from pathlib import Path

from nexus.memory.workspace import AgentDirs, bootstrap_workspace_knowledge
from nexus.integrations.mcp import mcp_server_example_for_workspace


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
            'provider = "mistral"',
            'model_name = "mistral-medium-latest"',
            '# API key resolution order for Mistral:',
            '#   1. MISTRAL_API_KEY in workspace .env file',
            '#   2. MISTRAL_API_KEY environment variable',
            '#   3. NEXUS_API_KEY environment variable',
            '# Create a .env file in your workspace root: MISTRAL_API_KEY=your_key_here',
            '# Mistral base URL defaults to https://api.mistral.ai/v1.',
            '# Override with MISTRAL_BASE_URL env var or api_base_url in config.',
            '# Switch to provider = "fake" for local offline use (no API key required).',
            'default_mode = "default"',
            'stream_output = true',
            'show_tool_calls = true',
            'color_output = true',
            'write_note_max_bytes = 65536',
            'delegation_poll_interval_seconds = 0.05',
            'delegation_message_history_limit = 200',
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
            '# Allowlist of tools available in this workspace.',
            '# Remove this key entirely (or set to []) to allow ALL registered tools.',
            '# Builtin tools: get_time, read_file, write_file, modify_file, replace_text, glob, grep, ls, bash',
            '# Add external tool names here when enabling plugins, MCP, or the sandboxed command tool.',
            'allowed_tools = ["get_time", "read_file", "write_file", "modify_file", "replace_text", "glob", "grep", "ls", "bash", "write_note"]',
            'denied_tools = []',
            '# Run `nexus doctor --output-format json` before wider rollout to verify production gates.',
            'mcp_servers = []',
            f'# Example: mcp_servers = [{mcp_server_example_for_workspace(workspace_root)}]',
            'delegation_enabled = false',
            'delegation_workers = ["worker-1", "worker-2"]',
            'sandbox_commands = false',
            '',
        ]
    )
