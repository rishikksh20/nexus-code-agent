"""Backward-compatibility shim.

All permission logic has moved to :mod:`nexus.security`.  Import from there
for new code; this module re-exports the public symbols so that existing
imports continue to work without changes.
"""
from nexus.security.permissions import (  # noqa: F401
    PermissionChecker,
    PermissionDecision,
    PermissionResult,
)

__all__ = ["PermissionChecker", "PermissionDecision", "PermissionResult"]
