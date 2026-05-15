"""Subagent tool helpers exposed from the tools package.

This keeps delegation-oriented tool wiring under ``nexus.tools`` so the app can
register specialist worker tools from the same package surface as the rest of
the coding-agent toolchain.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.sandbox.agent_tool import SubAgentTool, SubagentDefinition
from nexus.tools.registry import tool_enabled

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nexus.runtime.delegation import DelegationRuntime
    from nexus.skills import SkillRegistry
    from nexus.tools.base import ToolRegistry


def register_subagent_tools(
    registry: "ToolRegistry",
    delegation: "DelegationRuntime | None",
    config,
    *,
    definitions: "Iterable[SubagentDefinition]" = (),
) -> int:
    """Register the default delegation tool and optional specialist workers."""
    if delegation is None:
        return 0
    if not getattr(config, "delegation_enabled", False):
        return 0

    count = 0
    default_tool = SubAgentTool(delegation)
    if tool_enabled(config, default_tool.name):
        registry.register(default_tool, source="agent")
        count += 1
    for definition in _merge_builtin_definitions(definitions):
        tool = SubAgentTool(delegation, definition)
        if tool_enabled(config, tool.name):
            registry.register(tool, source="agent", origin=definition.name)
            count += 1
    return count


def load_subagent_definitions_from_skills(skill_registry: "SkillRegistry") -> list[SubagentDefinition]:
    """Build specialist subagent definitions from loaded skills.

    Any skill whose name starts with ``subagent-`` or ``subagent_`` is treated
    as a specialist worker definition.
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
            )
        )
    return definitions


def register_skill_subagent_tools(
    registry: "ToolRegistry",
    delegation: "DelegationRuntime | None",
    config,
    skill_registry: "SkillRegistry",
) -> int:
    """Register specialist worker tools discovered from loaded skills."""
    if delegation is None:
        return 0
    if not getattr(config, "delegation_enabled", False):
        return 0

    count = 0
    existing_names = {record.name for record in registry.records()}
    for definition in load_subagent_definitions_from_skills(skill_registry):
        tool = SubAgentTool(delegation, definition)
        if tool.name in existing_names:
            continue
        if tool_enabled(config, tool.name):
            registry.register(tool, source="agent-skill", origin=definition.name)
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
                max_turns=int(entry.get("max_turns", 20)),
                timeout_seconds=float(entry.get("timeout_seconds", 600.0)),
            )
        )
    return definitions


def get_builtin_subagent_definitions() -> list[SubagentDefinition]:
    """Return conservative built-in specialist personas for multi-agent flows."""
    return [
        SubagentDefinition(
            name="research",
            description="Investigate repo structure and summarize findings without modifying files.",
            goal_prompt=(
                "You are a read-only Nexus research agent. Investigate the requested codebase slice, "
                "trace relevant files and symbols, and return a compressed summary with related paths. "
                "Do not modify files."
            ),
            allowed_tools=["read_file", "glob", "grep", "list_dir", "lsp"],
            max_turns=12,
            timeout_seconds=300,
        ),
        SubagentDefinition(
            name="review",
            description="Review code changes for bugs, regressions, and maintainability risks.",
            goal_prompt=(
                "You are a senior Nexus code reviewer. Inspect diffs and targeted source files, "
                "prioritize concrete bugs and regressions, and return findings with file references. "
                "Do not modify files."
            ),
            allowed_tools=["git_diff", "read_file", "grep", "lsp"],
            max_turns=10,
            timeout_seconds=300,
        ),
        SubagentDefinition(
            name="test",
            description="Run structured verification and summarize failures.",
            goal_prompt=(
                "You are a Nexus verification agent. Run focused tests, lint, type/syntax checks, "
                "and git status inspection. Return a concise validation summary and failures. "
                "Do not modify files."
            ),
            allowed_tools=["run_tests", "run_linter", "run_typecheck", "git_status"],
            max_turns=8,
            timeout_seconds=600,
        ),
    ]


def _merge_builtin_definitions(definitions: "Iterable[SubagentDefinition]") -> list[SubagentDefinition]:
    merged = list(definitions)
    existing = {definition.name for definition in merged}
    for definition in get_builtin_subagent_definitions():
        if definition.name not in existing:
            merged.append(definition)
            existing.add(definition.name)
    return merged


def _skill_subagent_name(skill_name: str) -> str | None:
    prefixes = ("subagent-", "subagent_")
    for prefix in prefixes:
        if skill_name.startswith(prefix):
            suffix = skill_name[len(prefix):].strip().replace("-", "_")
            return suffix or None
    return None


def register_agent_tool(
    registry: "ToolRegistry",
    delegation: "DelegationRuntime | None",
    config,
) -> bool:
    """Compatibility wrapper for the legacy single-tool registration path."""
    return register_subagent_tools(registry, delegation, config) > 0


__all__ = [
    "SubAgentTool",
    "SubagentDefinition",
    "load_subagent_definitions",
    "load_subagent_definitions_from_skills",
    "register_agent_tool",
    "register_skill_subagent_tools",
    "register_subagent_tools",
    "get_builtin_subagent_definitions",
]
