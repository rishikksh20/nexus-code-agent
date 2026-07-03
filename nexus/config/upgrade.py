from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from nexus.config.model_catalog import get_model_context_limit
from nexus.runtime.agent_scope import BUILTIN_SUBAGENT_SPECS, SUPERVISOR_DEFAULT_TOOLS


CURRENT_CONFIG_VERSION = 5
UPGRADE_MARKER = "# Added by Nexus config upgrade"
DEPRECATED_CONFIG_KEYS: dict[str, str] = {
    "multi_agent_mode": "Use agent_mode = \"basic\" or agent_mode = \"advanced\" instead.",
    "delegation_enabled": "Cognitive sub-agents are controlled by agent_mode = \"advanced\".",
    "delegation_workers": "Worker delegation has been replaced by cognitive sub-agent tools.",
    "delegation_poll_interval_seconds": "Worker delegation has been replaced by cognitive sub-agent tools.",
    "delegation_message_history_limit": "Worker delegation has been replaced by cognitive sub-agent tools.",
    "multi_agent_show_plan": "Advanced mode now lets the supervisor call cognitive sub-agent tools directly.",
    "multi_agent_max_parallel_tasks": "The old automatic multi-agent DAG scheduler has been removed.",
    "multi_agent_max_repair_iterations": "The old automatic multi-agent repair scheduler has been removed.",
    "multi_agent_complexity_threshold": "The old automatic multi-agent complexity router has been removed.",
}
LEGACY_TOOL_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "delegate_task": (),
    "subagent_research": ("subagent_explorer",),
    "subagent_test": ("subagent_code_reviewer",),
    "subagent_planning_analysis": ("subagent_explorer",),
    "subagent_execution": ("subagent_coding",),
    "subagent_review": ("subagent_code_reviewer",),
    "subagent_verification": ("subagent_impact_analyzer",),
}
LEGACY_SUBAGENT_NAME_ALIASES: dict[str, str] = {
    "research": "explorer",
    "test": "code_reviewer",
    "planning_analysis": "explorer",
    "execution": "coding",
    "review": "code_reviewer",
    "verification": "impact_analyzer",
}
LEGACY_AGENT_SCOPE_KEYS: dict[str, str] = {
    "agent_allowed_tools": "allowed_tools",
    "agent_allowed_skills": "allowed_skills",
    "agent_allowed_mcp_servers": "allowed_mcp_servers",
}
AGENT_DELTA_SCOPE_KEYS: dict[str, str] = {
    "agent_add_tools": "add_tools",
    "agent_remove_tools": "remove_tools",
    "agent_add_skills": "add_skills",
    "agent_remove_skills": "remove_skills",
    "agent_add_mcp_servers": "add_mcp_servers",
    "agent_remove_mcp_servers": "remove_mcp_servers",
}
OBSOLETE_SCOPE_KEYS: frozenset[str] = frozenset(
    {
        "agent_attached_tools",
        "agent_detached_tools",
        "agent_attached_skills",
        "agent_detached_skills",
        "agent_attached_mcp_servers",
        "agent_detached_mcp_servers",
    }
)


@dataclass(slots=True, frozen=True)
class ConfigUpgradeReport:
    path: Path
    missing_keys: tuple[str, ...] = ()
    deprecated_keys: tuple[str, ...] = ()
    allowed_tool_additions: tuple[str, ...] = ()
    allowed_tools_updated: bool = False
    agent_scope_migrated: bool = False
    subagent_scope_migrated: bool = False
    legacy_subagent_names_migrated: bool = False
    current_version: int | None = None
    target_version: int = CURRENT_CONFIG_VERSION

    @property
    def needs_upgrade(self) -> bool:
        return bool(
            self.missing_keys
            or self.deprecated_keys
            or self.allowed_tool_additions
            or self.allowed_tools_updated
            or self.agent_scope_migrated
            or self.subagent_scope_migrated
            or self.legacy_subagent_names_migrated
            or self.current_version != self.target_version
        )


def inspect_config_upgrade(path: Path, template_str: str) -> ConfigUpgradeReport:
    existing = _read_top_level_toml(path)
    template = tomllib.loads(template_str)
    missing = tuple(key for key in template if key not in existing)
    deprecated = tuple(key for key in DEPRECATED_CONFIG_KEYS if key in existing)
    upgraded_allowed_tools = _upgraded_allowed_tools(existing, template)
    allowed_tool_additions = _allowed_tool_additions(existing, template)
    agent_scope_migrated = _needs_agent_scope_migration(existing)
    subagent_scope_migrated = _needs_subagent_scope_migration(existing)
    legacy_subagent_names_migrated = _needs_legacy_subagent_name_migration(existing)
    version = _optional_int(existing.get("config_version"))
    return ConfigUpgradeReport(
        path=path,
        missing_keys=missing,
        deprecated_keys=deprecated,
        allowed_tool_additions=allowed_tool_additions,
        allowed_tools_updated=upgraded_allowed_tools is not None,
        agent_scope_migrated=agent_scope_migrated,
        subagent_scope_migrated=subagent_scope_migrated,
        legacy_subagent_names_migrated=legacy_subagent_names_migrated,
        current_version=version,
    )


def upgrade_config_file(path: Path, template_str: str) -> ConfigUpgradeReport:
    before = inspect_config_upgrade(path, template_str)
    template = tomllib.loads(template_str)
    existing = _read_top_level_toml(path)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if content and not _can_parse_toml(content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template_str.rstrip() + "\n", encoding="utf-8")
        return before
    lines = _remove_deprecated_and_version_lines(content.splitlines())
    if _needs_agent_scope_migration(existing):
        lines = _remove_top_level_assignments(lines, {*LEGACY_AGENT_SCOPE_KEYS, *OBSOLETE_SCOPE_KEYS})
        lines = _remove_table(lines, "agents")
    subagent_scope_migrated = _needs_subagent_scope_migration(existing)
    if subagent_scope_migrated:
        lines = _remove_top_level_assignments(lines, {"subagent_profiles"})
        lines = _remove_array_table(lines, "sub-agents")
    upgraded_allowed_tools = _upgraded_allowed_tools(existing, template)
    if upgraded_allowed_tools is not None:
        lines = _replace_top_level_assignment(
            lines,
            "allowed_tools",
            _render_toml_value(upgraded_allowed_tools),
        )
    upgraded_delegation_subagents = _upgraded_delegation_subagents(existing)
    if upgraded_delegation_subagents is not None:
        lines = _replace_top_level_assignment(
            lines,
            "delegation_subagents",
            _render_toml_value(upgraded_delegation_subagents),
        )

    additions: list[str] = []
    additions.append(UPGRADE_MARKER)
    additions.append(f"config_version = {CURRENT_CONFIG_VERSION}")
    migrated_agent_scope = _migrated_agent_scope(existing, template.get("agents", {})) if _needs_agent_scope_migration(existing) else {}
    migrated_subagents = _migrated_subagent_profiles(existing) if subagent_scope_migrated else []
    migrated_profile_values = _migrated_provider_profile_values(existing, template)
    for key in template:
        if key == "config_version" or key in existing:
            continue
        if key == "active_model_profile":
            # Existing flat configs remain active until the user explicitly
            # selects a generated or newly created model profile.
            continue
        if key == "sub-agents" and migrated_subagents:
            continue
        value = migrated_profile_values.get(key, template[key])
        value = _migrated_agent_scope(existing, value) if key == "agents" else value
        additions.extend(_render_toml_assignment(key, value))
    if migrated_agent_scope:
        if additions and additions[-1].strip():
            additions.append("")
        additions.extend(_render_toml_assignment("agents", migrated_agent_scope))
    if migrated_subagents:
        if additions and additions[-1].strip():
            additions.append("")
        additions.extend(_render_toml_assignment("sub-agents", migrated_subagents))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(_insert_before_first_table(lines, additions)).rstrip() + "\n", encoding="utf-8")
    return before


def normalize_legacy_config_values(values: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(values)
    legacy_mode = normalized.pop("multi_agent_mode", None)
    if "agent_mode" not in normalized and isinstance(legacy_mode, str):
        mode = legacy_mode.strip().lower()
        if mode in {"auto", "always"}:
            normalized["agent_mode"] = "advanced"
        elif mode == "off":
            normalized["agent_mode"] = "basic"
    for key in (
        "allowed_tools",
        "denied_tools",
        "agent_allowed_tools",
        "agent_add_tools",
        "agent_remove_tools",
    ):
        tool_names = normalized.get(key)
        if isinstance(tool_names, list):
            normalized[key] = _normalize_legacy_tool_names(tool_names)
    return normalized


def _normalize_legacy_tool_names(tool_names: list[Any]) -> list[str]:
    normalized: list[str] = []
    for item in tool_names:
        name = str(item).strip()
        if not name:
            continue
        replacements = LEGACY_TOOL_NAME_ALIASES.get(name)
        if replacements is None:
            candidates = (name,)
        else:
            candidates = replacements
        for candidate in candidates:
            if candidate not in normalized:
                normalized.append(candidate)
    if "subagent_explorer" in normalized and "subagent_coding" not in normalized:
        normalized.append("subagent_coding")
    return normalized


def normalize_legacy_subagent_name(name: Any) -> str:
    text = str(name).strip()
    if text.startswith("subagent_"):
        text = text[len("subagent_") :]
    if text.startswith("subagent-"):
        text = text[len("subagent-") :]
    normalized = text.replace("-", "_")
    return LEGACY_SUBAGENT_NAME_ALIASES.get(normalized, normalized)


def _normalize_legacy_subagent_profile(entry: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(entry)
    if "name" in migrated:
        migrated["name"] = normalize_legacy_subagent_name(migrated["name"])
    for key in ("allowed_tools", "add_tools", "remove_tools"):
        tool_names = migrated.get(key)
        if isinstance(tool_names, list):
            migrated[key] = _normalize_legacy_tool_names(tool_names)
    return migrated


def _normalize_legacy_delegation_subagent(entry: dict[str, Any]) -> dict[str, Any]:
    migrated = _normalize_legacy_subagent_profile(entry)
    allowed_mcps = migrated.get("allowed_mcp_servers")
    if allowed_mcps is not None and "allowed_mcps" not in migrated:
        migrated["allowed_mcps"] = allowed_mcps
    return migrated


def _allowed_tool_additions(existing: dict[str, Any], template: dict[str, Any]) -> tuple[str, ...]:
    current = existing.get("allowed_tools")
    if not isinstance(current, list) or not current:
        return ()
    upgraded = _upgraded_allowed_tools(existing, template)
    if upgraded is None:
        return ()
    current_names = _normalize_legacy_tool_names(current)
    return tuple(tool_name for tool_name in upgraded if tool_name not in current_names)


def _upgraded_allowed_tools(existing: dict[str, Any], template: dict[str, Any]) -> list[str] | None:
    current = existing.get("allowed_tools")
    defaults = template.get("allowed_tools")
    if not isinstance(current, list) or not current or not isinstance(defaults, list):
        return None

    denied = {
        str(tool_name).strip()
        for tool_name in existing.get("denied_tools", [])
        if str(tool_name).strip()
    }
    upgraded = _normalize_legacy_tool_names(current)
    for item in defaults:
        tool_name = str(item).strip()
        if tool_name and tool_name not in denied and tool_name not in upgraded:
            upgraded.append(tool_name)

    current_normalized = _normalize_legacy_tool_names(current)
    if upgraded == current_normalized:
        return None
    return upgraded


def _read_top_level_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(_normalize_bare_all_assignments(path.read_text(encoding="utf-8")))
    except tomllib.TOMLDecodeError:
        return {}
    return dict(data)


def _can_parse_toml(content: str) -> bool:
    try:
        tomllib.loads(_normalize_bare_all_assignments(content))
    except tomllib.TOMLDecodeError:
        return False
    return True


def _normalize_bare_all_assignments(content: str) -> str:
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


def _render_toml_assignment(key: str, value: Any) -> list[str]:
    if isinstance(value, dict):
        return _render_toml_table(key, value)
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        lines: list[str] = []
        for entry in value:
            if lines:
                lines.append("")
            lines.append(f"[[{key}]]")
            for child_key, child_value in entry.items():
                if isinstance(child_value, dict):
                    continue
                lines.append(f"{child_key} = {_render_toml_value(child_value)}")
        return lines
    return [f"{key} = {_render_toml_value(value)}"]


def _render_toml_table(name: str, value: dict[str, Any]) -> list[str]:
    lines = [f"[{name}]"]
    for child_key, child_value in value.items():
        if not isinstance(child_value, dict):
            lines.append(f"{child_key} = {_render_toml_value(child_value)}")
    for child_key, child_value in value.items():
        if not isinstance(child_value, dict):
            continue
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(_render_toml_table(f"{name}.{child_key}", child_value))
    return lines


def _migrated_provider_profile_values(existing: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    if "active_model_profile" in existing or "models" not in template:
        return {}
    provider = str(existing.get("provider", "openai-compatible") or "openai-compatible")
    model_name = str(existing.get("model_name", "mistral-medium-latest") or "mistral-medium-latest")
    max_output_tokens = int(existing.get("max_output_tokens", 4096) or 4096)
    providers = dict(template.get("providers", {}))
    provider_payload = dict(providers.get(provider, {}))
    provider_payload["enabled"] = True
    base_url = str(existing.get("api_base_url", "") or "")
    if base_url:
        provider_payload["base_url"] = base_url
    providers[provider] = provider_payload
    return {
        "active_model_profile": "legacy-current",
        "providers": providers,
        "models": {
            "legacy-current": {
                "provider": provider,
                "model_name": model_name,
                "context_length": get_model_context_limit(model_name),
                "max_output_tokens": max_output_tokens,
                "reserved_output_tokens": int(existing.get("reserved_output_tokens", max_output_tokens) or max_output_tokens),
                "temperature": float(existing.get("temperature", 0.0) or 0.0),
                "top_p": float(existing.get("top_p", 1.0) or 1.0),
                "supports_tools": True,
                "supports_streaming": True,
                "supports_reasoning": False,
                "thinking": {"enabled": False, "mode": "provider_default"},
            }
        },
    }


def _needs_agent_scope_migration(existing: dict[str, Any]) -> bool:
    if (set(LEGACY_AGENT_SCOPE_KEYS) | set(OBSOLETE_SCOPE_KEYS)) & set(existing):
        return True
    agents = existing.get("agents")
    if not isinstance(agents, dict):
        return False
    if set(agents) & {key.removeprefix("agent_") for key in OBSOLETE_SCOPE_KEYS}:
        return True
    return _compact_agent_scope(existing) != _agent_scope_for_new_layout(existing)


def _needs_subagent_scope_migration(existing: dict[str, Any]) -> bool:
    if "subagent_profiles" in existing:
        return True
    profiles = _subagent_profiles_from_existing(existing)
    if not profiles:
        return False
    current = _dedupe_exact_subagent_profiles(
        profile
        for profile in (_subagent_profile_for_new_layout(dict(entry), compact_builtins=False) for entry in profiles)
        if profile
    )
    compacted = _migrated_subagent_profiles(existing)
    return current != compacted


def _needs_legacy_subagent_name_migration(existing: dict[str, Any]) -> bool:
    if _needs_agent_scope_migration(existing) or _needs_subagent_scope_migration(existing):
        return True
    allowed_tools = existing.get("allowed_tools")
    if isinstance(allowed_tools, list) and _normalize_legacy_tool_names(allowed_tools) != _string_list(allowed_tools):
        return True
    delegation_subagents = existing.get("delegation_subagents")
    return isinstance(delegation_subagents, list) and _upgraded_delegation_subagents(existing) is not None


def _migrated_agent_scope(existing: dict[str, Any], template_agents: Any) -> dict[str, Any]:
    return _compact_agent_scope(existing)


def _agent_scope_for_new_layout(existing: dict[str, Any]) -> dict[str, Any]:
    migrated: dict[str, Any] = {}
    current = existing.get("agents")
    if isinstance(current, dict):
        key_map = {
            "allowed_tools": "allowed_tools",
            "allowed_skills": "allowed_skills",
            "allowed_mcp_servers": "allowed_mcp_servers",
            "allowed_mcps": "allowed_mcp_servers",
            "add_tools": "add_tools",
            "remove_tools": "remove_tools",
            "add_skills": "add_skills",
            "remove_skills": "remove_skills",
            "add_mcp_servers": "add_mcp_servers",
            "remove_mcp_servers": "remove_mcp_servers",
            "add_mcps": "add_mcp_servers",
            "remove_mcps": "remove_mcp_servers",
        }
        for key, new_key in key_map.items():
            if key not in current:
                continue
            value = current[key]
            migrated[new_key] = _normalize_legacy_tool_names(value) if _is_tool_scope_key(new_key) and isinstance(value, list) else value
    existing_agents = existing.get("agents") if isinstance(existing.get("agents"), dict) else {}
    for old_key, new_key in LEGACY_AGENT_SCOPE_KEYS.items():
        value = existing.get(old_key)
        if old_key not in existing:
            continue
        # Only skip if the existing [agents] table explicitly had this key;
        # template defaults should not block legacy-value migration.
        if new_key in existing_agents:
            continue
        migrated[new_key] = _normalize_legacy_tool_names(value) if new_key == "allowed_tools" and isinstance(value, list) else value
    for old_key, new_key in AGENT_DELTA_SCOPE_KEYS.items():
        value = existing.get(old_key)
        if old_key not in existing:
            continue
        if new_key in existing_agents:
            continue
        migrated[new_key] = _normalize_legacy_tool_names(value) if _is_tool_scope_key(new_key) and isinstance(value, list) else value
    return migrated


def _compact_agent_scope(existing: dict[str, Any]) -> dict[str, Any]:
    current = _agent_scope_for_new_layout(existing)
    compacted: dict[str, Any] = {}
    default_tools = list(SUPERVISOR_DEFAULT_TOOLS)

    allowed_tools = current.get("allowed_tools")
    if isinstance(allowed_tools, list):
        normalized_tools = [
            tool_name
            for tool_name in _normalize_legacy_tool_names(allowed_tools)
            if not tool_name.startswith("subagent_")
        ]
        if _is_all_list(normalized_tools):
            compacted["allowed_tools"] = normalized_tools
        else:
            _extend_delta_fields(
                compacted,
                add_key="add_tools",
                remove_key="remove_tools",
                defaults=default_tools,
                current=normalized_tools,
            )
    _copy_scope_list(current, compacted, "add_tools", normalize_tools=True)
    _copy_scope_list(current, compacted, "remove_tools", normalize_tools=True)

    allowed_skills = current.get("allowed_skills")
    if isinstance(allowed_skills, list):
        if _is_all_list(allowed_skills):
            compacted["allowed_skills"] = _string_list(allowed_skills)
        elif _string_list(allowed_skills):
            compacted["add_skills"] = _string_list(allowed_skills)
    _copy_scope_list(current, compacted, "add_skills")
    _copy_scope_list(current, compacted, "remove_skills")

    allowed_mcp_servers = current.get("allowed_mcp_servers")
    if isinstance(allowed_mcp_servers, list):
        normalized_mcp = _string_list(allowed_mcp_servers)
        if _is_all_list(normalized_mcp):
            # Active MCP servers are already in the code default for advanced mode.
            pass
        elif normalized_mcp:
            # Narrowing an open-ended active MCP set is an explicit replacement.
            compacted["allowed_mcp_servers"] = normalized_mcp
    _copy_scope_list(current, compacted, "add_mcp_servers")
    _copy_scope_list(current, compacted, "remove_mcp_servers")

    return compacted


def _migrated_subagent_profiles(existing: dict[str, Any]) -> list[dict[str, Any]]:
    profiles = _subagent_profiles_from_existing(existing)
    migrated = [
        profile
        for entry in profiles
        if isinstance(entry, dict)
        for profile in (_subagent_profile_for_new_layout(dict(entry), compact_builtins=True),)
        if profile
    ]
    return _dedupe_exact_subagent_profiles(migrated)


def _subagent_profile_for_new_layout(entry: dict[str, Any], *, compact_builtins: bool) -> dict[str, Any]:
    migrated = _normalize_legacy_subagent_profile(dict(entry))
    if "allowed_mcp_servers" in migrated and "allowed_mcps" not in migrated:
        migrated["allowed_mcps"] = migrated.pop("allowed_mcp_servers")
    if "add_mcp_servers" in migrated and "add_mcps" not in migrated:
        migrated["add_mcps"] = migrated.pop("add_mcp_servers")
    if "remove_mcp_servers" in migrated and "remove_mcps" not in migrated:
        migrated["remove_mcps"] = migrated.pop("remove_mcp_servers")
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
        migrated.pop(obsolete, None)
    if not compact_builtins:
        return _drop_empty_scope_values(migrated)

    name = str(migrated.get("name", "")).strip()
    defaults = _builtin_subagent_scope_defaults(name)
    if defaults is None:
        return _drop_empty_scope_values(migrated)

    scope_keys = {
        "allowed_tools",
        "allowed_skills",
        "allowed_mcps",
        "add_tools",
        "remove_tools",
        "add_skills",
        "remove_skills",
        "add_mcps",
        "remove_mcps",
    }
    had_scope = any(key in migrated for key in scope_keys)
    compacted: dict[str, Any] = {"name": name}

    allowed_tools = migrated.get("allowed_tools")
    if isinstance(allowed_tools, list):
        normalized_tools = _normalize_legacy_tool_names(allowed_tools)
        if _is_all_list(normalized_tools):
            compacted["allowed_tools"] = normalized_tools
        else:
            _extend_delta_fields(
                compacted,
                add_key="add_tools",
                remove_key="remove_tools",
                defaults=defaults["allowed_tools"],
                current=normalized_tools,
            )
    _copy_scope_list(migrated, compacted, "add_tools", normalize_tools=True)
    _copy_scope_list(migrated, compacted, "remove_tools", normalize_tools=True)

    allowed_skills = migrated.get("allowed_skills")
    if isinstance(allowed_skills, list):
        normalized_skills = _string_list(allowed_skills)
        if _is_all_list(normalized_skills):
            compacted["allowed_skills"] = normalized_skills
        else:
            _extend_delta_fields(
                compacted,
                add_key="add_skills",
                remove_key="remove_skills",
                defaults=defaults["allowed_skills"],
                current=normalized_skills,
            )
    _copy_scope_list(migrated, compacted, "add_skills")
    _copy_scope_list(migrated, compacted, "remove_skills")

    allowed_mcps = migrated.get("allowed_mcps")
    if isinstance(allowed_mcps, list):
        normalized_mcps = _string_list(allowed_mcps)
        if _is_all_list(normalized_mcps):
            compacted["allowed_mcps"] = normalized_mcps
        else:
            _extend_delta_fields(
                compacted,
                add_key="add_mcps",
                remove_key="remove_mcps",
                defaults=defaults["allowed_mcps"],
                current=normalized_mcps,
            )
    _copy_scope_list(migrated, compacted, "add_mcps")
    _copy_scope_list(migrated, compacted, "remove_mcps")

    if len(compacted) == 1 and had_scope:
        return {}
    return _drop_empty_scope_values(compacted)


def _subagent_profiles_from_existing(existing: dict[str, Any]) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    legacy_profiles = existing.get("subagent_profiles")
    if isinstance(legacy_profiles, list):
        profiles.extend(dict(entry) for entry in legacy_profiles if isinstance(entry, dict))
    raw_new_profiles = existing.get("sub-agents")
    if isinstance(raw_new_profiles, list):
        profiles.extend(dict(entry) for entry in raw_new_profiles if isinstance(entry, dict))
    elif isinstance(raw_new_profiles, dict):
        if "name" in raw_new_profiles:
            profiles.append(dict(raw_new_profiles))
        for name, value in raw_new_profiles.items():
            if not isinstance(value, dict):
                continue
            entry = dict(value)
            entry.setdefault("name", str(name))
            profiles.append(entry)
    return profiles


def _subagent_profiles_need_name_migration(profiles: list[dict[str, Any]]) -> bool:
    for profile in profiles:
        name = str(profile.get("name", "")).strip()
        if name and normalize_legacy_subagent_name(name) != name.replace("-", "_"):
            return True
        allowed_tools = profile.get("allowed_tools")
        if isinstance(allowed_tools, list) and _normalize_legacy_tool_names(allowed_tools) != _string_list(allowed_tools):
            return True
        if "allowed_mcp_servers" in profile:
            return True
    return False


def _has_exact_duplicate_subagent_profiles(profiles: list[dict[str, Any]]) -> bool:
    return len(_dedupe_exact_subagent_profiles(profiles)) != len(profiles)


def _dedupe_exact_subagent_profiles(profiles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    for profile in profiles:
        if profile not in unique:
            unique.append(profile)
    return unique


def _upgraded_delegation_subagents(existing: dict[str, Any]) -> list[dict[str, Any]] | None:
    delegation_subagents = existing.get("delegation_subagents")
    if not isinstance(delegation_subagents, list):
        return None
    migrated = [
        _normalize_legacy_delegation_subagent(dict(entry))
        for entry in delegation_subagents
        if isinstance(entry, dict)
    ]
    return migrated if migrated != [dict(entry) for entry in delegation_subagents if isinstance(entry, dict)] else None


def _string_list(values: list[Any]) -> list[str]:
    return [str(item).strip() for item in values if str(item).strip()]


def _is_all_list(values: list[Any]) -> bool:
    return any(str(item).strip().lower() == "all" for item in values)


def _is_tool_scope_key(key: str) -> bool:
    return key in {"allowed_tools", "add_tools", "remove_tools"}


def _extend_delta_fields(
    target: dict[str, Any],
    *,
    add_key: str,
    remove_key: str,
    defaults: list[str],
    current: list[str],
) -> None:
    default_values = _string_list(defaults)
    current_values = _string_list(current)
    additions = [item for item in current_values if item not in default_values]
    removals = [item for item in default_values if item not in current_values]
    if additions:
        target[add_key] = _ordered_unique_scope([*target.get(add_key, []), *additions])
    if removals:
        target[remove_key] = _ordered_unique_scope([*target.get(remove_key, []), *removals])


def _copy_scope_list(
    source: dict[str, Any],
    target: dict[str, Any],
    key: str,
    *,
    normalize_tools: bool = False,
) -> None:
    value = source.get(key)
    if not isinstance(value, list):
        return
    values = _normalize_legacy_tool_names(value) if normalize_tools else _string_list(value)
    if values:
        target[key] = _ordered_unique_scope([*target.get(key, []), *values])


def _ordered_unique_scope(values: list[Any]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in unique:
            unique.append(text)
    return unique


def _drop_empty_scope_values(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in entry.items()
        if not (isinstance(value, list) and not value)
    }


def _builtin_subagent_scope_defaults(name: str) -> dict[str, list[str]] | None:
    normalized_name = normalize_legacy_subagent_name(name)
    for spec in BUILTIN_SUBAGENT_SPECS:
        if spec.name == normalized_name:
            return {
                "allowed_tools": list(spec.allowed_tools),
                "allowed_skills": [],
                "allowed_mcps": [],
            }
    return None


def _is_non_empty_scope_value(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    return value not in (None, "")


def _remove_deprecated_and_version_lines(lines: list[str]) -> list[str]:
    remove_keys = {*DEPRECATED_CONFIG_KEYS, "config_version"}
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == UPGRADE_MARKER:
            continue
        if any(stripped.startswith(f"{key} ") or stripped.startswith(f"{key}=") for key in remove_keys):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


def _insert_before_first_table(lines: list[str], additions: list[str]) -> list[str]:
    if not additions:
        return lines

    insertion_index = next(
        (index for index, line in enumerate(lines) if _is_table_header(line.strip())),
        len(lines),
    )
    before = list(lines[:insertion_index])
    after = list(lines[insertion_index:])

    merged = before
    if merged and merged[-1].strip():
        merged.append("")
    merged.extend(additions)
    if after:
        if merged and merged[-1].strip():
            merged.append("")
        merged.extend(after)
    return merged


def _is_table_header(line: str) -> bool:
    return line.startswith("[") and line.endswith("]")


def _remove_top_level_assignments(lines: list[str], keys: set[str] | dict[str, Any]) -> list[str]:
    updated = lines
    for key in keys:
        updated = _remove_top_level_assignment(updated, key)
    return updated


def _remove_top_level_assignment(lines: list[str], key: str) -> list[str]:
    kept: list[str] = []
    skipping_multiline = False
    bracket_depth = 0

    for line in lines:
        if skipping_multiline:
            bracket_depth += line.count("[") - line.count("]")
            if bracket_depth <= 0:
                skipping_multiline = False
            continue
        stripped = line.strip()
        is_assignment = (
            stripped
            and not stripped.startswith("#")
            and (stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="))
        )
        if is_assignment:
            bracket_depth = line.count("[") - line.count("]")
            if bracket_depth > 0:
                skipping_multiline = True
            continue
        kept.append(line)
    return kept


def _remove_table(lines: list[str], table_name: str) -> list[str]:
    return _remove_toml_block(lines, header=f"[{table_name}]", array_header_prefix="[[")


def _remove_array_table(lines: list[str], table_name: str) -> list[str]:
    return _remove_toml_block(lines, header=f"[[{table_name}]]", array_header_prefix=None)


def _remove_toml_block(lines: list[str], *, header: str, array_header_prefix: str | None) -> list[str]:
    kept: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if skipping:
            is_table_header = stripped.startswith("[") and stripped.endswith("]")
            if is_table_header:
                skipping = stripped == header
                if skipping:
                    continue
                kept.append(line)
            continue
        if stripped == header:
            skipping = True
            continue
        if array_header_prefix is not None and stripped.startswith(array_header_prefix):
            kept.append(line)
            continue
        kept.append(line)
    return kept


def _replace_top_level_assignment(lines: list[str], key: str, rendered_value: str) -> list[str]:
    kept: list[str] = []
    replaced = False
    skipping_multiline = False
    bracket_depth = 0

    for line in lines:
        if skipping_multiline:
            bracket_depth += line.count("[") - line.count("]")
            if bracket_depth <= 0:
                skipping_multiline = False
            continue

        stripped = line.strip()
        is_assignment = (
            stripped
            and not stripped.startswith("#")
            and (stripped.startswith(f"{key} ") or stripped.startswith(f"{key}="))
        )
        if is_assignment:
            if not replaced:
                kept.append(f"{key} = {rendered_value}")
                replaced = True
            bracket_depth = line.count("[") - line.count("]")
            if bracket_depth > 0:
                skipping_multiline = True
            continue

        kept.append(line)

    return kept


def _render_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{key} = {_render_toml_value(item)}"
            for key, item in value.items()
        ) + " }"
    return json.dumps(value)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
