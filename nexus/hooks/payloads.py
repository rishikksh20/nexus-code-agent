"""Typed payload dataclasses for each :class:`~nexus.hooks.events.HookEvent`.

All payloads are plain :func:`dataclasses.dataclass` instances.  The
:class:`~nexus.hooks.executor.HookExecutor` serialises them to
``dict[str, Any]`` via :func:`dataclasses.asdict` before calling handlers,
so **existing handlers that accept raw dicts continue to work unchanged**.

Typed payloads are optional: callers may pass a plain ``dict`` to
:meth:`~nexus.hooks.executor.HookExecutor.emit` and it will be forwarded
directly.

Quick reference
---------------

=========================  ===================================================
Payload class              Matching event
=========================  ===================================================
:class:`AgentStartPayload` :attr:`~HookEvent.AGENT_START`
:class:`AgentStopPayload`  :attr:`~HookEvent.AGENT_STOP`
:class:`TurnStartPayload`  :attr:`~HookEvent.TURN_START`
:class:`TurnEndPayload`    :attr:`~HookEvent.TURN_END`
:class:`UserPromptPayload` :attr:`~HookEvent.USER_PROMPT_SUBMIT`
:class:`PreToolPayload`    :attr:`~HookEvent.PRE_TOOL_USE`
:class:`PostToolPayload`   :attr:`~HookEvent.POST_TOOL_USE`
:class:`CompactionPayload` :attr:`~HookEvent.CONTEXT_COMPACTION`
:class:`LoopPayload`       :attr:`~HookEvent.LOOP_DETECTED`
:class:`StopPayload`       :attr:`~HookEvent.STOP`
=========================  ===================================================
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentStartPayload:
    """Emitted once when the agent is fully initialised."""

    session_id: str | None = None
    mode: str | None = None
    provider: str | None = None


@dataclass(slots=True)
class AgentStopPayload:
    """Emitted once when the agent runtime is shutting down."""

    session_id: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class TurnStartPayload:
    """Emitted at the beginning of each user-turn processing loop."""

    session_id: str | None = None
    turn_id: str | None = None
    turn_number: int = 0


@dataclass(slots=True)
class TurnEndPayload:
    """Emitted after all events for a turn have been collected."""

    session_id: str | None = None
    turn_id: str | None = None
    message_count: int = 0
    duration_ms: float = 0.0
    status: str = "completed"


# ---------------------------------------------------------------------------
# User interaction
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UserPromptPayload:
    """Emitted when a user prompt is submitted for processing."""

    prompt: str
    session_id: str | None = None
    turn_id: str | None = None
    mode: str | None = None


# ---------------------------------------------------------------------------
# Tool lifecycle
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PreToolPayload:
    """Emitted immediately before a tool is invoked."""

    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_source: str = "builtin"
    is_mutating: bool = False
    session_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None


@dataclass(slots=True)
class PostToolPayload:
    """Emitted immediately after a tool returns (success or error)."""

    tool_name: str
    tool_call_id: str
    output: str
    duration_ms: float
    tool_source: str = "builtin"
    is_mutating: bool = False
    is_error: bool = False
    arguments: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    turn_id: str | None = None
    trace_id: str | None = None


# ---------------------------------------------------------------------------
# Context management
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CompactionPayload:
    """Emitted when context compaction removes older messages."""

    messages_before: int
    messages_after: int
    session_id: str | None = None
    turn_id: str | None = None


@dataclass(slots=True)
class LoopPayload:
    """Emitted when the loop detector identifies a repetition pattern."""

    warning: str
    session_id: str | None = None
    turn_id: str | None = None


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StopPayload:
    """Emitted when a turn ends cleanly (headless or REPL)."""

    session_id: str | None = None
    turn_id: str | None = None
    message_count: int = 0
