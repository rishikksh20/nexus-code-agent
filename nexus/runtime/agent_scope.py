from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nexus.tools.base import ToolRegistry


SUPERVISOR_SCOPE_FIELDS: tuple[str, ...] = (
    "agent_allowed_tools",
    "agent_allowed_skills",
    "agent_allowed_mcp_servers",
)

SUBAGENT_PROFILE_FIELDS: tuple[str, ...] = (
    "allowed_tools",
    "allowed_skills",
    "allowed_mcps",
    "allowed_mcp_servers",
)

BUILTIN_SUBAGENT_NAMES: frozenset[str] = frozenset(
    {
        "planning_analysis",
        "execution",
        "review",
        "verification",
    }
)


ALL_SCOPE_SENTINEL = "all"


def clean_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def is_all_scope(value: object) -> bool:
    return any(item.lower() == ALL_SCOPE_SENTINEL for item in clean_string_list(value))


def explicit_scope_names(value: object) -> list[str]:
    return [item for item in clean_string_list(value) if item.lower() != ALL_SCOPE_SENTINEL]


def normalize_subagent_name(name: str) -> str:
    normalized = str(name).strip()
    if normalized.startswith("subagent_"):
        normalized = normalized[len("subagent_") :]
    if normalized.startswith("subagent-"):
        normalized = normalized[len("subagent-") :]
    return normalized.replace("-", "_")


def subagent_tool_name(name: str) -> str:
    normalized = normalize_subagent_name(name)
    return normalized if normalized.startswith("subagent_") else f"subagent_{normalized}"


def subagent_profile(config: Any, name: str) -> dict[str, Any]:
    normalized = normalize_subagent_name(name)
    for entry in getattr(config, "subagent_profiles", []) or []:
        if not isinstance(entry, dict):
            continue
        if normalize_subagent_name(str(entry.get("name", ""))) == normalized:
            return entry
    return {}


def configured_subagent_names(config: Any) -> set[str]:
    names: set[str] = set()
    for entry in getattr(config, "subagent_profiles", []) or []:
        if not isinstance(entry, dict):
            continue
        name = normalize_subagent_name(str(entry.get("name", "")))
        if name:
            names.add(name)
    return names


def mcp_tool_names_for_servers(registry: ToolRegistry, server_names: Iterable[str]) -> set[str]:
    servers = {str(name).strip() for name in server_names if str(name).strip()}
    if not servers:
        return set()
    return {
        record.name
        for record in registry.records()
        if record.source == "mcp" and record.origin in servers
    }


def all_mcp_tool_names(registry: ToolRegistry) -> set[str]:
    return {record.name for record in registry.records() if record.source == "mcp"}


def supervisor_tool_names(config: Any, registry: ToolRegistry) -> set[str]:
    records = registry.records()
    all_names = {record.name for record in records}
    subagent_names = {record.name for record in records if record.name.startswith("subagent_")}
    direct_normal_names = {
        record.name
        for record in records
        if record.source != "mcp" and record.name not in subagent_names and record.name != "delegate_task"
    }
    configured_tool_scope = getattr(config, "agent_allowed_tools", [])
    configured_mcp_scope = getattr(config, "agent_allowed_mcp_servers", [])
    configured_tools = set(explicit_scope_names(configured_tool_scope))
    configured_mcp = set(explicit_scope_names(configured_mcp_scope))
    all_configured_tools = is_all_scope(configured_tool_scope)
    all_configured_mcp = is_all_scope(configured_mcp_scope)

    if configured_tools or configured_mcp or all_configured_tools or all_configured_mcp:
        allowed = set(direct_normal_names) if all_configured_tools else configured_tools & direct_normal_names
        allowed |= all_mcp_tool_names(registry) if all_configured_mcp else mcp_tool_names_for_servers(registry, configured_mcp)
    elif str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced":
        allowed = set()
    else:
        allowed = set(all_names)
    if str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced":
        allowed |= subagent_names
    return allowed


def supervisor_skill_names(config: Any, active_skills: Iterable[str]) -> list[str]:
    active = _ordered_unique(active_skills)
    active_set = set(active)
    configured_scope = getattr(config, "agent_allowed_skills", [])
    configured = explicit_scope_names(configured_scope)
    if is_all_scope(configured_scope):
        base = active
    elif configured:
        base = [name for name in configured if name in active_set]
    elif str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced":
        base = []
    else:
        base = active
    return _ordered_unique(base)


def subagent_skill_names(
    config: Any,
    name: str,
    active_skills: Iterable[str],
    *,
    base_allowed_skills: Iterable[str] | None | object = (),
) -> list[str]:
    profile = subagent_profile(config, name)
    active = _ordered_unique(active_skills)
    active_set = set(active)
    configured_scope = profile.get("allowed_skills", [])
    configured = [name for name in explicit_scope_names(configured_scope) if name in active_set]
    if is_all_scope(configured_scope):
        configured = active
    elif not configured:
        if base_allowed_skills is None:
            configured = active
        elif base_allowed_skills != ():
            configured = [name for name in clean_string_list(list(base_allowed_skills)) if name in active_set]
    return _ordered_unique(configured)


def subagent_tool_names(
    config: Any,
    registry: ToolRegistry,
    name: str,
    *,
    base_allowed_tools: Iterable[str] | None,
    base_allowed_mcps: Iterable[str] | None | object = (),
) -> set[str]:
    profile = subagent_profile(config, name)
    normal_candidate_names = {
        record.name
        for record in registry.records()
        if record.source != "mcp" and not record.name.startswith("subagent_") and record.name != "delegate_task"
    }

    configured_tool_scope = profile.get("allowed_tools", [])
    configured_mcp_scope = profile.get("allowed_mcps", []) or profile.get("allowed_mcp_servers", [])
    configured_tools = explicit_scope_names(configured_tool_scope)
    configured_mcp = explicit_scope_names(configured_mcp_scope)

    if is_all_scope(configured_tool_scope):
        allowed = set(normal_candidate_names)
    elif configured_tools:
        allowed = set(configured_tools) & normal_candidate_names
    elif base_allowed_tools is None:
        allowed = set(normal_candidate_names)
    else:
        allowed = set(clean_string_list(list(base_allowed_tools))) & normal_candidate_names

    normalized_name = normalize_subagent_name(name)
    if is_all_scope(configured_mcp_scope):
        allowed |= all_mcp_tool_names(registry)
    elif configured_mcp:
        allowed |= mcp_tool_names_for_servers(registry, configured_mcp)
    elif base_allowed_mcps is None or normalized_name in BUILTIN_SUBAGENT_NAMES:
        allowed |= all_mcp_tool_names(registry)
    elif base_allowed_mcps != ():
        allowed |= mcp_tool_names_for_servers(registry, clean_string_list(list(base_allowed_mcps)))

    return allowed


def skill_metadata_catalog(skill_registry: Any) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for skill in skill_registry.all():
        catalog[skill.name] = {
            "name": skill.name,
            "description": skill.description,
            "source": skill.source,
            "path": str(skill.skill_path) if skill.skill_path else "",
            "license": skill.license or "",
            "compatibility": skill.compatibility or "",
            "allowed_tools": list(skill.allowed_tools),
        }
    return catalog


def render_skill_metadata(catalog: dict[str, dict[str, Any]], skill_names: Iterable[str]) -> tuple[str, ...]:
    lines: list[str] = []
    for skill_name in _ordered_unique(skill_names):
        payload = catalog.get(skill_name)
        if not payload:
            continue
        parts = [
            f"name={payload['name']}",
            f"description={payload['description']}",
            f"source={payload['source']}",
            "active=yes",
        ]
        if payload.get("path"):
            parts.append(f"path={payload['path']}")
        if payload.get("license"):
            parts.append(f"license={payload['license']}")
        if payload.get("compatibility"):
            parts.append(f"compatibility={payload['compatibility']}")
        allowed_tools = payload.get("allowed_tools") or []
        if allowed_tools:
            parts.append("allowed-tools=" + " ".join(str(tool) for tool in allowed_tools))
        lines.append("- " + "; ".join(parts))
    return tuple(lines)


def _ordered_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result
