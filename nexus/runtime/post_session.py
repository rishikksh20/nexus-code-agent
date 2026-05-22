from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.config.defaults import AgentConfig
from nexus.memory.profiles import UserProfile
from nexus.memory.workspace import AgentDirs, WorkspaceKnowledge
from nexus.runtime.sessions import SessionSnapshot


logger = logging.getLogger(__name__)
_POST_SESSION_TRANSCRIPT_MAX_CHARS = 200_000


FACT_PATTERNS = {
    "venv_path": re.compile(r"([./~]\S+/bin/activate)"),
    "test_command": re.compile(r"\b(pytest|npm test|cargo test|go test)\b"),
    "build_command": re.compile(r"\b(npm run build|cargo build|make build|python -m build)\b"),
}


def run_post_session_updates(config: AgentConfig, snapshot: SessionSnapshot, *, active_skills: list[str] | None = None) -> None:
    if not snapshot.messages:
        return
    dirs = AgentDirs(workspace_root=config.workspace_root.resolve(), global_root=config.global_root.resolve())
    dirs.ensure()
    transcript_parts: list[str] = []
    running_chars = 0
    for message in reversed(snapshot.messages):
        if message.role == "tool" or not message.content:
            continue
        content = message.content
        running_chars += len(content)
        transcript_parts.append(content)
        if running_chars >= _POST_SESSION_TRANSCRIPT_MAX_CHARS:
            break
    transcript = "\n".join(reversed(transcript_parts))
    facts = extract_facts(transcript)
    try:
        workspace_payload = _load_json(dirs.facts_file, default={})
        workspace_payload = _update_workspace_payload(
            workspace_payload,
            config=config,
            snapshot=snapshot,
            facts=facts,
            active_skills=active_skills or [],
        )
        _atomic_write_json(dirs.facts_file, workspace_payload)
        _atomic_write_text(dirs.knowledge_file, _workspace_knowledge_from_payload(workspace_payload).to_markdown())

        workspaces_payload = _load_json(dirs.workspaces_file, default={"workspaces": {}})
        workspaces_payload.setdefault("workspaces", {})[str(config.workspace_root)] = workspace_payload
        _atomic_write_json(dirs.workspaces_file, workspaces_payload)
        _atomic_write_text(dirs.profile_file, _profile_from_workspaces(workspaces_payload).to_markdown())
    except Exception:
        logger.exception("Post-session updates failed")


def extract_facts(text: str) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    for fact_type, pattern in FACT_PATTERNS.items():
        for match in pattern.finditer(text):
            facts.append({"type": fact_type, "value": match.group(1) if match.lastindex else match.group(0)})
    return _dedupe_dicts(facts)


def _update_workspace_payload(
    existing: dict[str, Any],
    *,
    config: AgentConfig,
    snapshot: SessionSnapshot,
    facts: list[dict[str, str]],
    active_skills: list[str],
) -> dict[str, Any]:
    recent_task = snapshot.summary or next((message.content for message in snapshot.messages if message.role == "user"), "Completed session")
    tool_names = [message.name for message in snapshot.messages if message.role == "tool" and message.name]
    tech_stack = list(existing.get("tech_stack", []))
    if "pytest" in " ".join(fact["value"] for fact in facts) and "Python" not in tech_stack:
        tech_stack.append("Python")
    recent_tasks = _merge_recent(list(existing.get("recent_tasks", [])), [recent_task], limit=10)
    all_facts = _dedupe_dicts(list(existing.get("facts", [])) + facts)
    conventions = _merge_recent(list(existing.get("conventions", [])), [f"Active skills used: {', '.join(active_skills)}"] if active_skills else [], limit=10)
    next_steps = list(existing.get("next_steps", [])) or ["Continue evolving the harness incrementally."]
    return {
        "project_name": config.project_name,
        "description": config.project_description,
        "tech_stack": tech_stack,
        "conventions": conventions,
        "source_of_truth": list(existing.get("source_of_truth", [])),
        "key_files": {
            "README.md": "Primary operator documentation.",
            str(config.local_config_file.relative_to(config.workspace_root)): "Workspace-specific Nexus configuration.",
        },
        "next_steps": next_steps,
        "recent_tasks": recent_tasks,
        "facts": all_facts,
        "session_count": int(existing.get("session_count", 0)) + 1,
        "last_updated": datetime.now(UTC).isoformat(),
        "tools": _merge_recent(list(existing.get("tools", [])), tool_names, limit=20),
    }


def _workspace_knowledge_from_payload(payload: dict[str, Any]) -> WorkspaceKnowledge:
    return WorkspaceKnowledge(
        project_name=str(payload.get("project_name", "workspace")),
        description=str(payload.get("description", "")),
        tech_stack=list(payload.get("tech_stack", [])),
        conventions=list(payload.get("conventions", [])),
        source_of_truth=list(payload.get("source_of_truth", [])),
        key_files=dict(payload.get("key_files", {})),
        next_steps=list(payload.get("next_steps", [])),
        recent_tasks=list(payload.get("recent_tasks", [])),
        facts=list(payload.get("facts", [])),
        session_count=int(payload.get("session_count", 0)),
    )


def _profile_from_workspaces(payload: dict[str, Any]) -> UserProfile:
    workspaces = payload.get("workspaces", {})
    tool_counts: dict[str, int] = {}
    preferred_languages: set[str] = set()
    recurring_workflows: set[str] = set()
    common_constraints: set[str] = set()
    for workspace in workspaces.values():
        for tool_name in workspace.get("tools", []):
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
        for stack_item in workspace.get("tech_stack", []):
            preferred_languages.add(stack_item)
        for fact in workspace.get("facts", []):
            if fact.get("type") == "test_command":
                recurring_workflows.add(f"Verification usually uses {fact['value']}")
        if workspace.get("conventions"):
            common_constraints.add("Prefers explicit runtime boundaries and small iterative changes")
    preferred_tools = [name for name, _count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))[:5]]
    return UserProfile(
        preferred_languages=sorted(preferred_languages),
        response_style="concise",
        preferred_tools=preferred_tools,
        recurring_workflows=sorted(recurring_workflows),
        common_constraints=sorted(common_constraints),
    )


def _load_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return dict(default)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")
    tmp.replace(path)


def _merge_recent(existing: list[str], new_items: list[str], *, limit: int) -> list[str]:
    merged = [item for item in existing if item]
    for item in new_items:
        if not item:
            continue
        if item in merged:
            merged.remove(item)
        merged.append(item)
    return merged[-limit:]


def _dedupe_dicts(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for item in items:
        key = (str(item.get("type", "")), str(item.get("value", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"type": key[0], "value": key[1]})
    return deduped
