from __future__ import annotations

from nexus.models import Message
from nexus.runtime.context import CarryOverState, ContextCompactor, TokenEstimator, compact_messages


def test_compact_messages_keeps_assistant_with_tool_results_at_recent_boundary():
    messages = [
        Message(role="user", content="older"),
        Message(role="assistant", content="calling tool"),
        Message(role="tool", content="tool output"),
        Message(role="user", content="latest"),
    ]

    compacted = compact_messages(messages, max_tokens=100, estimator=TokenEstimator(), keep_recent=2)

    roles = [message.role for message in compacted]
    tool_index = roles.index("tool")
    assert compacted[tool_index - 1].role == "assistant"
    assert roles[-2:] == ["tool", "user"]


def test_compact_messages_drops_orphaned_tool_when_budget_cannot_fit_assistant_pair():
    messages = [
        Message(role="user", content="older"),
        Message(role="assistant", content="calling tool"),
        Message(role="tool", content="tool output"),
        Message(role="user", content="latest"),
    ]

    compacted = compact_messages(messages, max_tokens=2, estimator=TokenEstimator(), keep_recent=1)

    assert [message.role for message in compacted] == ["user"]


def test_context_compactor_summarizes_only_messages_before_safe_recent_boundary():
    messages = [
        Message(role="user", content="older request"),
        Message(role="assistant", content="calling tool"),
        Message(role="tool", content="first result"),
        Message(role="tool", content="second result"),
        Message(role="user", content="latest request"),
    ]
    carry_over = CarryOverState()
    compactor = ContextCompactor(TokenEstimator(), soft_limit=1, hard_limit=100)

    compacted, updated = compactor.compact(messages, carry_over, keep_recent=2)

    assert [message.role for message in compacted] == ["assistant", "tool", "tool", "user"]
    assert updated.summarized_history
    assert "1 messages" in updated.summarized_history[-1]


