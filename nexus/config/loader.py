from __future__ import annotations

import os
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

from nexus.config.defaults import AgentConfig, build_default_config, config_to_plain_dict
from nexus.config.upgrade import normalize_legacy_config_values
from nexus.runtime.agent_scope import SUBAGENT_PROFILE_FIELDS, SUPERVISOR_SCOPE_FIELDS


PATH_FIELDS = {
    "skills_dir",
    "plugins_dir",
    "memory_dir",
    "session_dir",
    "knowledge_file",
    "log_dir",
    "workspace_root",
    "global_root",
    "local_root",
    "global_config_file",
    "local_config_file",
}
PATH_LIST_FIELDS = {"skill_paths"}


class ConfigError(ValueError):
    """Raised when Nexus configuration is invalid."""


def load_config(
    workspace_root: Path,
    *,
    global_root: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    local_config_path: Path | None = None,
    global_config_path: Path | None = None,
    strict: bool = True,
) -> AgentConfig:
    try:
        return _load_config_strict(
            workspace_root,
            global_root=global_root,
            cli_overrides=cli_overrides,
            local_config_path=local_config_path,
            global_config_path=global_config_path,
        )
    except Exception as exc:
        if strict:
            raise
        defaults = build_default_config(workspace_root, global_root=global_root)
        merged = config_to_plain_dict(defaults)
        merged.update(_read_environment(defaults))
        merged = _apply_agent_mode_profile(merged)
        merged = _apply_provider_defaults(merged)
        for field_name, value in merged.items():
            if hasattr(defaults, field_name):
                setattr(defaults, field_name, _coerce_value(value, getattr(defaults, field_name)))
        defaults.global_config_file = (global_config_path or defaults.global_config_file).expanduser()
        defaults.local_config_file = local_config_path or defaults.local_config_file
        defaults.config_warnings.append(
            f"Config could not be loaded; using defaults for this run: {exc}"
        )
        return defaults


def _load_config_strict(
    workspace_root: Path,
    *,
    global_root: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
    local_config_path: Path | None = None,
    global_config_path: Path | None = None,
) -> AgentConfig:
    # Load .env from the workspace root first so its values are visible to
    # all subsequent os.environ reads (including resolve_provider_api_key).
    # .env values take priority over the system environment; a key already
    # set in .env is written into os.environ regardless of any existing value.
    dotenv_path = workspace_root / ".env"
    _inject_dotenv(dotenv_path)

    defaults = build_default_config(workspace_root, global_root=global_root)
    base = config_to_plain_dict(defaults)

    global_path = (global_config_path or defaults.global_config_file).expanduser()
    local_path = local_config_path or defaults.local_config_file

    global_values = _read_toml(global_path)
    local_values = _read_toml(local_path)
    cli_values = cli_overrides or {}
    global_values = _normalize_config_layout(global_values)
    local_values = _normalize_config_layout(local_values)
    cli_values = _normalize_config_layout(cli_values)
    global_values = normalize_legacy_config_values(global_values)
    local_values = normalize_legacy_config_values(local_values)
    cli_values = normalize_legacy_config_values(cli_values)
    global_mcp_servers = _as_mcp_server_list(global_values.pop("mcp_servers", []))
    local_mcp_servers = _as_mcp_server_list(local_values.pop("mcp_servers", []))
    for duplicate_name in (
        _duplicate_mcp_server_name(global_mcp_servers),
        _duplicate_mcp_server_name(local_mcp_servers),
    ):
        if duplicate_name is not None:
            raise ConfigError(f"Duplicate mcp_servers entry '{duplicate_name}'.")

    merged = dict(base)
    merged.update(global_values)
    merged.update(local_values)
    merged.update(_read_environment(defaults))
    merged.update(cli_values)
    merged["mcp_servers"] = _active_mcp_servers(
        global_mcp_servers=global_mcp_servers,
        local_mcp_servers=local_mcp_servers,
        enabled_names=merged.get("enabled_mcp_servers", []),
        disabled_names=merged.get("disabled_mcp_servers", []),
    )
    merged = _apply_agent_mode_profile(merged)
    merged = _apply_provider_defaults(merged)

    merged["workspace_root"] = str(defaults.workspace_root)
    merged["global_root"] = str(defaults.global_root)
    merged["local_root"] = str(defaults.local_root)
    merged["global_config_file"] = str(global_path.expanduser())
    merged["local_config_file"] = str(local_path)

    resolved = _resolve_paths(merged, defaults.workspace_root)
    values: dict[str, Any] = {}
    for item in fields(AgentConfig):
        raw_value = resolved[item.name]
        values[item.name] = _coerce_value(raw_value, getattr(defaults, item.name))
        if item.name in PATH_LIST_FIELDS:
            values[item.name] = _resolve_path_list(values[item.name], defaults.workspace_root)
    _validate_config_values(values)
    return AgentConfig(**values)


def _as_mcp_server_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(entry) for entry in value if isinstance(entry, dict)]


def _mcp_server_name(entry: dict[str, Any]) -> str:
    return str(entry.get("name", "")).strip()


def _duplicate_mcp_server_name(entries: list[dict[str, Any]]) -> str | None:
    seen: set[str] = set()
    for entry in entries:
        name = _mcp_server_name(entry)
        if not name:
            continue
        if name in seen:
            return name
        seen.add(name)
    return None


def _active_mcp_servers(
    *,
    global_mcp_servers: list[dict[str, Any]],
    local_mcp_servers: list[dict[str, Any]],
    enabled_names: Any,
    disabled_names: Any,
) -> list[dict[str, Any]]:
    enabled = _normalized_name_list(enabled_names)
    disabled = {str(name).strip() for name in disabled_names if str(name).strip()} if isinstance(disabled_names, list) else set()

    global_by_name = {
        name: dict(entry)
        for entry in global_mcp_servers
        if (name := _mcp_server_name(entry))
    }
    local_by_name = {
        name: dict(entry)
        for entry in local_mcp_servers
        if (name := _mcp_server_name(entry))
    }

    active: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in enabled:
        if name in disabled or name in seen:
            continue
        entry = local_by_name.get(name) or global_by_name.get(name)
        if entry is not None:
            active.append(dict(entry))
            seen.add(name)
    return active


def _normalized_name_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item).strip()
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def _inject_dotenv(path: Path) -> None:
    """Parse a .env file and inject its key-value pairs into os.environ.

    .env values take priority: existing os.environ entries for the same keys
    are overwritten. Only simple KEY=VALUE lines are parsed; comments (#)
    and blank lines are skipped. Quoted values have their surrounding quotes
    stripped. If the file does not exist the function is a no-op.
    """
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw_value = line.partition("=")
            key = key.strip()
            raw_value = raw_value.strip()
            # Strip surrounding single or double quotes
            if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {'"', "'"}:
                raw_value = raw_value[1:-1]
            if key:
                os.environ[key] = raw_value
    except OSError:
        pass  # Unreadable .env is silently ignored


def ensure_config_dirs(config: AgentConfig) -> None:
    for path in (
        config.global_root,
        config.local_root,
        config.session_dir,
        config.memory_dir,
        config.skills_dir,
        config.plugins_dir,
        config.log_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    config.knowledge_file.parent.mkdir(parents=True, exist_ok=True)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    content = path.read_text(encoding="utf-8")
    data = tomllib.loads(_normalize_bare_all_assignments(content))
    return dict(data)


def _normalize_bare_all_assignments(content: str) -> str:
    """Accept the user-friendly ``key = all`` shorthand for scope lists."""
    lines: list[str] = []
    for line in content.splitlines():
        before_hash, hash_mark, after_hash = line.partition("#")
        key, equals, value = before_hash.partition("=")
        if equals and value.strip().lower() == "all":
            line = f'{key}{equals} "all"'
            if hash_mark:
                line = f"{line} {hash_mark}{after_hash}"
        lines.append(line)
    return "\n".join(lines)


def _normalize_config_layout(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    agents = normalized.pop("agents", None)
    if isinstance(agents, dict):
        _merge_agent_section(normalized, agents)

    subagent_entries: list[dict[str, Any]] = []
    legacy_profiles = normalized.get("subagent_profiles")
    if isinstance(legacy_profiles, list):
        subagent_entries.extend(_normalize_subagent_profile_aliases(dict(entry)) for entry in legacy_profiles if isinstance(entry, dict))

    for key in ("sub-agents", "sub_agents", "subagents"):
        raw = normalized.pop(key, None)
        subagent_entries.extend(_subagent_entries_from_section(raw))
    if subagent_entries:
        normalized["subagent_profiles"] = subagent_entries
    return normalized


def _merge_agent_section(target: dict[str, Any], agents: dict[str, Any]) -> None:
    key_map = {
        "allowed_tools": "agent_allowed_tools",
        "allowed_skills": "agent_allowed_skills",
        "allowed_mcp_servers": "agent_allowed_mcp_servers",
        "allowed_mcps": "agent_allowed_mcp_servers",
    }
    for source, destination in key_map.items():
        if source in agents:
            target[destination] = agents[source]


def _subagent_entries_from_section(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [_normalize_subagent_profile_aliases(dict(entry)) for entry in raw if isinstance(entry, dict)]
    if not isinstance(raw, dict):
        return []
    entries: list[dict[str, Any]] = []
    if "name" in raw:
        entries.append(dict(raw))
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        entry = dict(value)
        entry.setdefault("name", str(name))
        entries.append(entry)
    return [_normalize_subagent_profile_aliases(entry) for entry in entries]


def _normalize_subagent_profile_aliases(entry: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(entry)
    alias_map = {
        "allowed_mcps": "allowed_mcp_servers",
    }
    for alias, canonical in alias_map.items():
        if alias in normalized and canonical not in normalized:
            normalized[canonical] = normalized[alias]
    for obsolete in (
        "attached_tools",
        "detached_tools",
        "attached_skills",
        "detached_skills",
        "attached_mcps",
        "attached_mcp_servers",
        "detached_mcps",
        "detached_mcp_servers",
    ):
        normalized.pop(obsolete, None)
    for field_name in SUBAGENT_PROFILE_FIELDS:
        if field_name in normalized:
            normalized[field_name] = _coerce_scope_list_value(normalized[field_name])
    return normalized


def _coerce_scope_list_value(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() == "all":
        return ["all"]
    return value


def _read_environment(defaults: AgentConfig) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for item in fields(defaults):
        env_key = f"AGENT_{item.name.upper()}"
        raw_value = os.getenv(env_key)
        if raw_value is None:
            continue
        overrides[item.name] = _parse_scalar(raw_value, getattr(defaults, item.name))

    if "AGENT_MAX_TOKENS" in os.environ:
        overrides["compaction_hard_limit"] = int(os.environ["AGENT_MAX_TOKENS"])

    # Generic shorthand aliases used in .env (no AGENT_ prefix).
    # These only apply when the AGENT_-prefixed form hasn't already been set.
    _generic_aliases: list[tuple[str, str]] = [
        ("PROVIDER", "provider"),
        ("MODEL", "model_name"),
        ("API_KEY", "api_key"),
        ("BASE_URL", "api_base_url"),
    ]
    for env_key, field_name in _generic_aliases:
        if field_name not in overrides:
            raw = os.getenv(env_key)
            if raw:
                overrides[field_name] = _parse_scalar(raw, getattr(defaults, field_name))

    return overrides


def _resolve_paths(data: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    resolved = dict(data)
    for key in PATH_FIELDS:
        value = resolved.get(key)
        if not value:
            continue
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = workspace_root / path
        resolved[key] = str(path.resolve())
    return resolved


def _resolve_path_list(values: list, workspace_root: Path) -> list[Path]:
    resolved: list[Path] = []
    for value in values:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = workspace_root / path
        resolved.append(path.resolve())
    return resolved


def _coerce_value(value: Any, template: Any) -> Any:
    if isinstance(template, Path):
        return Path(value)
    if isinstance(template, bool):
        return bool(value)
    if isinstance(template, int):
        return int(value)
    if isinstance(template, float):
        return float(value)
    if isinstance(template, list):
        if isinstance(value, list):
            return list(value)
        if isinstance(value, str):
            if value.strip().lower() == "all":
                return ["all"]
            return [part.strip() for part in value.split(",") if part.strip()]
        return []
    return value


def _parse_scalar(raw_value: str, template: Any) -> Any:
    if isinstance(template, bool):
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(template, int):
        return int(raw_value)
    if isinstance(template, float):
        return float(raw_value)
    if isinstance(template, list):
        return [part.strip() for part in raw_value.split(",") if part.strip()]
    return raw_value


def _validate_config_values(values: dict[str, Any]) -> None:
    valid_modes = {"plan", "default", "auto"}
    valid_agent_modes = {"basic", "advanced"}
    valid_log_formats = {"text", "json"}
    valid_providers = {"anthropic", "cohere", "fake", "gemini", "mistral", "openai", "openai-compatible", "ollama"}
    valid_approval_policies = {
        "on-request", "approve-turn", "approve-session", "auto", "plan"
    }

    provider = str(values["provider"]).strip()
    if provider not in valid_providers:
        raise ConfigError(
            f"Invalid provider '{provider}'. Expected one of: {', '.join(sorted(valid_providers))}."
        )

    if provider in {"mistral", "openai", "openai-compatible"} and not str(values["api_base_url"]).strip():
        raise ConfigError(f"provider '{provider}' requires api_base_url to be set.")

    default_mode = values["default_mode"]
    if default_mode not in valid_modes:
        raise ConfigError(
            f"Invalid default_mode '{default_mode}'. Expected one of: {', '.join(sorted(valid_modes))}."
        )

    agent_mode = values["agent_mode"]
    if agent_mode not in valid_agent_modes:
        raise ConfigError(
            f"Invalid agent_mode '{agent_mode}'. Expected one of: {', '.join(sorted(valid_agent_modes))}."
        )

    approval_policy = values.get("approval_policy", "on-request")
    if approval_policy not in valid_approval_policies:
        raise ConfigError(
            f"Invalid approval_policy '{approval_policy}'. "
            f"Expected one of: {', '.join(sorted(valid_approval_policies))}."
        )

    log_format = values["log_format"]
    if log_format not in valid_log_formats:
        raise ConfigError(
            f"Invalid log_format '{log_format}'. Expected one of: {', '.join(sorted(valid_log_formats))}."
        )

    integer_fields = (
        "max_output_tokens",
        "compaction_soft_limit",
        "compaction_hard_limit",
        "compaction_keep_recent",
        "max_loop_iterations",
        "max_tool_calls_per_turn",
        "textual_transcript_max_lines",
        "prompt_history_max_entries",
        "tool_output_max_chars",
        "max_sessions_retained",
        "sandbox_timeout_seconds",
        "context_prune_protect_tokens",
        "context_prune_minimum_tokens",
    )
    for field_name in integer_fields:
        if values[field_name] < 1:
            raise ConfigError(f"{field_name} must be greater than 0.")

    if values["compaction_hard_limit"] < values["compaction_soft_limit"]:
        raise ConfigError("compaction_hard_limit must be greater than or equal to compaction_soft_limit.")

    temperature = values["temperature"]
    if not 0.0 <= temperature <= 2.0:
        raise ConfigError("temperature must be between 0.0 and 2.0.")

    mcp_servers = values["mcp_servers"]
    if not isinstance(mcp_servers, list):
        raise ConfigError("mcp_servers must be a list of server definitions.")
    for entry in mcp_servers:
        if not isinstance(entry, dict):
            raise ConfigError("Each mcp_servers entry must be a table with name and command fields.")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError("Each mcp_servers entry must define a non-empty name.")
        transport = str(entry.get("transport", "stdio")).strip().lower() or "stdio"
        if transport == "streamable-http":
            transport = "streamable_http"
        if transport not in {"stdio", "http", "streamable_http"}:
            raise ConfigError(f"mcp_servers entry '{name}' has unsupported transport '{transport}'.")
        command = entry.get("command")
        url = str(entry.get("url", "")).strip()
        if transport == "stdio":
            if not isinstance(command, list) or not command:
                raise ConfigError(f"mcp_servers entry '{name}' must define a non-empty command list.")
        elif not url:
            raise ConfigError(f"mcp_servers entry '{name}' must define a non-empty url.")
        env = entry.get("env")
        if env is not None and not isinstance(env, dict):
            raise ConfigError(f"mcp_servers entry '{name}' env must be a table.")
        disabled_tools = entry.get("disabled_tools", [])
        if disabled_tools is not None and not isinstance(disabled_tools, list):
            raise ConfigError(f"mcp_servers entry '{name}' disabled_tools must be a list.")
        for field_name in ("startup_timeout_seconds", "tool_timeout_seconds"):
            if field_name in entry and float(entry[field_name]) <= 0:
                raise ConfigError(f"mcp_servers entry '{name}' {field_name} must be greater than 0.")

    delegation_subagents = values["delegation_subagents"]
    if not isinstance(delegation_subagents, list):
        raise ConfigError("delegation_subagents must be a list of subagent definitions.")
    subagent_names: set[str] = set()
    for entry in delegation_subagents:
        if not isinstance(entry, dict):
            raise ConfigError("Each delegation_subagents entry must be a table.")
        name = str(entry.get("name", "")).strip()
        description = str(entry.get("description", "")).strip()
        goal_prompt = str(entry.get("goal_prompt", "")).strip()
        if not name:
            raise ConfigError("Each delegation_subagents entry must define a non-empty name.")
        if not description:
            raise ConfigError(f"delegation_subagents entry '{name}' must define a non-empty description.")
        if not goal_prompt:
            raise ConfigError(f"delegation_subagents entry '{name}' must define a non-empty goal_prompt.")
        allowed = entry.get("allowed_tools")
        if allowed is not None:
            if not isinstance(allowed, list):
                raise ConfigError(f"delegation_subagents entry '{name}' allowed_tools must be a list.")
            if any(not str(tool_name).strip() for tool_name in allowed):
                raise ConfigError(f"delegation_subagents entry '{name}' allowed_tools must only contain non-empty strings.")
        max_turns = entry.get("max_turns", 20)
        if int(max_turns) < 1:
            raise ConfigError(f"delegation_subagents entry '{name}' max_turns must be greater than 0.")
        timeout_seconds = entry.get("timeout_seconds", 600.0)
        if float(timeout_seconds) <= 0:
            raise ConfigError(f"delegation_subagents entry '{name}' timeout_seconds must be greater than 0.")
        if name in subagent_names:
            raise ConfigError(f"Duplicate delegation_subagents entry '{name}'.")
        subagent_names.add(name)

    allowed_tools = values["allowed_tools"]
    denied_tools = values["denied_tools"]
    overlap = sorted(set(allowed_tools) & set(denied_tools))
    if overlap:
        raise ConfigError("allowed_tools and denied_tools must not overlap: " + ", ".join(overlap))

    for field_name in ("enabled_skills", "disabled_skills", *SUPERVISOR_SCOPE_FIELDS):
        if not isinstance(values[field_name], list):
            raise ConfigError(f"{field_name} must be a list of names or patterns.")
        if any(not str(item).strip() for item in values[field_name]):
            raise ConfigError(f"{field_name} must only contain non-empty strings.")

    subagent_profiles = values["subagent_profiles"]
    if not isinstance(subagent_profiles, list):
        raise ConfigError("subagent_profiles must be a list of sub-agent scope definitions.")
    profile_names: set[str] = set()
    for entry in subagent_profiles:
        if not isinstance(entry, dict):
            raise ConfigError("Each subagent_profiles entry must be a table.")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError("Each subagent_profiles entry must define a non-empty name.")
        if name in profile_names:
            raise ConfigError(f"Duplicate subagent_profiles entry '{name}'.")
        profile_names.add(name)
        for field_name in SUBAGENT_PROFILE_FIELDS:
            value = entry.get(field_name, [])
            if value is not None and not isinstance(value, list):
                raise ConfigError(f"subagent_profiles entry '{name}' {field_name} must be a list.")
            if isinstance(value, list) and any(not str(item).strip() for item in value):
                raise ConfigError(f"subagent_profiles entry '{name}' {field_name} must only contain non-empty strings.")

    server_names: set[str] = set()
    for entry in mcp_servers:
        name = str(entry.get("name", "")).strip()
        if name in server_names:
            raise ConfigError(f"Duplicate mcp_servers entry '{name}'.")
        server_names.add(name)


def _apply_provider_defaults(values: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(values)
    provider = str(resolved.get("provider", "")).strip().lower()
    api_base_url = str(resolved.get("api_base_url", "")).strip()
    default_compatible_base_url = "https://api.mistral.ai/v1"
    if provider == "mistral":
        # MISTRAL_BASE_URL always takes priority; fall back to the hardcoded default
        # when api_base_url has not been set explicitly via TOML or env.
        mistral_base_url_env = os.getenv("MISTRAL_BASE_URL")
        if mistral_base_url_env:
            resolved["api_base_url"] = mistral_base_url_env
        elif not api_base_url:
            resolved["api_base_url"] = "https://api.mistral.ai/v1"
    elif provider == "cohere":
        cohere_base_url_env = os.getenv("COHERE_BASE_URL") or os.getenv("CO_API_BASE_URL")
        if cohere_base_url_env:
            resolved["api_base_url"] = cohere_base_url_env
        elif not api_base_url or api_base_url.rstrip("/") == default_compatible_base_url:
            resolved["api_base_url"] = "https://api.cohere.com"
    elif provider in {"anthropic", "gemini"}:
        # Native SDK providers do not use api_base_url by default.
        if not api_base_url:
            resolved["api_base_url"] = ""
    elif provider == "ollama":
        # Default to localhost:11434; strip /v1 suffix if user copied it from openai-compatible.
        if not api_base_url:
            ollama_host = os.getenv("OLLAMA_HOST", "").strip().rstrip("/")
            base_url_env = os.getenv("BASE_URL", "").strip().rstrip("/")
            resolved["api_base_url"] = ollama_host or base_url_env or "http://localhost:11434"
        # Strip /v1 suffix users may have left from switching providers.
        if str(resolved["api_base_url"]).endswith("/v1"):
            resolved["api_base_url"] = str(resolved["api_base_url"])[:-3]
    elif not api_base_url:
        # For openai-compatible (and openai) provider, fall back to generic BASE_URL env var.
        base_url_env = os.getenv("BASE_URL", "").strip().rstrip("/")
        if base_url_env:
            resolved["api_base_url"] = base_url_env
    return resolved


def _apply_agent_mode_profile(values: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(values)
    agent_mode = str(resolved.get("agent_mode", "basic")).strip().lower()
    resolved["agent_mode"] = agent_mode
    if agent_mode == "advanced":
        allowed_tools = resolved.get("allowed_tools")
        if isinstance(allowed_tools, list) and allowed_tools:
            for tool_name in _advanced_mode_required_tool_names():
                if tool_name not in allowed_tools:
                    allowed_tools.append(tool_name)
            resolved["allowed_tools"] = allowed_tools
    return resolved


def _advanced_mode_required_tool_names() -> tuple[str, ...]:
    return (
        *_builtin_cognitive_tool_names(),
        "read_file",
        "glob",
        "grep",
        "list_dir",
        "lsp",
        "write_file",
        "edit",
        "insert_edit_into_file",
        "apply_patch",
        "git_status",
        "git_diff",
        "run_tests",
        "run_python_check",
        "bash",
    )


def _builtin_cognitive_tool_names() -> tuple[str, ...]:
    return (
        "subagent_planning_analysis",
        "subagent_execution",
        "subagent_review",
        "subagent_verification",
    )
