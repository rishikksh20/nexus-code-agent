"""Context compaction and tool-output pruning.

This module provides two complementary mechanisms for keeping the agent's
message history within the model's context window:

1. **Compaction** (``ContextCompactor`` / ``compact_messages``): trims the
   *number* of messages when the estimated token count exceeds a soft limit.
   Older messages are summarised and captured in :class:`CarryOverState` so
   that key facts are not lost.

2. **Pruning** (``prune_tool_outputs``): truncates the *content* of old
   tool-result messages in place, replacing their bodies with a short
   placeholder.  Pruning fires before compaction and targets only messages
   that are outside a recency-protection window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from nexus.models import Message


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


class TokenEstimator:
    """Fast, model-agnostic token estimator.

    Uses the ``len(text) // 4`` heuristic — good enough for deciding when
    to compact; not intended for billing.
    """

    def estimate(self, text: str) -> int:
        return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Carry-over state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CarryOverState:
    """Structured facts preserved across compaction rounds.

    These are injected into the system prompt so the model retains
    high-level context even after its message window has been trimmed.
    """

    pinned_facts: list[str] = field(default_factory=list)
    summarized_history: list[str] = field(default_factory=list)
    active_constraints: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Message-list compaction
# ---------------------------------------------------------------------------


class ContextCompactor:
    """Trim message history when the estimated token count exceeds *soft_limit*.

    Parameters
    ----------
    estimator:
        :class:`TokenEstimator` used to estimate token counts.
    soft_limit:
        Trigger compaction when the total estimated tokens meets or exceeds
        this value.
    hard_limit:
        Maximum tokens to keep after compaction.  The most-recent messages
        are preserved up to this budget.
    """

    def __init__(
        self,
        estimator: TokenEstimator,
        soft_limit: int,
        hard_limit: int,
    ) -> None:
        self.estimator = estimator
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit

    def should_compact(self, messages: list[Message]) -> bool:
        total = sum(self.estimator.estimate(m.content) for m in messages)
        return total >= self.soft_limit

    def compact(
        self,
        messages: list[Message],
        carry_over: CarryOverState,
        *,
        keep_recent: int,
    ) -> tuple[list[Message], CarryOverState]:
        """Return a trimmed copy of *messages* and an updated *carry_over*.

        Messages before the safe recent boundary are summarised and stored in
        *carry_over*.  The returned list contains only messages that fit
        within *hard_limit* tokens.
        """
        recent_start = _safe_recent_start(
            messages, max(0, len(messages) - keep_recent)
        )
        recent = list(messages[recent_start:])
        older = list(messages[:recent_start])

        if older:
            carry_over.summarized_history.append(_summarize_messages(older))
            carry_over.summarized_history = carry_over.summarized_history[-5:]

        trimmed = compact_messages(
            recent,
            self.hard_limit,
            self.estimator,
            keep_recent=min(keep_recent, len(recent)),
        )
        return trimmed, carry_over


def compact_messages(
    messages: list[Message],
    max_tokens: int,
    estimator: TokenEstimator,
    *,
    keep_recent: int,
) -> list[Message]:
    """Trim *messages* so the total token estimate stays under *max_tokens*.

    Always keeps the *keep_recent* most-recent messages.  Older messages are
    added back greedily from newest to oldest as long as they fit.
    Orphaned leading tool messages (without a preceding assistant message)
    are dropped to maintain wire-format integrity.
    """
    if len(messages) <= keep_recent:
        return list(messages)

    recent_start = _safe_recent_start(
        messages, max(0, len(messages) - keep_recent)
    )
    recent = list(messages[recent_start:])
    recent_tokens = sum(estimator.estimate(m.content) for m in recent)

    if recent_tokens >= max_tokens:
        return recent

    kept_older: list[Message] = []
    running_total = recent_tokens
    for message in reversed(messages[:recent_start]):
        size = estimator.estimate(message.content)
        if running_total + size > max_tokens:
            break
        kept_older.append(message)
        running_total += size
    kept_older.reverse()
    kept_older = _drop_orphaned_leading_tools(kept_older)
    return kept_older + recent


# ---------------------------------------------------------------------------
# Tool-output pruning
# ---------------------------------------------------------------------------

_PRUNED_PLACEHOLDER = "[Tool output cleared — older than protection window]"


def prune_tool_outputs(
    messages: list[Message],
    *,
    protect_tokens: int,
    minimum_tokens: int,
) -> int:
    """Truncate old tool-result content **in place** to reclaim context space.

    Pruning only fires when the cumulative tool-result tokens *outside* the
    protection window exceed *minimum_tokens* — so small histories are left
    untouched.

    Parameters
    ----------
    messages:
        The full history list.  Modified in place.
    protect_tokens:
        Tool results totalling fewer than *protect_tokens* (counting from the
        most-recent tool result backwards) are never pruned.
    minimum_tokens:
        Do nothing unless at least *minimum_tokens* of old tool content would
        be freed.  Avoids thrashing on short histories.

    Returns
    -------
    int
        Number of messages whose content was replaced with the placeholder.
    """
    # Only prune when there are at least 2 user messages (i.e. at least one
    # completed turn already exists in history).
    user_count = sum(1 for m in messages if m.role == "user")
    if user_count < 2:
        return 0

    estimator = TokenEstimator()
    cumulative = 0
    to_prune: list[Message] = []

    for msg in reversed(messages):
        if msg.role != "tool" or not msg.tool_call_id:
            continue
        # Skip already-pruned messages.
        if msg.content == _PRUNED_PLACEHOLDER:
            break
        tokens = estimator.estimate(msg.content)
        cumulative += tokens
        if cumulative > protect_tokens:
            to_prune.append(msg)

    if not to_prune:
        return 0

    freed = sum(estimator.estimate(m.content) for m in to_prune)
    if freed < minimum_tokens:
        return 0

    for msg in to_prune:
        msg.content = _PRUNED_PLACEHOLDER
    return len(to_prune)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _summarize_messages(messages: list[Message]) -> str:
    user_count = sum(1 for m in messages if m.role == "user")
    tool_count = sum(1 for m in messages if m.role == "tool")
    assistant_count = sum(1 for m in messages if m.role == "assistant")
    last_user = next(
        (m.content for m in reversed(messages) if m.role == "user"), ""
    )
    summary = (
        f"Earlier context compacted: {len(messages)} messages "
        f"({user_count} user, {assistant_count} assistant, {tool_count} tool)."
    )
    if last_user:
        summary += f" Last compacted user task: {last_user[:120]}"
    return summary


def _safe_recent_start(messages: list[Message], proposed_start: int) -> int:
    start = max(0, min(proposed_start, len(messages)))
    while start > 0 and start < len(messages) and messages[start].role == "tool":
        start -= 1
    return start


def _drop_orphaned_leading_tools(messages: list[Message]) -> list[Message]:
    """Remove leading tool messages that have no preceding assistant message."""
    i = 0
    while i < len(messages) and messages[i].role == "tool":
        i += 1
    return messages[i:]
