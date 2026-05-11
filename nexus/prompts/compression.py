"""Compression / summarisation prompt for context compaction.

When the agent's message history grows large, an LLM-based compactor can be
called to produce a summary that replaces older messages.  This module
provides the static prompt used for that summarisation pass.

The prompt is intentionally model-agnostic: any chat model that supports a
``user`` turn can be given this text to produce a structured summary.
"""
from __future__ import annotations


def get_compression_prompt() -> str:
    """Return the system prompt used for LLM-based context compaction.

    The LLM is given the conversation history formatted as text and is
    asked to produce a concise, structured summary that preserves the
    information needed to continue the session coherently.
    """
    return """\
You are a context compaction assistant for an AI coding agent called Nexus.

Your task is to summarise the conversation history below into a concise,
structured summary that the agent can use to continue the session without
losing important context.

The summary MUST include:
1. **Goal** — what the user is trying to accomplish overall.
2. **Progress** — what has been done so far (files changed, commands run,
   decisions made).
3. **Current state** — the current state of the codebase or task (e.g.
   tests passing/failing, pending items).
4. **Constraints** — any explicit constraints or preferences the user has
   stated (style, tools to avoid, etc.).
5. **Next steps** — what the agent was about to do or what the user asked
   for most recently.

Format the summary in GitHub-flavoured Markdown.  Be as concise as possible
while preserving all information needed to continue the task.  Do NOT include
conversational filler or meta-commentary about the summary itself.
"""
