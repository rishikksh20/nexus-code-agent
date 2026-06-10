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

SUBAGENT_PROFILE_FIELDS: tuple[str, ...] = (
    "allowed_tools",
    "allowed_skills",
    "allowed_mcps",
    "allowed_mcp_servers",
)

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
        name="planning_analysis",
        description="Explore a bounded codebase slice and return a concise read-only summary.",
        goal_prompt=(
            "You are a read-only Nexus planning-analysis agent. Inspect only the requested codebase slice, "
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
        name="execution",
        description="Implement a focused coding task using the workspace edit tools and cheap local validation.",
        goal_prompt=(
            "You are a Nexus execution agent. Implement only the assigned change, follow existing project "
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
            "scope yourself; leave review and scoped test selection to the supervisor and the review agent. "
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
            "tests). Runtime verification belongs to subagent_review or the supervisor.\n"
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
        name="verification",
        description="Analyze change impact, blast radius, and scoped review or verification targets.",
        goal_prompt=(
            "You are a read-only Nexus verification agent. Determine the likely blast radius of the task or "
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
    BuiltinSubagentSpec(
        name="review",
        description="Review code changes and run scoped automated verification after impact analysis.",
        goal_prompt=(
            "You are a senior Nexus review agent. Inspect the diff and the targeted source files, "
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


def builtin_subagent_tool_names() -> tuple[str, ...]:
    return tuple(subagent_tool_name(spec.name) for spec in sorted(BUILTIN_SUBAGENT_SPECS, key=lambda item: item.priority))


def builtin_subagent_priority(tool_name: str) -> int:
    normalized = normalize_subagent_name(tool_name)
    for spec in BUILTIN_SUBAGENT_SPECS:
        if spec.name == normalized:
            return spec.priority
    return 50


def builtin_subagent_preference(tool_name: str) -> str | None:
    normalized = normalize_subagent_name(tool_name)
    for spec in BUILTIN_SUBAGENT_SPECS:
        if spec.name == normalized:
            return spec.preference_description
    return None


def builtin_subagent_context_packet_key(role: str) -> str:
    normalized = normalize_subagent_name(role)
    for spec in BUILTIN_SUBAGENT_SPECS:
        if spec.name == normalized:
            return spec.context_packet_key
    return "subagent_summary"


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
    configured_tools = set(explicit_scope_names(configured_tool_scope))
    configured_mcp = set(explicit_scope_names(configured_mcp_scope))
    all_configured_tools = is_all_scope(configured_tool_scope)
    all_configured_mcp = is_all_scope(configured_mcp_scope)

    if configured_tools or configured_mcp or all_configured_tools or all_configured_mcp:
        allowed = set(direct_normal_names) if all_configured_tools else configured_tools & direct_normal_names
        # When no MCP scope is explicitly restricted, include all active MCP tools by default.
        if all_configured_mcp or not configured_mcp:
            allowed |= all_mcp_tool_names(registry)
        else:
            allowed |= mcp_tool_names_for_servers(registry, configured_mcp)
    elif str(getattr(config, "agent_mode", "basic")).strip().lower() == "advanced":
        allowed = set()
    elif subagent_names:
        # When delegation tools exist, default supervisor scope to those tools
        # so task work routes through sub-agents unless explicitly overridden.
        allowed = set(subagent_names)
    else:
        allowed = set(all_names)
    # Always ensure delegation tools are reachable regardless of mode or explicit scope;
    # configuring direct tools like bash should not silently remove sub-agent access.
    allowed |= subagent_names
    allowed |= user_input_names
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
        if (
            record.source != "mcp"
            and not record.name.startswith("subagent_")
            and record.name != "delegate_task"
            and getattr(record.tool, "kind", None) is not ToolKind.USER_INPUT
        )
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

    if is_all_scope(configured_mcp_scope):
        allowed |= all_mcp_tool_names(registry)
    elif configured_mcp:
        allowed |= mcp_tool_names_for_servers(registry, configured_mcp)
    elif base_allowed_mcps is None:
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
