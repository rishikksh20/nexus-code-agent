"""Cognitive sub-agent tool helpers exposed from the tools package."""
from __future__ import annotations

from typing import TYPE_CHECKING

from nexus.sandbox.agent_tool import SubAgentTool, SubagentDefinition
from nexus.runtime.agent_scope import configured_subagent_names, normalize_subagent_name
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
    1. Built-in cognitive personas (explorer, coding, code_reviewer, impact_analyzer)
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
        definition = _with_builtin_mcp_tools(definition, registry)
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
            name="explorer",
            description="Explore a bounded codebase slice and return a concise read-only summary.",
            goal_prompt=(
                "You are a read-only Nexus explorer agent. Inspect only the requested codebase slice, "
                "use a small number of focused read-only tools, and prefer packet summaries before rereading files. "
                "Your goal is to answer the delegated question, not to map the whole repository. Start with the "
                "named path or symbol, then read only the README, entrypoint, or closest owner file needed to "
                "ground the answer. Stop once you have enough evidence to summarize the requested slice; do not "
                "keep searching for completeness. If the target cannot be found within the requested slice, return "
                "status `blocked` with the paths or patterns you tried. Return status `completed` only when your "
                "summary directly answers the objective. Do not modify files or invent implementation plans unless "
                "the instructions ask for one."
            ),
            allowed_tools=["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"],
            max_turns=20,
            timeout_seconds=300,
        ),
        SubagentDefinition(
            name="coding",
            description="Implement a focused coding task using the workspace edit tools and cheap local validation.",
            goal_prompt=(
                "You are a Nexus coding agent. Implement only the assigned change, follow existing project "
                "patterns, keep edits focused, and use only cheap local validation that directly supports your "
                "change. Your success condition is a real workspace edit for the requested code change. For a "
                "coding request, do not return status `completed` unless you used a mutating tool and can list "
                "changed_files. If you cannot identify the target file after a small focused search, return "
                "status `blocked` with clarifications_needed or recommended_next_action instead of reading more. "
                "Use minimal sufficient context: for a create-file, missing-file, or direct port task with known "
                "source and target paths, read the source/spec plus at most one or two local style references, then "
                "write the file. Before each read/search, know which edit decision it will unlock. Prefer packet "
                "summaries and targeted snippets before full-file rereads. Do not reread the same file in one task. "
                "Do not call exploration-style tools after you know the file to edit; edit it. If you reach three "
                "read/search calls before the first mutation and still cannot edit, return status `blocked` with "
                "the missing evidence instead of continuing to browse. Do not choose broad verification "
                "scope yourself; leave review and scoped test selection to the supervisor and the code reviewer. "
                "Return changed files, validation you ran, open risks, and any suggested follow-up context for "
                "downstream review.\n\n"
                "Validation rules:\n"
                "- run_python_check is a COMPILE/SYNTAX checker only (python -m compileall). It does NOT "
                "run or execute code. Only call it on .py files you just wrote or edited to verify they "
                "parse without syntax errors. Pass the file path(s) as args, e.g. args=['my_file.py'].\n"
                "- Do NOT pass -c, -u, or any flag that attempts code execution to run_python_check.\n"
                "- If run_python_check fails once, report the failure in your result and stop. Do NOT "
                "retry with different argument styles or creative workarounds.\n"
                "- Do NOT attempt to verify runtime behaviour (running the code, importing it, executing "
                "tests). Runtime verification belongs to subagent_code_reviewer or the supervisor.\n"
                "- Limit file reads: read each file at most once. If you already saw a file's content "
                "in the current task, do not read it again."
            ),
            allowed_tools=["read_file", "write_file", "edit", "insert_edit_into_file", "apply_patch", "glob", "grep", "list_dir", "lsp", "git_status", "git_diff", "run_python_check", "run_formatter"],
            max_turns=14,
            timeout_seconds=600,
        ),
        SubagentDefinition(
            name="code_reviewer",
            description="Review code changes and run scoped automated verification after impact analysis.",
            goal_prompt=(
                "You are a senior Nexus code reviewer. Inspect the diff and the targeted source files, "
                "prioritize concrete bugs, regressions, and maintainability risks, and run only the scoped "
                "verification justified by the provided impact analysis or task context. Your goal is a decision: "
                "approved, issues_found, failed_verification, or blocked. Stop after you have enough evidence for "
                "that decision. Distinguish failures that are likely related to the task from likely pre-existing, "
                "flaky, environment, or unclear failures. Prefer focused run_tests args; broad pytest is allowed "
                "only for medium/high-risk shared infrastructure, config, tool runtime, provider integration, or "
                "cross-cutting changes. Do not modify files."
            ),
            allowed_tools=["git_diff", "read_file", "grep", "lsp", "git_status", "run_tests", "run_python_check"],
            max_turns=8,
            timeout_seconds=300,
        ),
        SubagentDefinition(
            name="impact_analyzer",
            description="Analyze change impact, blast radius, and scoped review or verification targets.",
            goal_prompt=(
                "You are a read-only Nexus impact analyzer. Determine the likely blast radius of the task or "
                "recent code changes, identify affected files and interfaces, recommend scoped verification and "
                "review targets, and call out where manual validation is still required. Your goal is to produce "
                "a verification plan, not to inspect every caller. Use git_diff or the named task/files first, "
                "then read only the smallest source slices needed to justify risk and tests. Stop once risk_level "
                "and candidate_tests are justified. Return changed_files, "
                "affected_modules, public_interfaces_changed, risk_level, validation_category, "
                "candidate_review_targets, candidate_tests, verification_policy, and failure_attribution_hints. "
                "Use repository evidence rather than generic assumptions, and do not modify files."
            ),
            allowed_tools=["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"],
            max_turns=10,
            timeout_seconds=300,
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
        allowed_skills=definition.allowed_skills,
        allowed_mcps=definition.allowed_mcps,
        max_turns=definition.max_turns,
        timeout_seconds=definition.timeout_seconds,
    )


def _load_yaml_definitions(config) -> list[SubagentDefinition]:
    """Load YAML sub-agent definitions; returns empty list on any error."""
    try:
        from nexus.agents.loader import load_yaml_subagent_definitions
        return load_yaml_subagent_definitions(config)
    except Exception:  # noqa: BLE001
        return []


def _is_builtin_subagent_definition(definition: SubagentDefinition) -> bool:
    return definition.name in {
        "explorer",
        "coding",
        "code_reviewer",
        "impact_analyzer",
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
    "register_yaml_subagent_tools",
    "get_builtin_subagent_definitions",
]
