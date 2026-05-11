# 03-1 — Context Compaction: Managing Long Conversations

## Prerequisites

Complete [03-session-manager.md](03-session-manager.md) first.

Every chapter adds to `self.messages`. After a session with 20+ tool calls, the message list can exceed the model's context window — resulting in API errors or silent quality degradation as the model loses sight of early context.

This chapter adds **context compaction**: a strategy to keep the conversation within bounds while preserving the information that matters.

---

## What you will build

```
agent/
    compaction.py     ← NEW: TokenEstimator, CompactionStrategy, compact_messages()
    agent.py          ← updated: check token budget before each model call
```

---

## 1. The problem: unbounded message growth

```
Turn 1:   [user] [assistant] [tool_result]            ~300 tokens
Turn 5:   [user][user][user][user][user]               ~1500 tokens
          [assistant x5][tool_result x12]
Turn 20:  [everything above + 15 more turns]          ~8000 tokens
          → model context window: 8192 tokens
          → API error or silent truncation by provider
```

The message list MUST be managed. This is not optional for production use.

---

## 2. Token estimation (without an API call)

Exact token counts require calling the tokenizer, which adds latency. A fast approximation is sufficient for compaction decisions:

```python
# agent/compaction.py

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


# ── Token estimator ───────────────────────────────────────────────────────────

class TokenEstimator:
    """
    Fast approximate token estimator — no API call required.

    Rule of thumb: 1 token ≈ 4 characters of English text.
    Structured content (JSON, code) is typically denser: ~3 chars/token.
    We use 3.5 as a conservative estimate.

    This is NOT exact, but accurate enough to trigger compaction well before
    the hard limit is reached (using 80% budget target).
    """
    CHARS_PER_TOKEN = 3.5

    def estimate_message(self, message: dict[str, Any]) -> int:
        """Estimate tokens for one serialized message dict."""
        text = json.dumps(message, ensure_ascii=False)
        return max(1, int(len(text) / self.CHARS_PER_TOKEN))

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        """Estimate total tokens for a list of message dicts."""
        return sum(self.estimate_message(m) for m in messages)

    def estimate_text(self, text: str) -> int:
        return max(1, int(len(text) / self.CHARS_PER_TOKEN))

    def within_budget(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        context_window: int,
        budget_fraction: float = 0.80,
    ) -> bool:
        """Return True if total estimated tokens are within the budget."""
        total = self.estimate_messages(messages) + self.estimate_text(system_prompt)
        budget = int(context_window * budget_fraction)
        return total <= budget
```

---

## 3. Compaction strategies

```python
# agent/compaction.py  (continued)

from agent.models import Message


@dataclass
class CompactionResult:
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    strategy_used: str


def sliding_window(
    messages: list[dict[str, Any]],
    keep_first: int = 2,
    keep_last: int = 20,
) -> list[dict[str, Any]]:
    """
    Keep the first N and last N messages, drop everything in between.

    The first messages usually contain the user's original goal (important context).
    The last messages contain recent tool results and reasoning (immediately relevant).
    The middle messages are most likely to be safely dropped.

    keep_first: number of messages to always keep from the beginning
    keep_last:  number of most-recent messages to always keep
    """
    if len(messages) <= keep_first + keep_last:
        return messages  # no compaction needed

    first = messages[:keep_first]
    last = messages[-keep_last:]
    dropped = len(messages) - keep_first - keep_last

    # Insert a marker so the model knows context was compacted
    marker = {
        "role": "user",
        "content": [{
            "type": "text",
            "text": (
                f"[Context compacted: {dropped} older messages were removed to stay within "
                f"the context window. The conversation continues from the most recent "
                f"{keep_last} messages.]"
            ),
        }],
    }
    return first + [marker] + last


def prune_tool_results(
    messages: list[dict[str, Any]],
    max_tool_result_chars: int = 500,
) -> list[dict[str, Any]]:
    """
    Truncate large tool results in older messages.

    Full tool output is often only needed for one or two turns after it arrives.
    In older messages, a digest is sufficient for the model to stay coherent.
    """
    result = []
    for msg in messages:
        content = msg.get("content", [])
        if not isinstance(content, list):
            result.append(msg)
            continue

        new_content = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
                and len(block["content"]) > max_tool_result_chars
            ):
                preview = block["content"][:max_tool_result_chars]
                new_content.append({
                    **block,
                    "content": preview + f"\n[...truncated — original was {len(block['content'])} chars]",
                })
            else:
                new_content.append(block)
        result.append({**msg, "content": new_content})
    return result


def compact_messages(
    messages: list[dict[str, Any]],
    system_prompt: str,
    context_window: int = 8192,
    budget_fraction: float = 0.80,
    keep_first: int = 2,
    keep_last: int = 20,
) -> tuple[list[dict[str, Any]], CompactionResult | None]:
    """
    Main compaction entry point.

    1. First tries tool result pruning (less destructive)
    2. Then tries sliding window if still over budget
    3. Returns (compacted_messages, CompactionResult) or (messages, None) if no action taken
    """
    estimator = TokenEstimator()

    if estimator.within_budget(messages, system_prompt, context_window, budget_fraction):
        return messages, None  # no compaction needed

    tokens_before = estimator.estimate_messages(messages)
    original_len = len(messages)

    # Step 1: prune large tool results in older messages
    pruned = prune_tool_results(messages)
    if estimator.within_budget(pruned, system_prompt, context_window, budget_fraction):
        return pruned, CompactionResult(
            messages_before=original_len,
            messages_after=len(pruned),
            tokens_before=tokens_before,
            tokens_after=estimator.estimate_messages(pruned),
            strategy_used="prune_tool_results",
        )

    # Step 2: sliding window (more aggressive)
    windowed = sliding_window(pruned, keep_first=keep_first, keep_last=keep_last)
    return windowed, CompactionResult(
        messages_before=original_len,
        messages_after=len(windowed),
        tokens_before=tokens_before,
        tokens_after=estimator.estimate_messages(windowed),
        strategy_used="sliding_window",
    )
```

---

## 4. Update `Agent.run()` to compact before each model call

```python
# agent/agent.py  — add token budget check

from agent.compaction import compact_messages, TokenEstimator, CompactionResult
from agent.events import StatusEvent

class Agent:
    def __init__(
        self,
        # ...existing params...
        context_window: int = 8192,      # ← new: model's context window size
        budget_fraction: float = 0.80,  # ← new: target utilization
    ) -> None:
        # ...existing init...
        self.context_window = context_window
        self.budget_fraction = budget_fraction

    async def run(self, user_text: str):
        self.messages.append(Message.user(user_text))
        # ...hooks...
        system_prompt = self._build_system_prompt(user_text=user_text)

        while True:
            # ── Compact if needed, BEFORE calling the model ──────────────────
            messages_as_dicts = [m.to_dict() for m in self.messages]
            compacted_dicts, result = compact_messages(
                messages_as_dicts,
                system_prompt=system_prompt,
                context_window=self.context_window,
                budget_fraction=self.budget_fraction,
            )

            if result is not None:
                # Rebuild Message objects from compacted dicts
                self.messages = [Message.from_dict(d) for d in compacted_dicts]
                yield StatusEvent(
                    message=(
                        f"Context compacted ({result.strategy_used}): "
                        f"{result.messages_before}→{result.messages_after} messages, "
                        f"~{result.tokens_before}→{result.tokens_after} tokens"
                    )
                )
                # Also update the session snapshot carry_over to note compaction
                if self._snapshot:
                    self._snapshot.carry_over["last_compaction"] = result.strategy_used

            # ── Model call (with compacted messages) ─────────────────────────
            try:
                response = await self.model_client.complete(
                    messages=self.messages,
                    tools=self.tool_registry.schemas(),
                    system_prompt=system_prompt,
                )
            except Exception as exc:
                yield ErrorEvent(message="Model call failed.", details=str(exc))
                return

            # ...rest of loop unchanged...
```

---

## 5. Set `context_window` correctly per model

```python
# main.py  — model-specific context windows

CONTEXT_WINDOWS = {
    "gpt-4o":           128_000,
    "gpt-4o-mini":       16_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-haiku":    200_000,
    "llama-3.1-8b":       32_000,
    "gemini-1.5-pro":  1_000_000,
}

model_name = args.model or "gpt-4o"
context_window = CONTEXT_WINDOWS.get(model_name, 8192)

agent = Agent(
    # ...other params...
    context_window=context_window,
    budget_fraction=0.80,
)
```

---

## 6. See compaction in action

After a long session:

```
you> [after many tool calls]
  · Thinking... (turn 15)
  · Context compacted (sliding_window): 67→24 messages, ~6890→2100 tokens
  ⚙ read_file(...)
  ✓ read_file → ...
```

The model continues seamlessly — it sees the compaction marker and understands it is working from a summarized view.

---

## 7. What compaction does NOT solve

- **Forgetting early context** — sliding window drops middle messages. If the user said something important in turn 3, it may be gone.
  - **Fix:** use `carry_over` to preserve key facts before compacting
- **Streaming mid-generation** — do not compact while the model is generating
  - **Fix:** always compact at the start of the loop, before the model call
- **Tool results that must survive** — if a tool's output is referenced 10 turns later, pruning truncates it
  - **Fix:** save critical tool outputs to memory (`save_memory` tool) before they age out

---

## 8. Checklist before moving on

- [ ] `TokenEstimator.within_budget()` uses a 80% fraction target by default
- [ ] `prune_tool_results()` truncates large tool outputs in older messages, not recent ones
- [ ] `sliding_window()` inserts a compaction marker message so the model knows context was dropped
- [ ] `compact_messages()` tries pruning first, then sliding window
- [ ] Compaction happens in `Agent.run()` before each model call, not after
- [ ] A `StatusEvent` is emitted when compaction fires (so the user sees it in the REPL)
- [ ] `Agent` accepts `context_window` and `budget_fraction` as parameters
- [ ] `context_window` is set per model, not hardcoded to 8192

---

Next: [04-hooks.md](04-hooks.md) — add lifecycle extension points to the agent loop.

