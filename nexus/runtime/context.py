"""Backward-compatibility shim for ``nexus.runtime.context``.

All types and functions have moved to :mod:`nexus.context`.
This module re-exports them so that existing imports continue to work
without modification.
"""
# ruff: noqa: F401
from nexus.context.builder import ContextBuilder, ContextSections
from nexus.context.compactor import (
    CarryOverState,
    ContextCompactor,
    TokenEstimator,
    compact_messages,
    prune_tool_outputs,
)

__all__ = [
    "ContextSections",
    "ContextBuilder",
    "TokenEstimator",
    "CarryOverState",
    "ContextCompactor",
    "compact_messages",
    "prune_tool_outputs",
]

