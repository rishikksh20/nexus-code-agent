"""Approval state manager — tracks which tool invocations have been approved.

``ApprovalScope`` controls how long a user approval is remembered:

* ``once``    — approved for a single invocation (the default).
* ``turn``    — approved for the duration of the current user turn.
* ``session`` — approved for the entire conversation session.

``ApprovalManager`` is the stateful companion to :class:`PermissionChecker`.
It remembers approvals by *tool name + normalized arguments* so that approving
one mutating call (for example ``write_file`` on ``calculator.py``) never
implicitly approves a later distinct call (for example ``write_file`` on
``logging_calculator.py``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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
    _once_approved: dict[str, int] = field(default_factory=dict, repr=False)
    _turn_mutating_approved: bool = field(default=False, repr=False)
    _turn_refused: set[str] = field(default_factory=set, repr=False)
    # Normalised (case-folded + whitespace-collapsed) once-approvals.
    # The agent re-runs from scratch after user approval, so the LLM may
    # regenerate the same tool call with minor formatting differences (e.g.
    # "rishikesh" → "Rishikesh").  Storing a normalised key means a single
    # "yes" never prompts twice for semantically-identical calls.  Structural
    # differences (different paths, keys, commands) still produce different
    # normalised keys, so cross-invocation security is preserved.
    _once_normalized: dict[str, int] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def begin_turn(self) -> None:
        """Clear turn-scoped approvals.

        Call this at the start of each new user message so that tools
        approved with ``APPROVE_TURN`` scope are forgotten.
        """
        self._once_approved.clear()
        self._turn_approved.clear()
        self._turn_mutating_approved = False
        self._turn_refused.clear()
        self._once_normalized.clear()

    # ------------------------------------------------------------------
    # Approval recording
    # ------------------------------------------------------------------

    def record_approval(
        self,
        tool_name: str,
        scope: ApprovalScope,
        *,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Record that a specific tool invocation was approved with *scope*."""
        key = _approval_key(tool_name, arguments)
        if scope is ApprovalScope.ONCE:
            self._once_approved[key] = self._once_approved.get(key, 0) + 1
            # Also store a normalised (case-folded) copy so that a re-run of
            # the agent that regenerates the same call with minor formatting
            # differences (e.g. different capitalisation of a value) doesn't
            # prompt the user a second time.
            norm_key = _normalized_approval_key(tool_name, arguments)
            self._once_normalized[norm_key] = self._once_normalized.get(norm_key, 0) + 1
            return
        self._turn_approved.add(key)
        if scope is ApprovalScope.SESSION:
            self._session_approved.add(key)

    def record_approval_once(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Record a one-time approval (for a single retry iteration)."""
        self.record_approval(tool_name, ApprovalScope.ONCE, arguments=arguments)

    def record_turn_wide_mutating_approval(self) -> None:
        """Approve non-dangerous confirmable tool calls for the current user turn."""
        self._turn_mutating_approved = True

    def record_refusal(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Remember that the user refused this invocation for the current turn."""
        self._turn_refused.add(_approval_key(tool_name, arguments))

    # ------------------------------------------------------------------
    # Approval queries
    # ------------------------------------------------------------------

    def is_pre_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        """Return ``True`` if *tool_name* is currently pre-approved.

        A tool invocation is pre-approved when its normalized signature has been
        explicitly approved at session or turn scope by the user, OR when a
        case-/whitespace-normalised once-approval exists (covering re-runs where
        the LLM may regenerate the same tool call with subtly different
        formatting, e.g. different capitalisation of a value).
        """
        once_key = _approval_key(tool_name, arguments)
        return (
            once_key in self._session_approved
            or once_key in self._turn_approved
            or self._once_approved.get(once_key, 0) > 0
            or self._once_normalized.get(_normalized_approval_key(tool_name, arguments), 0) > 0
        )

    def consume_approval(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Consume a one-time approval after the matching invocation executes."""
        key = _approval_key(tool_name, arguments)
        remaining = self._once_approved.get(key, 0)
        if remaining > 0:
            if remaining == 1:
                self._once_approved.pop(key, None)
            else:
                self._once_approved[key] = remaining - 1
        # Also consume the normalised entry so that subsequent invocations of
        # the same tool with the same normalised arguments are not silently
        # auto-approved after the matched execution.
        norm_key = _normalized_approval_key(tool_name, arguments)
        norm_remaining = self._once_normalized.get(norm_key, 0)
        if norm_remaining > 0:
            if norm_remaining == 1:
                self._once_normalized.pop(norm_key, None)
            else:
                self._once_normalized[norm_key] = norm_remaining - 1

    def is_turn_wide_mutating_preapproved(
        self,
        tool_name: str,
        *,
        is_mutating: bool,
        risk_level: str = "medium",
    ) -> bool:
        """Return ``True`` when the current turn has blanket per-turn approval.

        Turn-wide approvals intentionally do not cover high/dangerous shell
        commands, which must always be confirmed per invocation. Callers only
        consult this helper for tools that would otherwise prompt.
        """
        del is_mutating
        if not self._turn_mutating_approved:
            return False
        return not _requires_fresh_turn_approval(tool_name, risk_level)

    def is_refused(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        """Return ``True`` when the current turn already refused this invocation."""
        return _approval_key(tool_name, arguments) in self._turn_refused

    def should_auto_approve(
        self,
        tool_name: str,
        risk_level: str = "medium",
        *,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        """Return ``True`` if the policy says to skip the confirmation prompt.

        This is consulted *before* the agent checks ``PermissionChecker``.
        """
        if self.policy is ApprovalPolicy.PLAN:
            return False
        if self.policy is ApprovalPolicy.AUTO:
            # Auto-approve low and medium risk without asking; still ask for high.
            return risk_level in {"low", "medium"}
        # For all scoped policies: auto-approve only if already approved.
        return self.is_pre_approved(tool_name, arguments)

    def get_approved_set(self) -> set[str]:
        """Return the union of session- and turn-approved invocation keys.

        Legacy callers may pass this to ``Agent.run(approved_tools=...)``.
        """
        return self._session_approved | self._turn_approved


def _approval_key(tool_name: str, arguments: dict[str, Any] | None) -> str:
    normalised_arguments = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return f"{tool_name}:{normalised_arguments}"


def _normalized_approval_key(tool_name: str, arguments: dict[str, Any] | None) -> str:
    """Return a case-folded, whitespace-collapsed approval key.

    Used as a secondary lookup so that the LLM re-generating a tool call with
    minor formatting differences (e.g. ``"rishikesh"`` vs ``"Rishikesh"``) is
    treated as the same invocation.  Structural differences (different paths,
    keys, commands) still produce distinct normalised keys.
    """
    raw = _approval_key(tool_name, arguments)
    return " ".join(raw.lower().split())


def _requires_fresh_turn_approval(tool_name: str, risk_level: str) -> bool:
    normalised_risk = str(risk_level).strip().lower().split(".")[-1]
    return tool_name == "bash" and normalised_risk in {"high", "dangerous"}


