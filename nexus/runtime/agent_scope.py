from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from nexus.tools.base import ToolRegistry
from nexus.tools.base import ToolKind


SUPERVISOR_SCOPE_FIELDS: tuple[str, ...] = (
    "agent_allowed_tools",
    "agent_allowed_skills",
    "agent_allowed_mcp_servers",
)

SUPERVISOR_DELTA_SCOPE_FIELDS: tuple[str, ...] = (
    "agent_add_tools",
    "agent_remove_tools",
    "agent_add_skills",
    "agent_remove_skills",
    "agent_add_mcp_servers",
    "agent_remove_mcp_servers",
)

SUBAGENT_PROFILE_FIELDS: tuple[str, ...] = (
    "allowed_tools",
    "allowed_skills",
    "allowed_mcps",
    "allowed_mcp_servers",
    "add_tools",
    "remove_tools",
    "add_skills",
    "remove_skills",
    "add_mcps",
    "remove_mcps",
    "add_mcp_servers",
    "remove_mcp_servers",
)

SUPERVISOR_DEFAULT_TOOLS: tuple[str, ...] = ("bash", "read_file", "ask_user")

LEGACY_BUILTIN_SUBAGENT_NAME_ALIASES: dict[str, str] = {
    "planning_analysis": "explorer",
    "execution": "coding",
    "review": "code_reviewer",
    "verification": "impact_analyzer",
    "research": "explorer",
    "test": "code_reviewer",
}

@dataclass(frozen=True, slots=True)
class BuiltinSubagentSpec:
    name: str
    description: str
    goal_prompt: str
    allowed_tools: tuple[str, ...]
    max_turns: int
    timeout_seconds: float
    route_label: str
    preference_description: str
    priority: int
    context_packet_key: str


BUILTIN_SUBAGENT_SPECS: tuple[BuiltinSubagentSpec, ...] = (
    BuiltinSubagentSpec(
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
        allowed_tools=("read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"),
        max_turns=20,
        timeout_seconds=300,
        route_label="planning",
        preference_description="Preferred for bounded read-only exploration, planning context, and codebase scans.",
        priority=0,
        context_packet_key="exploration_summary",
    ),
    BuiltinSubagentSpec(
        name="coding",
        description="Implement a focused coding task using the workspace edit tools and cheap local validation.",
        goal_prompt=(
            "You are a Nexus coding agent. Implement only the assigned change, follow existing project "
            "patterns, keep edits focused, and use only cheap local validation that directly supports your "
            "change. Your success condition is a real workspace edit for the requested code change. For a "
            "coding request, do not return status `completed` unless you used a mutating tool and can list "
            "changed_files. If you cannot identify the target file after a small focused search, return "
            "status `blocked` with clarifications_needed or recommended_next_action instead of reading more. "
            "Before each read/search, know which edit decision it will unlock. Prefer packet "
            "summaries and targeted snippets before full-file rereads. Do not reread the same file in one task. "
            "Do not call exploration-style tools after you know the file to edit; edit it. Do not choose broad verification "
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
        allowed_tools=(
            "read_file",
            "write_file",
            "edit",
            "insert_edit_into_file",
            "apply_patch",
            "glob",
            "grep",
            "list_dir",
            "lsp",
            "git_status",
            "git_diff",
            "run_python_check",
            "run_formatter",
        ),
        max_turns=14,
        timeout_seconds=600,
        route_label="execution",
        preference_description="Preferred for file edits, implementation, and cheap local validation tied to those edits.",
        priority=1,
        context_packet_key="coding_summary",
    ),
    BuiltinSubagentSpec(
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
        allowed_tools=("git_diff", "read_file", "grep", "lsp", "git_status", "run_tests", "run_python_check"),
        max_turns=8,
        timeout_seconds=300,
        route_label="review",
        preference_description="Preferred for post-change review, scoped automated verification, and failure attribution.",
        priority=2,
        context_packet_key="review_findings",
    ),
    BuiltinSubagentSpec(
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
        allowed_tools=("read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"),
        max_turns=10,
        timeout_seconds=300,
        route_label="verification",
        preference_description="Preferred when blast radius, affected interfaces, or scoped verification targets are unclear.",
        priority=3,
        context_packet_key="impact_analysis",
    ),
)

BUILTIN_SUBAGENT_NAMES: frozenset[str] = frozenset(spec.name for spec in BUILTIN_SUBAGENT_SPECS)


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


def _mapping_scope_names(mapping: dict[str, Any], *keys: str) -> list[str]:
    names: list[str] = []
    for key in keys:
        for item in explicit_scope_names(mapping.get(key, [])):
            if item not in names:
                names.append(item)
    return names


def _mapping_scope_has_all(mapping: dict[str, Any], *keys: str) -> bool:
    return any(is_all_scope(mapping.get(key, [])) for key in keys)


def _mapping_has_scope(mapping: dict[str, Any], *keys: str) -> bool:
    return _mapping_scope_has_all(mapping, *keys) or bool(_mapping_scope_names(mapping, *keys))


def _config_scope_names(config: Any, field_name: str) -> list[str]:
    return explicit_scope_names(getattr(config, field_name, []))


def _config_has_scope(config: Any, field_name: str) -> bool:
    value = getattr(config, field_name, [])
    return is_all_scope(value) or bool(explicit_scope_names(value))


def _apply_scope_delta(
    base: Iterable[str],
    *,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    candidates: Iterable[str] | None = None,
) -> set[str]:
    allowed = set(base)
    candidate_set = set(candidates) if candidates is not None else None
    additions = set(add)
    removals = set(remove)
    if candidate_set is not None:
        additions &= candidate_set
        removals &= candidate_set
    allowed |= additions
    allowed -= removals
    return allowed


def normalize_subagent_name(name: str) -> str:
    normalized = str(name).strip()
    if normalized.startswith("subagent_"):
        normalized = normalized[len("subagent_") :]
    if normalized.startswith("subagent-"):
        normalized = normalized[len("subagent-") :]
    return normalized.replace("-", "_")


def canonical_builtin_subagent_name(name: str) -> str:
    normalized = normalize_subagent_name(name)
    return LEGACY_BUILTIN_SUBAGENT_NAME_ALIASES.get(normalized, normalized)


def subagent_tool_name(name: str) -> str:
    normalized = normalize_subagent_name(name)
    return normalized if normalized.startswith("subagent_") else f"subagent_{normalized}"


def builtin_subagent_tool_names() -> tuple[str, ...]:
    return tuple(subagent_tool_name(spec.name) for spec in sorted(BUILTIN_SUBAGENT_SPECS, key=lambda item: item.priority))


def builtin_subagent_priority(tool_name: str) -> int:
    normalized = canonical_builtin_subagent_name(tool_name)
    for spec in BUILTIN_SUBAGENT_SPECS:
        if spec.name == normalized:
            return spec.priority
    return 50


def builtin_subagent_preference(tool_name: str) -> str | None:
    normalized = canonical_builtin_subagent_name(tool_name)
    for spec in BUILTIN_SUBAGENT_SPECS:
        if spec.name == normalized:
            return spec.preference_description
    return None


def builtin_subagent_context_packet_key(role: str) -> str:
    normalized = canonical_builtin_subagent_name(role)
    for spec in BUILTIN_SUBAGENT_SPECS:
        if spec.name == normalized:
            return spec.context_packet_key
    return "subagent_summary"


def subagent_profile(config: Any, name: str) -> dict[str, Any]:
    normalized = normalize_subagent_name(name)
    canonical = canonical_builtin_subagent_name(name)
    fallback: dict[str, Any] | None = None
    for entry in getattr(config, "subagent_profiles", []) or []:
        if not isinstance(entry, dict):
            continue
        entry_name = normalize_subagent_name(str(entry.get("name", "")))
        if entry_name == normalized:
            return entry
        if canonical_builtin_subagent_name(entry_name) == canonical:
            fallback = entry
    if fallback is not None:
        return fallback
    return {}


def configured_subagent_names(config: Any) -> set[str]:
    names: set[str] = set()
    for entry in getattr(config, "subagent_profiles", []) or []:
        if not isinstance(entry, dict):
            continue
        name = normalize_subagent_name(str(entry.get("name", "")))
        if name:
            names.add(name)
            names.add(canonical_builtin_subagent_name(name))
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


def _supervisor_default_normal_tools(config: Any, direct_normal_names: set[str], *, has_subagents: bool) -> set[str]:
    if str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced":
        return set(SUPERVISOR_DEFAULT_TOOLS) & direct_normal_names
    if has_subagents:
        # Explicitly configured sub-agents in basic mode route work through
        # those agents unless supervisor scope is expanded.
        return set()
    return set(direct_normal_names)


def _supervisor_default_mcp_tools(config: Any, registry: ToolRegistry, *, has_subagents: bool) -> set[str]:
    if str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced":
        return all_mcp_tool_names(registry)
    if has_subagents:
        return set()
    return all_mcp_tool_names(registry)


def supervisor_tool_names(config: Any, registry: ToolRegistry) -> set[str]:
    records = registry.records()
    subagent_names = {record.name for record in records if record.name.startswith("subagent_")}
    user_input_names = {
        record.name
        for record in records
        if getattr(record.tool, "kind", None) is ToolKind.USER_INPUT
    }
    direct_normal_names = {
        record.name
        for record in records
        if record.source != "mcp" and record.name not in subagent_names and record.name != "delegate_task"
    }
    configured_tool_scope = getattr(config, "agent_allowed_tools", [])
    configured_mcp_scope = getattr(config, "agent_allowed_mcp_servers", [])
    explicit_tools = _config_has_scope(config, "agent_allowed_tools")
    explicit_mcp = _config_has_scope(config, "agent_allowed_mcp_servers")

    if is_all_scope(configured_tool_scope):
        normal_base = set(direct_normal_names)
    elif explicit_tools:
        normal_base = set(_config_scope_names(config, "agent_allowed_tools")) & direct_normal_names
    else:
        normal_base = _supervisor_default_normal_tools(config, direct_normal_names, has_subagents=bool(subagent_names))

    if is_all_scope(configured_mcp_scope):
        mcp_base = all_mcp_tool_names(registry)
    elif explicit_mcp:
        mcp_base = mcp_tool_names_for_servers(registry, _config_scope_names(config, "agent_allowed_mcp_servers"))
    else:
        mcp_base = _supervisor_default_mcp_tools(config, registry, has_subagents=bool(subagent_names))

    allowed = _apply_scope_delta(
        normal_base,
        add=_config_scope_names(config, "agent_add_tools"),
        remove=_config_scope_names(config, "agent_remove_tools"),
        candidates=direct_normal_names,
    )
    allowed |= _apply_scope_delta(
        mcp_base,
        add=mcp_tool_names_for_servers(registry, _config_scope_names(config, "agent_add_mcp_servers")),
        remove=mcp_tool_names_for_servers(registry, _config_scope_names(config, "agent_remove_mcp_servers")),
        candidates=all_mcp_tool_names(registry),
    )
    # Always ensure delegation tools are reachable regardless of mode or explicit scope;
    # configuring direct tools like bash should not silently remove sub-agent access.
    allowed |= subagent_names
    allowed |= user_input_names
    return allowed


def supervisor_skill_names(config: Any, active_skills: Iterable[str]) -> list[str]:
    active = _ordered_unique(active_skills)
    active_set = set(active)
    configured_scope = getattr(config, "agent_allowed_skills", [])
    if is_all_scope(configured_scope):
        base = active
    elif _config_has_scope(config, "agent_allowed_skills"):
        base = [name for name in _config_scope_names(config, "agent_allowed_skills") if name in active_set]
    elif str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced":
        base = []
    else:
        base = active
    allowed = _apply_scope_delta(
        base,
        add=_config_scope_names(config, "agent_add_skills"),
        remove=_config_scope_names(config, "agent_remove_skills"),
        candidates=active,
    )
    return _ordered_unique(name for name in active if name in allowed)


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
    if _mapping_scope_has_all(profile, "allowed_skills"):
        configured = active
    elif _mapping_has_scope(profile, "allowed_skills"):
        configured = [skill_name for skill_name in _mapping_scope_names(profile, "allowed_skills") if skill_name in active_set]
    else:
        if base_allowed_skills is None:
            configured = active
        elif base_allowed_skills != ():
            configured = [skill_name for skill_name in clean_string_list(list(base_allowed_skills)) if skill_name in active_set]
        else:
            configured = []
    allowed = _apply_scope_delta(
        configured,
        add=_mapping_scope_names(profile, "add_skills"),
        remove=_mapping_scope_names(profile, "remove_skills"),
        candidates=active,
    )
    return _ordered_unique(skill_name for skill_name in active if skill_name in allowed)


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
        if (
            record.source != "mcp"
            and not record.name.startswith("subagent_")
            and record.name != "delegate_task"
            and getattr(record.tool, "kind", None) is not ToolKind.USER_INPUT
        )
    }

    if _mapping_scope_has_all(profile, "allowed_tools"):
        allowed = set(normal_candidate_names)
    elif _mapping_has_scope(profile, "allowed_tools"):
        allowed = set(_mapping_scope_names(profile, "allowed_tools")) & normal_candidate_names
    elif base_allowed_tools is None:
        allowed = set(normal_candidate_names)
    else:
        allowed = set(clean_string_list(list(base_allowed_tools))) & normal_candidate_names

    allowed = _apply_scope_delta(
        allowed,
        add=_mapping_scope_names(profile, "add_tools"),
        remove=_mapping_scope_names(profile, "remove_tools"),
        candidates=normal_candidate_names,
    )

    if _mapping_scope_has_all(profile, "allowed_mcps", "allowed_mcp_servers"):
        mcp_allowed = all_mcp_tool_names(registry)
    elif _mapping_has_scope(profile, "allowed_mcps", "allowed_mcp_servers"):
        mcp_allowed = mcp_tool_names_for_servers(registry, _mapping_scope_names(profile, "allowed_mcps", "allowed_mcp_servers"))
    elif base_allowed_mcps is None:
        mcp_allowed = all_mcp_tool_names(registry)
    elif base_allowed_mcps != ():
        mcp_allowed = mcp_tool_names_for_servers(registry, clean_string_list(list(base_allowed_mcps)))
    else:
        mcp_allowed = set()

    allowed |= _apply_scope_delta(
        mcp_allowed,
        add=mcp_tool_names_for_servers(registry, _mapping_scope_names(profile, "add_mcps", "add_mcp_servers")),
        remove=mcp_tool_names_for_servers(registry, _mapping_scope_names(profile, "remove_mcps", "remove_mcp_servers")),
        candidates=all_mcp_tool_names(registry),
    )
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
