from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tomlkit import TOMLDocument, dumps, parse, table

from nexus.config.provider_profiles import ModelProfile, ProviderConfig


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
