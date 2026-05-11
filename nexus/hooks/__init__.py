"""nexus.hooks — the agent hook system.

This package provides everything needed to observe, intercept, and extend
the Nexus agent runtime via an event-driven hook mechanism.

Public surface
--------------
Events
~~~~~~
:class:`HookEvent` — all events emitted by the runtime.

Payloads (typed dataclasses)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
:class:`AgentStartPayload`, :class:`AgentStopPayload`,
:class:`TurnStartPayload`, :class:`TurnEndPayload`,
:class:`UserPromptPayload`,
:class:`PreToolPayload`, :class:`PostToolPayload`,
:class:`CompactionPayload`, :class:`LoopPayload`,
:class:`StopPayload`.

Executor
~~~~~~~~
:class:`HookExecutor` — register handlers and dispatch events.
:data:`HookHandler` — type alias for async handler callables.

Integration
~~~~~~~~~~~
:func:`setup_hooks` — factory that wires the full hooks stack from config.
"""

from nexus.hooks.events import HookEvent
from nexus.hooks.executor import HookExecutor, HookHandler
from nexus.hooks.integration import setup_hooks
from nexus.hooks.payloads import (
    AgentStartPayload,
    AgentStopPayload,
    CompactionPayload,
    LoopPayload,
    PostToolPayload,
    PreToolPayload,
    StopPayload,
    TurnEndPayload,
    TurnStartPayload,
    UserPromptPayload,
)

__all__ = [
    # Events
    "HookEvent",
    # Executor
    "HookExecutor",
    "HookHandler",
    # Integration
    "setup_hooks",
    # Payloads
    "AgentStartPayload",
    "AgentStopPayload",
    "TurnStartPayload",
    "TurnEndPayload",
    "UserPromptPayload",
    "PreToolPayload",
    "PostToolPayload",
    "CompactionPayload",
    "LoopPayload",
    "StopPayload",
]
