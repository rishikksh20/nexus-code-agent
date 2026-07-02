from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext
from nexus.tools.base import ToolKind


def parallel_tool_execution_enabled(context: ToolExecutionContext) -> bool:
    """Return whether this run context allows parallel tool scheduling."""
    value = context.metadata.get(
        "parallel_tool_execution_enabled",
        context.metadata.get("parallel_tools", True),
    )
    return _bool_metadata(value, default=True)


def tool_call_can_run_in_parallel(record: Any, context: ToolExecutionContext) -> bool:
    """Return whether a prepared tool record is safe for parallel execution."""
    if not parallel_tool_execution_enabled(context):
        return False
    tool = getattr(record, "tool", None)
    if tool is None or getattr(tool, "is_mutating", False):
        return False
    name = str(getattr(record, "name", ""))
    if name == "delegate_task" or name.startswith("subagent_"):
        return False
    return getattr(tool, "kind", None) not in {ToolKind.AGENT, ToolKind.USER_INPUT}


def _bool_metadata(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)
