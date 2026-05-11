"""Hook events catalogue for the Nexus agent.

All events that the runtime emits are declared here as members of
:class:`HookEvent`.  Consumers register handlers against these events via
:class:`~nexus.hooks.executor.HookExecutor`.

Event groups
------------
* **Lifecycle** — coarse-grained agent / turn boundaries.
* **User interaction** — user-facing prompt submission.
* **Tool lifecycle** — fired before and after each tool execution.
* **Context management** — compaction and loop-detection events.
* **General** — ``STOP`` and ``NOTIFICATION`` carry miscellaneous payloads.
"""
from __future__ import annotations

from enum import Enum


class HookEvent(str, Enum):
    """Enumeration of all hook events emitted by the Nexus runtime.

    Each member is a stable, lowercase string value used as the event key
    in payloads and log records.
    """

    # --- Lifecycle -----------------------------------------------------------
    #: Fired once when the agent is initialised and ready to process turns.
    AGENT_START = "agent_start"
    #: Fired once when the agent shuts down (REPL exit or headless end).
    AGENT_STOP = "agent_stop"
    #: Fired at the start of each user-turn processing loop.
    TURN_START = "turn_start"
    #: Fired after all events for a turn have been collected.
    TURN_END = "turn_end"

    # --- User interaction ----------------------------------------------------
    #: Fired when a user prompt is submitted for processing.
    USER_PROMPT_SUBMIT = "user_prompt_submit"

    # --- Tool lifecycle ------------------------------------------------------
    #: Fired immediately before a tool is invoked.
    PRE_TOOL_USE = "pre_tool_use"
    #: Fired immediately after a tool returns (success or error).
    POST_TOOL_USE = "post_tool_use"

    # --- Context management --------------------------------------------------
    #: Fired when context compaction removes older messages.
    CONTEXT_COMPACTION = "context_compaction"
    #: Fired when the loop detector identifies a repetition pattern.
    LOOP_DETECTED = "loop_detected"

    # --- General -------------------------------------------------------------
    #: Fired when the turn ends cleanly (replaces / supersedes AGENT_STOP in
    #: single-turn headless mode).
    STOP = "stop"
    #: Catch-all notification for sub-events such as model usage, tool denial,
    #: confirmation requests, and clarification prompts.
    NOTIFICATION = "notification"
