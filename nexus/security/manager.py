"""Approval state manager — tracks which tools have been approved and at what scope.

``ApprovalScope`` controls how long a user approval is remembered:

* ``once``    — approved for a single invocation (the default).
* ``turn``    — approved for the duration of the current user turn.
* ``session`` — approved for the entire conversation session.

``ApprovalManager`` is the stateful companion to :class:`PermissionChecker`.
It maintains per-turn and per-session approval sets and exposes the helpers
needed by the REPL and headless runner to drive the confirmation loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from nexus.security.policy import ApprovalPolicy


class ApprovalScope(str, Enum):
    ONCE = "once"
    TURN = "turn"
    SESSION = "session"


@dataclass
class ApprovalManager:
    """Track approval state across turns and sessions.

    Parameters
    ----------
    policy:
        The active :class:`ApprovalPolicy`.  Drives
        :meth:`should_auto_approve` and :meth:`get_approved_set`.
    """

    policy: ApprovalPolicy = ApprovalPolicy.ON_REQUEST
    _session_approved: set[str] = field(default_factory=set, repr=False)
    _turn_approved: set[str] = field(default_factory=set, repr=False)

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def begin_turn(self) -> None:
        """Clear turn-scoped approvals.

        Call this at the start of each new user message so that tools
        approved with ``APPROVE_TURN`` scope are forgotten.
        """
        if self.policy is not ApprovalPolicy.APPROVE_SESSION:
            self._turn_approved.clear()

    # ------------------------------------------------------------------
    # Approval recording
    # ------------------------------------------------------------------

    def record_approval(self, tool_name: str, scope: ApprovalScope) -> None:
        """Record that *tool_name* was approved with the given *scope*."""
        # ONCE, TURN, and SESSION all add to _turn_approved so that the
        # current iteration loop can proceed without another prompt.
        self._turn_approved.add(tool_name)
        if scope is ApprovalScope.SESSION:
            self._session_approved.add(tool_name)

    def record_approval_once(self, tool_name: str) -> None:
        """Record a one-time approval (for a single retry iteration)."""
        self._turn_approved.add(tool_name)

    # ------------------------------------------------------------------
    # Approval queries
    # ------------------------------------------------------------------

    def is_pre_approved(self, tool_name: str) -> bool:
        """Return ``True`` if *tool_name* is currently pre-approved.

        A tool is pre-approved when it has been explicitly approved at
        session or turn scope by the user.
        """
        return (
            tool_name in self._session_approved
            or tool_name in self._turn_approved
        )

    def should_auto_approve(self, tool_name: str, risk_level: str = "medium") -> bool:
        """Return ``True`` if the policy says to skip the confirmation prompt.

        This is consulted *before* the agent checks ``PermissionChecker``.
        """
        if self.policy is ApprovalPolicy.PLAN:
            return False
        if self.policy is ApprovalPolicy.AUTO:
            # Auto-approve low and medium risk without asking; still ask for high.
            return risk_level in {"low", "medium"}
        # For all scoped policies: auto-approve only if already approved.
        return self.is_pre_approved(tool_name)

    def get_approved_set(self) -> set[str]:
        """Return the union of session- and turn-approved tools.

        Pass this to ``Agent.run(approved_tools=...)`` to signal which
        tools the agent may execute without another confirmation event.
        """
        return self._session_approved | self._turn_approved
