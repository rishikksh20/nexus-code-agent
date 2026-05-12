"""Compression / summarisation prompt for context compaction.

When the agent's message history grows large, an LLM-based compactor can be
called to produce a structured continuation summary that replaces older
messages.  This module provides the static prompt used for that pass.

The prompt follows the strategy from ``reference_code/core/prompts/system.py``
— it produces a seven-section structured summary that allows seamless session
continuation without redoing completed work.
"""
from __future__ import annotations


def get_compression_prompt() -> str:
    """Return the system prompt used for LLM-based context compaction.

    The LLM is given the conversation history and is asked to produce a
    structured continuation prompt that a new session (without access to
    the full history) can use to pick up exactly where the previous one
    left off.
    """
    return """\
Provide a detailed continuation prompt for resuming this work. \
The new session will NOT have access to our conversation history.

IMPORTANT: Structure your response EXACTLY as follows:

## ORIGINAL GOAL
[State the user's original request / goal in one paragraph.]

## COMPLETED ACTIONS (DO NOT REPEAT THESE)
[List specific actions that are DONE and must NOT be repeated. \
Be specific: include file paths, function names, and a description of each change made. \
Use bullet points.]

## CURRENT STATE
[Describe the current state of the codebase or project after all completed actions. \
What files exist? What has been modified? What is the current build / test status?]

## IN-PROGRESS WORK
[What was being worked on when the context limit was reached? \
Describe any partial changes or open edits.]

## REMAINING TASKS
[What still needs to be done to complete the original goal? \
Be specific — list each outstanding item.]

## NEXT STEP
[What is the immediate next action to take? \
Be very specific — this is the first thing the new agent session should do.]

## KEY CONTEXT
[Any important decisions, constraints, user preferences, technical assumptions, \
or architectural choices that must persist into the new session.]

Be extremely specific with file paths and function names. \
The goal is to allow seamless continuation without redoing any completed work."""
