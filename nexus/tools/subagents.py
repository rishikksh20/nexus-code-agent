"""Cognitive sub-agent tool helpers exposed from the tools package."""
from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.sandbox.agent_tool import SubAgentTool, SubagentDefinition
from nexus.runtime.agent_scope import (
    BUILTIN_SUBAGENT_SPECS,
    configured_subagent_names,
    normalize_subagent_name,
)
from nexus.tools.registry import tool_enabled

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nexus.skills import SkillRegistry
    from nexus.tools.base import ToolRegistry


def register_subagent_tools(
    registry: "ToolRegistry",
    config,
    *,
    model_client_factory=None,
    definitions: "Iterable[SubagentDefinition]" = (),
) -> int:
    """Register cognitive sub-agent tools.

    Order of precedence for definitions (later wins on name collision):
    1. Built-in cognitive personas from ``nexus.runtime.agent_scope``
    2. Config-declared ``delegation_subagents``
    3. YAML files from ``~/.nexus/agents/`` and ``.nexus/agents/``
    """
    configured_names = configured_subagent_names(config)
    advanced_mode = str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced"
    if not advanced_mode and not configured_names:
        return 0

    # Load YAML-discovered definitions and merge with explicit ones.
    yaml_definitions = _load_yaml_definitions(config)
    yaml_names = {definition.name for definition in yaml_definitions}
    merged_definitions = [
        definition
        for definition in _merge_definitions_ordered(definitions, ())
        if definition.name not in yaml_names
    ]

    count = 0
    for definition in merged_definitions:
        if not advanced_mode and normalize_subagent_name(definition.name) not in configured_names:
            continue
        tool = SubAgentTool(
            definition=definition,
            model_client_factory=model_client_factory,
            base_tool_registry=registry,
            config=config,
        )
        if tool_enabled(config, tool.name):
            registry.register(tool, source="agent", origin=definition.name)
            count += 1
    count += _register_yaml_definitions(
        registry,
        config,
        yaml_definitions,
        model_client_factory=model_client_factory,
    )
    return count


def load_subagent_definitions_from_skills(skill_registry: "SkillRegistry") -> list[SubagentDefinition]:
    """Build specialist subagent definitions from loaded skills.

    Any skill whose name starts with ``subagent-`` or ``subagent_`` is treated
    as a cognitive sub-agent definition.
    """
    definitions: list[SubagentDefinition] = []
    for skill in skill_registry.all():
        normalized_name = _skill_subagent_name(skill.name)
        if normalized_name is None:
            continue
        definitions.append(
            SubagentDefinition(
                name=normalized_name,
                description=skill.description,
                goal_prompt=skill.content.strip(),
                allowed_skills=[],
            )
        )
    return definitions


def register_skill_subagent_tools(
    registry: "ToolRegistry",
    config,
    skill_registry: "SkillRegistry",
    *,
    model_client_factory=None,
) -> int:
    """Register cognitive sub-agent tools discovered from loaded skills."""
    configured_names = configured_subagent_names(config)
    advanced_mode = str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced"
    if not advanced_mode and not configured_names:
        return 0

    count = 0
    existing_names = {record.name for record in registry.records()}
    for definition in load_subagent_definitions_from_skills(skill_registry):
        if not advanced_mode and normalize_subagent_name(definition.name) not in configured_names:
            continue
        tool = SubAgentTool(
            definition=definition,
            model_client_factory=model_client_factory,
            base_tool_registry=registry,
            config=config,
        )
        if tool.name in existing_names:
            continue
        if tool_enabled(config, tool.name):
            registry.register(tool, source="agent-skill", origin=definition.name)
            existing_names.add(tool.name)
            count += 1
    return count


def register_yaml_subagent_tools(
    registry: "ToolRegistry",
    config,
    *,
    model_client_factory=None,
    replace_existing: bool = False,
) -> int:
    """Register sub-agent tools discovered from YAML files in agent directories.

    Intended for live reloads triggered by ``/sub-agent agents reload``.
    Already-registered tool names are skipped to avoid duplicates.
    """
    from nexus.agents.loader import load_yaml_subagent_definitions

    configured_names = configured_subagent_names(config)
    advanced_mode = str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced"
    if not advanced_mode and not configured_names:
        return 0

    return _register_yaml_definitions(
        registry,
        config,
        load_yaml_subagent_definitions(config),
        model_client_factory=model_client_factory,
        replace_existing=replace_existing,
    )


def _register_yaml_definitions(
    registry: "ToolRegistry",
    config,
    definitions: "Iterable[SubagentDefinition]",
    *,
    model_client_factory=None,
    replace_existing: bool = False,
) -> int:
    configured_names = configured_subagent_names(config)
    advanced_mode = str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced"
    if not advanced_mode and not configured_names:
        return 0

    existing_names = {record.name for record in registry.records()}
    count = 0
    for definition in definitions:
        if not advanced_mode and normalize_subagent_name(definition.name) not in configured_names:
            continue
        tool = SubAgentTool(
            definition=definition,
            model_client_factory=model_client_factory,
            base_tool_registry=registry,
            config=config,
        )
        if tool.name in existing_names:
            if not replace_existing:
                continue
            registry.unregister(tool.name)
            existing_names.discard(tool.name)
        if tool_enabled(config, tool.name):
            registry.register(tool, source="agent-yaml", origin=definition.name)
            existing_names.add(tool.name)
            count += 1
    return count


def load_subagent_definitions(config) -> list[SubagentDefinition]:
    """Build subagent definitions from ``config.delegation_subagents``."""
    definitions: list[SubagentDefinition] = []
    for entry in getattr(config, "delegation_subagents", ()) or ():
        definitions.append(
            SubagentDefinition(
                name=str(entry["name"]).strip(),
                description=str(entry["description"]).strip(),
                goal_prompt=str(entry["goal_prompt"]).strip(),
                allowed_tools=[str(tool_name).strip() for tool_name in entry.get("allowed_tools") or []] or None,
                allowed_skills=[str(skill_name).strip() for skill_name in entry.get("allowed_skills") or []] or None,
                allowed_mcps=[str(server_name).strip() for server_name in entry.get("allowed_mcps") or entry.get("allowed_mcp_servers") or []] or None,
                max_turns=int(entry.get("max_turns", 20)),
                timeout_seconds=float(entry.get("timeout_seconds", 600.0)),
            )
        )
    return definitions


def get_builtin_subagent_definitions() -> list[SubagentDefinition]:
    """Return conservative built-in cognitive specialist personas."""
    return [
        SubagentDefinition(
            name=spec.name,
            description=spec.description,
            goal_prompt=spec.goal_prompt,
            allowed_tools=list(spec.allowed_tools),
            allowed_mcps=[],
            max_turns=spec.max_turns,
            timeout_seconds=spec.timeout_seconds,
        )
        for spec in sorted(BUILTIN_SUBAGENT_SPECS, key=lambda item: item.priority)
    ]


def _merge_builtin_definitions(definitions: "Iterable[SubagentDefinition]") -> list[SubagentDefinition]:
    merged = list(definitions)
    existing = {definition.name for definition in merged}
    for definition in get_builtin_subagent_definitions():
        if definition.name not in existing:
            merged.append(definition)
            existing.add(definition.name)
    return merged


def _merge_definitions_ordered(
    explicit: "Iterable[SubagentDefinition]",
    yaml_defs: list[SubagentDefinition],
) -> list[SubagentDefinition]:
    """Merge builtin, explicit config, and YAML definitions.

    Resolution order (last wins on name collision):
    1. Built-in cognitive personas
    2. Explicit ``delegation_subagents`` from config
    3. YAML-discovered agents (local overrides global inside loader)
    """
    result: dict[str, SubagentDefinition] = {}
    for defn in get_builtin_subagent_definitions():
        result[defn.name] = defn
    for defn in explicit:
        result[defn.name] = defn
    for defn in yaml_defs:
        result[defn.name] = defn
    return list(result.values())


def _load_yaml_definitions(config) -> list[SubagentDefinition]:
    """Load YAML sub-agent definitions; returns empty list on any error."""
    try:
        from nexus.agents.loader import load_yaml_subagent_definitions
        return load_yaml_subagent_definitions(config)
    except Exception:  # noqa: BLE001
        return []


def _skill_subagent_name(skill_name: str) -> str | None:
    prefixes = ("subagent-", "subagent_")
    for prefix in prefixes:
        if skill_name.startswith(prefix):
            suffix = skill_name[len(prefix):].strip().replace("-", "_")
            return suffix or None
    return None

__all__ = [
    "SubAgentTool",
    "SubagentDefinition",
    "load_subagent_definitions",
    "load_subagent_definitions_from_skills",
    "register_skill_subagent_tools",
    "register_subagent_tools",
    "register_yaml_subagent_tools",
    "get_builtin_subagent_definitions",
]
