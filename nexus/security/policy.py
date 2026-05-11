"""Approval policy — controls *when* the agent asks for user permission.

Policies
--------
``on-request``
    Ask every time a mutating or dangerous tool is invoked.  This is the
    safest (and default) mode.

``approve-turn``
    Once the user approves a tool within a turn, do not ask again for the
    same tool until the next user message.

``approve-session``
    Once the user approves a tool, remember it for the lifetime of the
    session — never ask again.

``auto``
    Auto-approve tools with LOW or MEDIUM risk; always confirm HIGH/DANGEROUS
    tools.  Corresponds to the agent's ``auto`` execution mode.

``plan``
    Never execute mutating tools — read-only mode.  Any tool flagged as
    mutating is silently blocked.
"""
from __future__ import annotations

from enum import Enum


class ApprovalPolicy(str, Enum):
    ON_REQUEST = "on-request"
    APPROVE_TURN = "approve-turn"
    APPROVE_SESSION = "approve-session"
    AUTO = "auto"
    PLAN = "plan"

    @classmethod
    def _missing_(cls, value: object) -> "ApprovalPolicy":
        """Case-insensitive and alias resolution."""
        if not isinstance(value, str):
            return NotImplemented  # type: ignore[return-value]
        normalised = value.lower().replace("_", "-")
        for member in cls:
            if member.value == normalised:
                return member
        return NotImplemented  # type: ignore[return-value]
