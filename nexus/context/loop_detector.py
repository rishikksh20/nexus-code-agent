"""Loop detection for the agent runtime.

:class:`LoopDetector` tracks recent agent actions and raises a flag when
repetitive behaviour is detected — either an exact repeat of the same action
or a short cycle pattern.

This is groundwork for an automatic loop-breaker; the caller is responsible
for acting on the returned warning string (e.g. injecting a corrective prompt).
"""
from __future__ import annotations

from collections import deque


class LoopDetector:
    """Track recent agent actions and detect repetition.

    Parameters
    ----------
    max_history:
        Number of past actions to keep in the rolling window.
    max_exact_repeats:
        Maximum number of times the *same* action signature may appear
        consecutively before a loop is reported.
    max_cycle_length:
        Maximum cycle length (in actions) to search for repeating patterns.
    """

    def __init__(
        self,
        *,
        max_history: int = 20,
        max_exact_repeats: int = 3,
        max_cycle_length: int = 3,
    ) -> None:
        self._history: deque[str] = deque(maxlen=max_history)
        self.max_exact_repeats = max_exact_repeats
        self.max_cycle_length = max_cycle_length

    def record_action(self, action_type: str, **details: object) -> None:
        """Record an action performed by the agent.

        The action is serialised as ``"type:key=value,..."`` for comparison.
        """
        detail_str = ",".join(f"{k}={v}" for k, v in sorted(details.items()))
        signature = f"{action_type}:{detail_str}" if detail_str else action_type
        self._history.append(signature)

    def check_for_loop(self) -> str | None:
        """Return a warning string if a loop is detected, else ``None``.

        Two detection strategies are applied:

        1. **Exact repeats** — the last N entries are all the same signature.
        2. **Short cycles** — a repeating pattern of length 2–*max_cycle_length*
           is found at the tail of the history.
        """
        history = list(self._history)
        if not history:
            return None

        # --- exact-repeat detection ---
        if len(history) >= self.max_exact_repeats:
            tail = history[-self.max_exact_repeats :]
            if len(set(tail)) == 1:
                return (
                    f"Detected loop: action '{tail[-1]}' repeated "
                    f"{self.max_exact_repeats} times in a row."
                )

        # --- short-cycle detection ---
        for cycle_len in range(2, self.max_cycle_length + 1):
            needed = cycle_len * 2
            if len(history) >= needed:
                tail_a = history[-needed : -cycle_len]
                tail_b = history[-cycle_len:]
                if tail_a == tail_b:
                    return (
                        f"Detected cycle of length {cycle_len}: "
                        + " -> ".join(tail_b)
                        + " (repeating)."
                    )

        return None

    def reset(self) -> None:
        """Clear the action history (e.g. at the start of a new turn)."""
        self._history.clear()
