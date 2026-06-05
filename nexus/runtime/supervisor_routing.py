from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


TINY_READ_ONLY_TOOL_BUDGET = 3


class SupervisorRoute(str, Enum):
    DIRECT_READ_ONLY = "direct_read_only"
    EXPLORER = "explorer"
    CODING = "coding"
    IMPACT_ANALYZER = "impact_analyzer"
    CODE_REVIEWER = "code_reviewer"


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
        return SupervisorRoutingDecision(SupervisorRoute.CODE_REVIEWER, "Task asks for review or bug-finding.")
    if any(word in text for word in _IMPACT_WORDS):
        return SupervisorRoutingDecision(SupervisorRoute.IMPACT_ANALYZER, "Task asks for impact or verification scope.")
    if any(word in text.split() for word in _MUTATION_WORDS):
        return SupervisorRoutingDecision(SupervisorRoute.CODING, "Task appears to require workspace mutation.")
    if any(word in text.split() for word in _READ_ONLY_WORDS) and estimated <= TINY_READ_ONLY_TOOL_BUDGET:
        return SupervisorRoutingDecision(SupervisorRoute.DIRECT_READ_ONLY, "Tiny read-only task fits supervisor budget.")
    return SupervisorRoutingDecision(SupervisorRoute.EXPLORER, "Read-only task exceeds tiny supervisor budget.")


def supervisor_routing_guidance_lines() -> tuple[str, ...]:
    return (
        f"- Tiny read-only budget: do the work directly only when it fits about {TINY_READ_ONLY_TOOL_BUDGET} simple read-only tool calls or fewer.",
        "- Coding route: delegate any workspace mutation to `subagent_coding`; the supervisor should not directly edit files in advanced mode.",
        "- Simple known-target implementation: route directly to `subagent_coding` with file hints and a minimal read budget; do not run a separate explorer first.",
        "- Explorer route: delegate bounded read-only exploration once the tiny budget is exceeded.",
        "- Impact route: call `subagent_impact_analyzer` when affected files, public interfaces, risk, or verification scope are unclear.",
        "- Review route: call `subagent_code_reviewer` for post-change review, scoped verification, and failure attribution.",
    )
