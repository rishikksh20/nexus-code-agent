from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CURRENT_CONFIG_VERSION = 2
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
    "subagent_research": ("subagent_planning_analysis",),
    "subagent_test": ("subagent_verification",),
}
LEGACY_AGENT_SCOPE_KEYS: dict[str, str] = {
    "agent_allowed_tools": "allowed_tools",
    "agent_attached_tools": "attached_tools",
    "agent_detached_tools": "detached_tools",
    "agent_allowed_skills": "allowed_skills",
    "agent_attached_skills": "attached_skills",
    "agent_detached_skills": "detached_skills",
    "agent_allowed_mcp_servers": "allowed_mcp_servers",
    "agent_attached_mcp_servers": "attached_mcp_servers",
    "agent_detached_mcp_servers": "detached_mcp_servers",
}


@dataclass(slots=True, frozen=True)
class ConfigUpgradeReport:
    path: Path
    missing_keys: tuple[str, ...] = ()
    deprecated_keys: tuple[str, ...] = ()
    allowed_tool_additions: tuple[str, ...] = ()
    allowed_tools_updated: bool = False
    agent_scope_migrated: bool = False
    subagent_scope_migrated: bool = False
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
    version = _optional_int(existing.get("config_version"))
    return ConfigUpgradeReport(
        path=path,
        missing_keys=missing,
        deprecated_keys=deprecated,
        allowed_tool_additions=allowed_tool_additions,
        allowed_tools_updated=upgraded_allowed_tools is not None,
        agent_scope_migrated=agent_scope_migrated,
        subagent_scope_migrated=subagent_scope_migrated,
        current_version=version,
    )


def upgrade_config_file(path: Path, template_str: str) -> ConfigUpgradeReport:
    before = inspect_config_upgrade(path, template_str)
    template = tomllib.loads(template_str)
    existing = _read_top_level_toml(path)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = _remove_deprecated_and_version_lines(content.splitlines())
    if _needs_agent_scope_migration(existing):
        lines = _remove_top_level_assignments(lines, LEGACY_AGENT_SCOPE_KEYS)
        lines = _remove_table(lines, "agents")
    if _needs_subagent_scope_migration(existing):
        lines = _remove_top_level_assignments(lines, {"subagent_profiles"})
        if "sub-agents" not in existing:
            lines = _remove_array_table(lines, "sub-agents")
    upgraded_allowed_tools = _upgraded_allowed_tools(existing, template)
    if upgraded_allowed_tools is not None:
        lines = _replace_top_level_assignment(
            lines,
            "allowed_tools",
            _render_toml_value(upgraded_allowed_tools),
        )

    additions: list[str] = []
    if lines and lines[-1].strip():
        additions.append("")
    additions.append("# Added by Nexus config upgrade")
    additions.append(f"config_version = {CURRENT_CONFIG_VERSION}")
    migrated_subagents = _migrated_subagent_profiles(existing)
    for key in template:
        if key == "config_version" or key in existing:
            continue
        if key == "sub-agents" and migrated_subagents:
            continue
        value = _migrated_agent_scope(existing, template[key]) if key == "agents" else template[key]
        additions.extend(_render_toml_assignment(key, value))
    if _needs_agent_scope_migration(existing) and "agents" in existing:
        additions.extend(_render_toml_assignment("agents", _migrated_agent_scope(existing, template.get("agents", {}))))
    if migrated_subagents:
        if additions and additions[-1].strip():
            additions.append("")
        additions.extend(_render_toml_assignment("sub-agents", migrated_subagents))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join((*lines, *additions)).rstrip() + "\n", encoding="utf-8")
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
    allowed_tools = normalized.get("allowed_tools")
    if isinstance(allowed_tools, list):
        normalized["allowed_tools"] = _normalize_legacy_tool_names(allowed_tools)
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
    if "subagent_planning_analysis" in normalized and "subagent_execution" not in normalized:
        normalized.append("subagent_execution")
    return normalized


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
        lines = [f"[{key}]"]
        for child_key, child_value in value.items():
            if isinstance(child_value, dict):
                continue
            lines.append(f"{child_key} = {_render_toml_value(child_value)}")
        return lines
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


def _needs_agent_scope_migration(existing: dict[str, Any]) -> bool:
    return bool(set(LEGACY_AGENT_SCOPE_KEYS) & set(existing))


def _needs_subagent_scope_migration(existing: dict[str, Any]) -> bool:
    return "subagent_profiles" in existing


def _migrated_agent_scope(existing: dict[str, Any], template_agents: Any) -> dict[str, Any]:
    if isinstance(template_agents, dict):
        migrated = dict(template_agents)
    else:
        migrated = {}
    current = existing.get("agents")
    if isinstance(current, dict):
        migrated.update(current)
    for old_key, new_key in LEGACY_AGENT_SCOPE_KEYS.items():
        value = existing.get(old_key)
        if old_key not in existing:
            continue
        if _is_non_empty_scope_value(migrated.get(new_key)):
            continue
        migrated[new_key] = value
    return migrated


def _migrated_subagent_profiles(existing: dict[str, Any]) -> list[dict[str, Any]]:
    if "sub-agents" in existing:
        return []
    profiles = existing.get("subagent_profiles")
    if not isinstance(profiles, list):
        return []
    return [_subagent_profile_for_new_layout(dict(entry)) for entry in profiles if isinstance(entry, dict)]


def _subagent_profile_for_new_layout(entry: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(entry)
    if "allowed_mcp_servers" in migrated and "allowed_mcps" not in migrated:
        migrated["allowed_mcps"] = migrated.pop("allowed_mcp_servers")
    if "attached_mcp_servers" in migrated and "attached_mcps" not in migrated:
        migrated["attached_mcps"] = migrated.pop("attached_mcp_servers")
    if "detached_mcp_servers" in migrated and "detached_mcps" not in migrated:
        migrated["detached_mcps"] = migrated.pop("detached_mcp_servers")
    return migrated


def _is_non_empty_scope_value(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    return value not in (None, "")


def _remove_deprecated_and_version_lines(lines: list[str]) -> list[str]:
    remove_keys = {*DEPRECATED_CONFIG_KEYS, "config_version"}
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(f"{key} ") or stripped.startswith(f"{key}=") for key in remove_keys):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


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
    return json.dumps(value)


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
