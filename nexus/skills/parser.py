from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class SkillParseError(ValueError):
    """Raised when an Agent Skill cannot be parsed or validated."""


_BOUNDARY = re.compile(r"^-{3}\s*$", re.MULTILINE)
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(slots=True, frozen=True)
class SkillMetadata:
    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()


def parse_skill_markdown(content: str) -> tuple[dict[str, Any], str]:
    parts = _BOUNDARY.split(content, maxsplit=2)
    if len(parts) < 3 or parts[0].strip():
        raise SkillParseError(
            "Missing or invalid YAML frontmatter; SKILL.md must start with --- metadata ---."
        )
    frontmatter = _parse_yaml_mapping(parts[1])
    return frontmatter, parts[2].strip()


def parse_skill_file(path: Path) -> tuple[SkillMetadata, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillParseError(f"Cannot read SKILL.md: {exc}") from exc
    frontmatter, body = parse_skill_markdown(raw)
    metadata = validate_skill_metadata(frontmatter, directory_name=path.parent.name)
    return metadata, body


def validate_skill_metadata(payload: dict[str, Any], *, directory_name: str) -> SkillMetadata:
    name = _required_string(payload, "name")
    if len(name) > 64 or not _NAME_RE.fullmatch(name):
        raise SkillParseError(
            "Skill name must be 1-64 lowercase letters, numbers, and hyphens; "
            "it must not start/end with a hyphen or contain consecutive hyphens."
        )
    if name != directory_name:
        raise SkillParseError(f"Skill name '{name}' must match parent directory '{directory_name}'.")

    description = _required_string(payload, "description")
    if len(description) > 1024:
        raise SkillParseError("Skill description must be 1-1024 characters.")

    license_value = _optional_string(payload, "license")
    compatibility = _optional_string(payload, "compatibility")
    if compatibility is not None and len(compatibility) > 500:
        raise SkillParseError("Skill compatibility must be 1-500 characters when provided.")

    raw_metadata = payload.get("metadata", {})
    if raw_metadata is None:
        metadata: dict[str, str] = {}
    elif isinstance(raw_metadata, dict):
        metadata = {str(key): str(value) for key, value in raw_metadata.items()}
    else:
        raise SkillParseError("Skill metadata must be a mapping when provided.")

    return SkillMetadata(
        name=name,
        description=description,
        license=license_value,
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=_parse_allowed_tools(payload.get("allowed-tools", ())),
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillParseError(f"Skill frontmatter requires a non-empty '{key}' field.")
    return value.strip()


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SkillParseError(f"Skill frontmatter field '{key}' must be a non-empty string when provided.")
    return value.strip()


def _parse_allowed_tools(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part for part in value.split() if part)
    if isinstance(value, (list, tuple)):
        return tuple(str(part).strip() for part in value if str(part).strip())
    raise SkillParseError("Skill allowed-tools must be a space-separated string or list.")


def _parse_yaml_mapping(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return _parse_basic_yaml_mapping(text)

    try:
        parsed = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        raise SkillParseError(f"Invalid YAML frontmatter: {exc}") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise SkillParseError("YAML frontmatter must be a mapping.")
    return dict(parsed)


def _parse_basic_yaml_mapping(text: str) -> dict[str, Any]:
    """Small YAML subset fallback used when PyYAML is not installed yet."""
    result: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line.startswith((" ", "\t")):
            if current_key is None:
                raise SkillParseError("Invalid indented YAML frontmatter line.")
            stripped = raw_line.strip()
            current = result.setdefault(current_key, {})
            if stripped.startswith("- "):
                if not isinstance(current, list):
                    current = []
                    result[current_key] = current
                current.append(_unquote(stripped[2:].strip()))
            elif ":" in stripped:
                if not isinstance(current, dict):
                    raise SkillParseError("Cannot mix YAML list and mapping entries.")
                key, value = stripped.split(":", 1)
                current[key.strip()] = _unquote(value.strip())
            else:
                raise SkillParseError("Invalid YAML frontmatter line.")
            continue
        if ":" not in raw_line:
            raise SkillParseError("Invalid YAML frontmatter line.")
        key, value = raw_line.split(":", 1)
        current_key = key.strip()
        value = value.strip()
        result[current_key] = _unquote(value) if value else {}
    return result


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
