"""YAML-based cognitive sub-agent file discovery and management.

Sub-agents are defined as individual ``.yml`` files — one file per agent.
The file name (without extension) must match the ``name`` field inside.

Discovery roots (later overrides earlier on name collision):

1. Global catalogue — ``~/.nexus/agents/*.yml``
2. Workspace-local  — ``.nexus/agents/*.yml``

Minimal example (``.nexus/agents/explore.yml``):

.. code-block:: yaml

    name: explore
    description: Investigate a focused codebase question and summarize the answer.
    goal_prompt: |
      Read the relevant code and summarize the answer. Do not modify files.
    allowed_tools:
      - read_file
      - glob
      - grep
      - list_dir
      - lsp
    max_turns: 12
    timeout_seconds: 300

The resulting sub-agent tool is registered as ``subagent_explore``.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.sandbox.agent_tool import SubagentDefinition

if TYPE_CHECKING:
    from nexus.config.defaults import AgentConfig

logger = logging.getLogger(__name__)

# Allowed characters in a sub-agent name.
_NAME_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")


class SubagentYamlError(ValueError):
    """Raised when a YAML sub-agent file cannot be parsed or validated."""


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def get_agent_roots(config: "AgentConfig") -> tuple[Path, Path]:
    """Return ``(local_agents_dir, global_agents_dir)`` for *config*.

    The directories are not required to exist; callers should check before
    iterating.
    """
    local = config.local_root / "agents"
    global_ = config.global_root / "agents"
    return local, global_


def load_yaml_subagent_definitions(config: "AgentConfig") -> list[SubagentDefinition]:
    """Discover and load all valid YAML sub-agent files.

    Global definitions are loaded first; workspace-local definitions with the
    same name override them.  Invalid files are logged and skipped.
    """
    local, global_ = get_agent_roots(config)
    # Use dict to allow local override of global definitions.
    definitions: dict[str, SubagentDefinition] = {}

    for root in (global_, local):
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.glob("*.yml")):
            try:
                defn = parse_subagent_yaml(path)
            except SubagentYamlError as exc:
                logger.warning("Skipping invalid sub-agent YAML at %s: %s", path, exc)
                continue
            definitions[defn.name] = defn

    return list(definitions.values())


def list_yaml_subagent_files(config: "AgentConfig") -> list[dict[str, Any]]:
    """Return metadata records for every discovered YAML sub-agent file.

    Each record contains: ``name``, ``scope`` (``"local"`` or ``"global"``),
    ``path``, ``description``, ``allowed_tools``, ``max_turns``,
    ``timeout_seconds``, ``allowed_skills``, ``allowed_mcps``, and ``valid``
    (bool). Invalid files include an ``error`` key instead of the definition
    fields.
    """
    local, global_ = get_agent_roots(config)
    records: list[dict[str, Any]] = []

    for scope, root in (("global", global_), ("local", local)):
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.glob("*.yml")):
            try:
                defn = parse_subagent_yaml(path)
                records.append({
                    "name": defn.name,
                    "scope": scope,
                    "path": str(path),
                    "description": defn.description,
                    "allowed_tools": defn.allowed_tools or [],
                    "allowed_skills": defn.allowed_skills or [],
                    "allowed_mcps": defn.allowed_mcps or [],
                    "max_turns": defn.max_turns,
                    "timeout_seconds": defn.timeout_seconds,
                    "valid": True,
                })
            except SubagentYamlError as exc:
                records.append({
                    "name": path.stem,
                    "scope": scope,
                    "path": str(path),
                    "error": str(exc),
                    "valid": False,
                })

    return records


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_subagent_yaml(path: Path) -> SubagentDefinition:
    """Parse and validate one YAML sub-agent file.

    Raises :class:`SubagentYamlError` on any validation failure.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SubagentYamlError(f"Cannot read {path}: {exc}") from exc

    data = _load_yaml(raw, path)

    name = _required_string(data, "name", path)
    stem = path.stem
    if name != stem:
        raise SubagentYamlError(
            f"Sub-agent name '{name}' must match the file name '{stem}.yml'."
        )
    if not _NAME_RE.fullmatch(name):
        raise SubagentYamlError(
            f"Sub-agent name '{name}' must be lowercase letters, digits, "
            "hyphens, or underscores only."
        )

    description = _required_string(data, "description", path)
    goal_prompt = _required_string(data, "goal_prompt", path)
    allowed_tools = _parse_tool_list(data.get("allowed_tools"), path)
    allowed_skills = _parse_tool_list(data.get("allowed_skills"), path)
    allowed_mcps = _parse_tool_list(data.get("allowed_mcps"), path)
    max_turns = _parse_positive_int(data.get("max_turns", 20), "max_turns", path)
    timeout_seconds = _parse_positive_float(
        data.get("timeout_seconds", 600.0), "timeout_seconds", path
    )

    return SubagentDefinition(
        name=name,
        description=description,
        goal_prompt=goal_prompt,
        allowed_tools=allowed_tools,
        allowed_skills=allowed_skills,
        allowed_mcps=allowed_mcps,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Promotion / demotion
# ---------------------------------------------------------------------------

def promote_to_global(name: str, config: "AgentConfig") -> Path:
    """Move a local YAML sub-agent file to the global agents directory.

    Returns the destination path.

    Raises
    ------
    FileNotFoundError
        If ``.nexus/agents/{name}.yml`` does not exist.
    FileExistsError
        If ``~/.nexus/agents/{name}.yml`` already exists.
    """
    local, global_ = get_agent_roots(config)
    src = local / f"{name}.yml"
    if not src.exists():
        raise FileNotFoundError(f"Local sub-agent file not found: {src}")
    global_.mkdir(parents=True, exist_ok=True)
    dst = global_ / f"{name}.yml"
    if dst.exists():
        raise FileExistsError(
            f"A global sub-agent named '{name}' already exists at {dst}."
        )
    shutil.copy2(src, dst)
    src.unlink()
    return dst


def demote_to_local(name: str, config: "AgentConfig") -> Path:
    """Move a global YAML sub-agent file to the local agents directory.

    Returns the destination path.

    Raises
    ------
    FileNotFoundError
        If ``~/.nexus/agents/{name}.yml`` does not exist.
    FileExistsError
        If ``.nexus/agents/{name}.yml`` already exists.
    """
    local, global_ = get_agent_roots(config)
    src = global_ / f"{name}.yml"
    if not src.exists():
        raise FileNotFoundError(f"Global sub-agent file not found: {src}")
    local.mkdir(parents=True, exist_ok=True)
    dst = local / f"{name}.yml"
    if dst.exists():
        raise FileExistsError(
            f"A local sub-agent named '{name}' already exists at {dst}."
        )
    shutil.copy2(src, dst)
    src.unlink()
    return dst


# ---------------------------------------------------------------------------
# YAML scaffold template
# ---------------------------------------------------------------------------

SUBAGENT_YAML_TEMPLATE = """\
name: {name}
description: "One-sentence description of what this sub-agent does."
goal_prompt: |
  Describe the sub-agent's role and constraints here.
  Do not modify files unless explicitly permitted.
allowed_tools:
  - read_file
  - glob
  - grep
  - list_dir
  - lsp
# Omit or leave empty to allow all active skills/MCP servers.
allowed_skills: []
allowed_mcps: []
max_turns: 12
timeout_seconds: 300
"""


def scaffold_yaml(name: str) -> str:
    """Return a starter YAML template for a new sub-agent named *name*."""
    return SUBAGENT_YAML_TEMPLATE.format(name=name)


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------

def _load_yaml(text: str, path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]
        parsed = yaml.safe_load(text)
    except Exception as exc:
        raise SubagentYamlError(f"Invalid YAML in {path}: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise SubagentYamlError(f"YAML root in {path} must be a mapping, got {type(parsed).__name__}.")
    return dict(parsed)


def _required_string(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SubagentYamlError(
            f"Sub-agent YAML at {path} requires a non-empty '{key}' field."
        )
    return value.strip()


def _parse_tool_list(value: Any, path: Path) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        tools = [part for part in value.split() if part]
        return tools or None
    if isinstance(value, list):
        tools = [str(t).strip() for t in value if str(t).strip()]
        return tools or None
    raise SubagentYamlError(
        f"'allowed_tools' in {path} must be a YAML list or space-separated string."
    )


def _parse_positive_int(value: Any, key: str, path: Path) -> int:
    try:
        n = int(value)
        if n <= 0:
            raise ValueError
        return n
    except (ValueError, TypeError) as exc:
        raise SubagentYamlError(
            f"'{key}' in {path} must be a positive integer, got {value!r}."
        ) from exc


def _parse_positive_float(value: Any, key: str, path: Path) -> float:
    try:
        f = float(value)
        if f <= 0:
            raise ValueError
        return f
    except (ValueError, TypeError) as exc:
        raise SubagentYamlError(
            f"'{key}' in {path} must be a positive number, got {value!r}."
        ) from exc
