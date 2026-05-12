from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Skill:
    name: str
    description: str
    content: str
    tags: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    source: str = "local"