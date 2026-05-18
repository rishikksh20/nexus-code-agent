"""Cognitive sub-agent tool helpers exposed from the tools package."""
from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.sandbox.agent_tool import SubAgentTool, SubagentDefinition
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
    """Register cognitive sub-agent tools."""
    if str(getattr(config, "agent_mode", "basic")).strip().lower() != "advanced":
        return 0

    count = 0
    for definition in _merge_builtin_definitions(definitions):
        definition = _with_builtin_mcp_tools(definition, registry)
        tool = SubAgentTool(
            definition=definition,
            model_client_factory=model_client_factory,
            base_tool_registry=registry,
            config=config,
        )
        if tool_enabled(config, tool.name):
            registry.register(tool, source="agent", origin=definition.name)
            count += 1
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
    if str(getattr(config, "agent_mode", "basic")).strip().lower() != "advanced":
        return 0

    count = 0
    existing_names = {record.name for record in registry.records()}
    for definition in load_subagent_definitions_from_skills(skill_registry):
        definition = _with_builtin_mcp_tools(definition, registry)
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
    """Return conservative built-in cognitive specialist personas."""
    return [
        SubagentDefinition(
            name="planning_analysis",
            description="Analyze the repo, detect ambiguity, and produce a focused execution plan without modifying files.",
            goal_prompt=(
                "You are a read-only Nexus planning and analysis agent. Research the requested codebase slice, "
                "trace relevant files and symbols, detect ambiguity, and return a compact implementation plan "
                "with dependencies, risks, clarification needs, and related paths. Do not modify files."
            ),
            allowed_tools=["read_file", "glob", "grep", "list_dir", "lsp"],
            max_turns=12,
            timeout_seconds=300,
        ),
        SubagentDefinition(
            name="execution",
            description="Implement a focused coding task using the normal workspace tools.",
            goal_prompt=(
                "You are a Nexus execution agent. Implement only the assigned task, follow existing project "
                "patterns, use tools for edits and validation, and return changed files, tests run, and blockers. "
                "When using bash, never run servers, watchers, REPLs, or infinite loops in the foreground. "
                "Use bounded commands with explicit timeouts; for dev servers, start them in the background, "
                "probe readiness, collect logs, and stop the process in the same command. If a bash command "
                "times out, do not retry the same command unchanged."
            ),
            allowed_tools=["read_file", "write_file", "edit", "insert_edit_into_file", "apply_patch", "glob", "grep", "list_dir", "lsp", "git_status", "git_diff", "run_tests", "run_linter", "run_typecheck", "bash"],
            max_turns=14,
            timeout_seconds=600,
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
            name="verification",
            description="Run structured verification and summarize failures.",
            goal_prompt=(
                "You are a Nexus verification agent. Run focused tests, lint, type/syntax checks, "
                "and git status inspection. Return a concise validation summary and failures. "
                "Do not modify files. When using bash, never run servers, watchers, REPLs, or infinite "
                "loops in the foreground. Use bounded commands with explicit timeouts; for server-based "
                "checks, start the server in the background, probe it, capture relevant output, and stop "
                "it in the same command. If a bash command times out, do not retry the same command "
                "unchanged; report the timeout and relevant output."
            ),
            allowed_tools=["run_tests", "run_linter", "run_typecheck", "git_status", "bash"],
            max_turns=6,
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


def _with_builtin_mcp_tools(definition: SubagentDefinition, registry: "ToolRegistry") -> SubagentDefinition:
    if not _is_builtin_subagent_definition(definition):
        return definition
    mcp_tools = [
        record.name
        for record in registry.records()
        if record.source == "mcp"
    ]
    if not mcp_tools:
        return definition
    allowed = list(definition.allowed_tools or [])
    changed = False
    for tool_name in mcp_tools:
        if tool_name not in allowed:
            allowed.append(tool_name)
            changed = True
    if not changed:
        return definition
    return SubagentDefinition(
        name=definition.name,
        description=definition.description,
        goal_prompt=definition.goal_prompt,
        allowed_tools=allowed,
        max_turns=definition.max_turns,
        timeout_seconds=definition.timeout_seconds,
    )


def _is_builtin_subagent_definition(definition: SubagentDefinition) -> bool:
    return definition.name in {
        "planning_analysis",
        "execution",
        "review",
        "verification",
    }


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
    "get_builtin_subagent_definitions",
]
