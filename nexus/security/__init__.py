"""nexus.security — permission, approval, and command-risk subsystem.

This package consolidates all security-related policy logic that was previously
spread across ``nexus/runtime/permissions.py`` and inline REPL code.

Public surface
--------------
- :class:`ApprovalPolicy`   — how aggressively the agent asks for permission.
- :class:`ApprovalScope`    — how long an approval stays remembered.
- :class:`ApprovalManager`  — tracks per-turn / per-session approvals.
- :class:`RiskLevel`        — LOW / MEDIUM / HIGH / DANGEROUS classification.
- :class:`CommandClassifier`— classifies bash commands by risk level.
- :class:`PermissionDecision` / :class:`PermissionResult` / :class:`PermissionChecker`
                            — tool-level gating (mutating, path, mode).
"""

from nexus.security.policy import ApprovalPolicy
from nexus.security.classifier import CommandClassifier, RiskLevel
from nexus.security.permissions import PermissionChecker, PermissionDecision, PermissionResult
from nexus.security.manager import ApprovalManager, ApprovalScope

__all__ = [
    "ApprovalPolicy",
    "ApprovalScope",
    "ApprovalManager",
    "RiskLevel",
    "CommandClassifier",
    "PermissionChecker",
    "PermissionDecision",
    "PermissionResult",
]
