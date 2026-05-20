from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class Skill:
    name: str
    description: str
    content: str
    root_dir: Path | None = None
    skill_path: Path | None = None
    tags: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    source: str = "local"
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    is_valid: bool = True
    error: str | None = None
