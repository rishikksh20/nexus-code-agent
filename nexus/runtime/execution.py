from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    PLAN = "plan"
    DEFAULT = "default"
    AUTO = "auto"
