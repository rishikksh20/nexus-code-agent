"""Advisory supervisor-route helpers used by prompts and tests.

This module intentionally does not schedule sub-agents. It gives the runtime a
deterministic, inspectable hint that the supervisor prompt can use while the
agent loop remains event-driven.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


TINY_READ_ONLY_TOOL_BUDGET = 3


class SupervisorRoute(str, Enum):
    DIRECT_READ_ONLY = "direct_read_only"
    PLANNING_ANALYSIS = "planning_analysis"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class SupervisorRoutingDecision:
    route: SupervisorRoute
    reason: str
    direct_tool_budget: int = TINY_READ_ONLY_TOOL_BUDGET


_MUTATION_WORDS = frozenset(
    {
        "add",
        "change",
        "create",
        "delete",
        "edit",
        "fix",
        "implement",
        "modify",
        "patch",
        "refactor",
        "remove",
        "rename",
        "update",
        "write",
    }
)
_REVIEW_WORDS = frozenset({"review", "audit", "inspect diff", "find bugs"})
_IMPACT_WORDS = frozenset({"impact", "blast radius", "affected", "verification scope", "test scope"})
_READ_ONLY_WORDS = frozenset({"explain", "summarize", "read", "inspect", "find", "search", "where"})


def classify_supervisor_route(task_text: str, *, estimated_tool_calls: int | None = None) -> SupervisorRoutingDecision:
    """Return the default supervisor route for a user task.

    This is deliberately a small deterministic helper, not a scheduler. It
    gives prompts, tests, and future UI surfaces one shared vocabulary for the
    supervisor-first contract while the LLM still performs the actual routing.
    """

    text = " ".join(str(task_text or "").lower().split())
    estimated = estimated_tool_calls if estimated_tool_calls is not None else TINY_READ_ONLY_TOOL_BUDGET + 1

    if any(word in text for word in _REVIEW_WORDS):
        return SupervisorRoutingDecision(SupervisorRoute.REVIEW, "Task asks for review or bug-finding.")
    if any(word in text for word in _IMPACT_WORDS):
        return SupervisorRoutingDecision(SupervisorRoute.VERIFICATION, "Task asks for impact or verification scope.")
    if any(word in text.split() for word in _MUTATION_WORDS):
        return SupervisorRoutingDecision(SupervisorRoute.EXECUTION, "Task appears to require workspace mutation.")
    if any(word in text.split() for word in _READ_ONLY_WORDS) and estimated <= TINY_READ_ONLY_TOOL_BUDGET:
        return SupervisorRoutingDecision(SupervisorRoute.DIRECT_READ_ONLY, "Tiny read-only task fits supervisor budget.")
    return SupervisorRoutingDecision(SupervisorRoute.PLANNING_ANALYSIS, "Read-only task exceeds tiny supervisor budget.")


def supervisor_routing_guidance_lines() -> tuple[str, ...]:
    return (
        f"- Tiny read-only budget: do the work directly only when it fits about {TINY_READ_ONLY_TOOL_BUDGET} simple read-only tool calls or fewer.",
        "- Execution route: delegate any workspace mutation to `subagent_execution`; the supervisor should not directly edit files in advanced mode.",
        "- Simple known-target implementation: route directly to `subagent_execution` with file hints and a minimal read budget; do not run separate planning first.",
        "- Planning-analysis route: delegate bounded read-only exploration once the tiny budget is exceeded.",
        "- Verification route: call `subagent_verification` when affected files, public interfaces, risk, or verification scope are unclear.",
        "- Review route: call `subagent_review` for post-change review, scoped verification, and failure attribution.",
    )


def supervisor_routing_hint_line(task_text: str, *, estimated_tool_calls: int | None = None) -> str:
    decision = classify_supervisor_route(task_text, estimated_tool_calls=estimated_tool_calls)
    return (
        "Supervisor routing hint (advisory, not an execution scheduler): "
        f"{decision.route.value} route. {decision.reason} "
        f"Direct read-only budget: {decision.direct_tool_budget}."
    )
