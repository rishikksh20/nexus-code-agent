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


@dataclass(slots=True, frozen=True)
class ConfigUpgradeReport:
    path: Path
    missing_keys: tuple[str, ...] = ()
    deprecated_keys: tuple[str, ...] = ()
    current_version: int | None = None
    target_version: int = CURRENT_CONFIG_VERSION

    @property
    def needs_upgrade(self) -> bool:
        return bool(self.missing_keys or self.deprecated_keys or self.current_version != self.target_version)


def inspect_config_upgrade(path: Path, template_str: str) -> ConfigUpgradeReport:
    existing = _read_top_level_toml(path)
    template = tomllib.loads(template_str)
    missing = tuple(key for key in template if key not in existing)
    deprecated = tuple(key for key in DEPRECATED_CONFIG_KEYS if key in existing)
    version = _optional_int(existing.get("config_version"))
    return ConfigUpgradeReport(
        path=path,
        missing_keys=missing,
        deprecated_keys=deprecated,
        current_version=version,
    )


def upgrade_config_file(path: Path, template_str: str) -> ConfigUpgradeReport:
    before = inspect_config_upgrade(path, template_str)
    template = tomllib.loads(template_str)
    existing = _read_top_level_toml(path)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = _remove_deprecated_and_version_lines(content.splitlines())

    additions: list[str] = []
    if lines and lines[-1].strip():
        additions.append("")
    additions.append("# Added by Nexus config upgrade")
    additions.append(f"config_version = {CURRENT_CONFIG_VERSION}")
    for key in template:
        if key == "config_version" or key in existing:
            continue
        additions.append(f"{key} = {_render_toml_value(template[key])}")

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


def _read_top_level_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        return {}
    return {key: value for key, value in data.items() if not isinstance(value, dict)}


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
