"""nexus.context — context management, compaction, pruning, and loop detection.

All types previously in ``nexus/runtime/context.py`` now live here.
``nexus/runtime/context.py`` is a backward-compat shim.

Public surface
--------------
- :class:`ContextSections`   — structured container for system-prompt sections.
- :class:`ContextBuilder`    — converts ``ContextSections`` to a prompt string.
- :class:`TokenEstimator`    — rough token-count estimator (len // 4).
- :class:`CarryOverState`    — pinned facts / summaries carried across compactions.
- :class:`ContextCompactor`  — trims message history when token budget is exceeded.
- :func:`compact_messages`   — low-level message-list trimmer.
- :func:`prune_tool_outputs` — in-place truncation of old tool-result content.
- :class:`LoopDetector`      — detects agent repetition / cycles.
"""

from nexus.context.builder import ContextBuilder, ContextSections
from nexus.context.compactor import (
    CarryOverState,
    ContextCompactor,
    TokenEstimator,
    compact_messages,
    prune_tool_outputs,
)
from nexus.context.loop_detector import LoopDetector

__all__ = [
    "ContextSections",
    "ContextBuilder",
    "TokenEstimator",
    "CarryOverState",
    "ContextCompactor",
    "compact_messages",
    "prune_tool_outputs",
    "LoopDetector",
]
