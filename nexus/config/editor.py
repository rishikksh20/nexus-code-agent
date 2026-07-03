from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from tomlkit import TOMLDocument, dumps, parse, table

from nexus.config.provider_profiles import ModelProfile, ProviderConfig


_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def save_provider(path: Path, provider: ProviderConfig) -> None:
    update_provider_fields(path, provider.name, provider.to_dict())


def update_provider_fields(path: Path, name: str, fields: dict[str, Any]) -> None:
    document = _load_document(path)
    providers = _table(document, "providers")
    current = _nested_table(providers, name)
    _update_mapping(current, fields)
    _write_document(path, document)


def save_model_profile(path: Path, profile: ModelProfile) -> None:
    update_model_profile_fields(path, profile.name, profile.to_dict())


def update_model_profile_fields(path: Path, name: str, fields: dict[str, Any]) -> None:
    document = _load_document(path)
    models = _table(document, "models")
    current = _nested_table(models, name)
    _update_mapping(current, fields)
    _write_document(path, document)


def delete_model_profile(path: Path, name: str) -> None:
    document = _load_document(path)
    models = document.get("models")
    if isinstance(models, dict):
        models.pop(name, None)
    _write_document(path, document)


def set_active_model_profile(path: Path, name: str) -> None:
    document = _load_document(path)
    document["active_model_profile"] = name
    _write_document(path, document)


def update_top_level(path: Path, key: str, value: Any) -> None:
    document = _load_document(path)
    document[key] = value
    _write_document(path, document)


def update_dotenv_value(path: Path, key: str, value: str) -> None:
    key = key.strip()
    if not _ENV_KEY_RE.fullmatch(key):
        raise ValueError("Environment key must be a valid variable name.")
    if "\n" in value or "\r" in value:
        raise ValueError("Environment value must be a single line.")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={_format_dotenv_value(value)}"
    updated = False
    next_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            current_key, _, _ = stripped.partition("=")
            if current_key.strip() == key:
                if not updated:
                    next_lines.append(replacement)
                    updated = True
                continue
        next_lines.append(line)
    if not updated:
        next_lines.append(replacement)
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def remove_top_level(path: Path, key: str) -> None:
    document = _load_document(path)
    document.pop(key, None)
    _write_document(path, document)


def _load_document(path: Path) -> TOMLDocument:
    if not path.exists():
        return TOMLDocument()
    return parse(path.read_text(encoding="utf-8"))


def _table(parent: Any, key: str) -> Any:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = table()
        parent[key] = value
    return value


def _nested_table(parent: Any, key: str) -> Any:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = table()
        parent[key] = value
    return value


def _update_mapping(target: Any, fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict):
            _update_mapping(_nested_table(target, key), value)
        else:
            target[key] = value


def _write_document(path: Path, document: TOMLDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(dumps(document), encoding="utf-8")
    temporary.replace(path)


def _format_dotenv_value(value: str) -> str:
    if value and all(char not in value for char in " \t#'\""):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
