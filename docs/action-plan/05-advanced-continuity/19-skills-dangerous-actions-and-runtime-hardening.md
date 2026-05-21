# Chapter 19: Skills, Dangerous Actions, And Runtime Hardening

## Objective

Fill the remaining runtime gaps that usually appear after a harness becomes genuinely useful. The attached tutorial folders go deeper than the first draft of this action plan in three places that belong together:

- modular skills as on-demand instruction packs
- richer confirmation and dangerous-action handling
- audit-driven runtime hardening and stronger test strategy

This chapter collects those into one practical implementation pass.

## Part 1: Add Skills As A Real Subsystem

Earlier chapters mentioned skills but did not give them dedicated implementation guidance. The OpenAI tutorial is right to treat them separately from tools and memory.

### Skills Are Not Tools

- tools execute side effects or retrieve data
- skills provide reusable operating instructions
- memory stores learned facts
- the base prompt defines stable runtime identity

Skills stay out of the base prompt until the harness decides they are relevant.

## Current Nexus Notes

The current Nexus runtime now includes a minimal but real skills subsystem and a separate dangerous-action audit path:

- skills are discovered from the global skills directory and the local `.nexus/skills/` directory
- active skills are session-scoped and injected into context only when explicitly activated
- the REPL now supports `/skills list|show|add|remove|reload`
- mutating actions are recorded in `.nexus/audit-trail.jsonl` with action state and rollback guidance
- session persistence now uses `latest_session.txt` instead of a platform-specific symlink shortcut

The current implementation does not yet let the model auto-request skills, and it keeps rollback handling descriptive rather than fully automated.

### Add A Skill Model

```python
from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class Skill:
    name: str
    description: str
    content: str
    tags: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    source: str = "local"
```

Include `required_permissions` only if you want the runtime to preflight whether loading the skill is likely to lead to risky actions. The skill itself should not bypass the permission layer.

### Add A Registry And Loader

```python
from pathlib import Path


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def summary(self) -> str:
        if not self._skills:
            return ""
        items = [f"- {skill.name}: {skill.description}" for skill in self._skills.values()]
        return "Available skills:\n" + "\n".join(items)


def load_skills(skills_root: Path) -> SkillRegistry:
    registry = SkillRegistry()

    for path in skills_root.glob("*/SKILL.md"):
        content = path.read_text(encoding="utf-8")
        # Use the first non-empty line after a leading '#' as the description
        description = ""
        for line in content.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                description = stripped[:120]  # cap length for registry display
                break
        registry.register(
            Skill(
                name=path.parent.name,
                description=description or f"Skill from {path.parent.name}",
                content=content,
                source=str(path),
            )
        )

    return registry
```

### Inject Skills Deliberately

The context builder should advertise only a short skill summary by default. The full skill content should be loaded when:

- the model requests it through a `skill` tool or equivalent runtime call
- the user explicitly asks for that workflow
- the runtime applies a deterministic rule such as a command alias

That keeps the core prompt stable.

### Example: Loading A Skill Into Context At Turn Start

```python
# In context_builder.py, append an active skill after the base prompt
def build_with_skill(base_prompt: str, skill: Skill | None) -> str:
    if skill is None:
        return base_prompt
    skill_block = (
        "---\n"
        f"Active Skill: {skill.name}\n\n"
        f"{skill.content}\n"
        "---"
    )
    return f"{base_prompt}\n\n{skill_block}"
```

The skill is appended after the base prompt, not embedded inside it. This keeps the base prompt stable and makes it easy to see in logs when a skill is active.

## Part 2: Upgrade Dangerous-Action Handling

Earlier chapters added approval and clarification as concepts. The attached source material goes further and correctly treats confirmation as a small state machine.

### Define Confirmation States

```python
from dataclasses import dataclass
from enum import Enum


class ConfirmationState(Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    EXECUTED = "executed"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


@dataclass(slots=True)
class DangerousActionRecord:
    action_id: str
    action_name: str
    scope: str
    state: ConfirmationState
    reason: str
    requested_by: str
```

This helps with auditability and rollback logic.

### Add Danger Levels

```python
class DangerLevel(Enum):
    SAFE = "safe"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"


def classify_danger(tool_name: str, arguments: dict) -> DangerLevel:
    if tool_name in {"read_file", "glob", "search_memory", "skill"}:
        return DangerLevel.SAFE
    if tool_name in {"write_file", "write_file"}:
        return DangerLevel.HIGH
    if tool_name in {"bash", "run_command", "delete_file"}:
        return DangerLevel.CRITICAL
    return DangerLevel.LOW
```

The permission layer still decides whether the action is allowed. Danger classification only helps you shape confirmation depth and audit detail.

### Add Rollback Planning

Do not promise rollback for actions that are not actually reversible. But where rollback is feasible, plan for it explicitly.

Examples:

- file overwrite: keep a backup and restore path
- generated note write: delete the new file
- git branch creation: delete the temporary branch

Represent rollback capability alongside the action record.

```python
@dataclass(slots=True, frozen=True)
class RollbackPlan:
    supported: bool
    summary: str
```

If `supported` is false, the confirmation prompt should say so plainly.

## Part 3: Fix The Audit-Driven Runtime Footguns

The attached audit documents surfaced several concrete correctness issues. Incorporate their fixes directly into your implementation plan.

### 1. Do Not Pretend Mutable Dicts Are Frozen

If a dataclass contains mutable dictionaries, either:

- make the class mutable and be honest about it, or
- expose immutable views such as `MappingProxyType`

```python
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(slots=True)
class ToolResult:
    output: str
    is_error: bool = False
    _metadata: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def metadata(self):
        return MappingProxyType(self._metadata)
```

### 2. Guard Against Infinite Tool Loops

```python
MAX_LOOP_ITERATIONS = 40


for iteration in range(MAX_LOOP_ITERATIONS):
    response = await model_client.complete(messages)
    if response.stop_reason == "done" and not response.tool_calls:
        break
else:
    raise RuntimeError("Agent loop exceeded MAX_LOOP_ITERATIONS")
```

This is a real runtime safety boundary, not an optional nicety.

### 3. Make Mode Precedence Explicit

Write the rules down in code and docs:

- hard-deny policy always wins
- plan mode blocks mutating actions unless the runtime is explicitly in an inspect-only exception path
- default mode allows read-only tools and confirms mutations
- auto mode can skip some confirmations but cannot bypass hard-deny or sandbox rules
- workers inherit the coordinator mode unless the coordinator grants a narrower override

If these rules are only implied, the system becomes inconsistent as soon as delegation arrives.

### 4. Avoid Platform-Specific Session Shortcuts

Do not use symlinks for a latest-session pointer if you want broad compatibility. Prefer a small text file such as `latest_session.txt` containing the active session ID.

### 5. Keep Imports Consistent And Boring

Do not scatter foundational runtime types across surprising import paths. If `ToolExecutionContext` lives in `models.py`, either import it from there consistently or re-export it clearly from a single public module.

## Part 4: Add Hardening Tests

The earlier testing chapter covered the basics. This chapter adds the missing deeper patterns.

### Recording Hooks

```python
class RecordingHook:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, payload: dict) -> None:
        self.events.append(payload)
```

Use this to verify pre-tool and post-tool event order.

### Table-Driven Permission Tests

```python
import pytest


@pytest.mark.parametrize(
    "mode,is_mutating,expected",
    [
        ("plan", False, "allow"),
        ("plan", True, "deny"),
        ("default", False, "allow"),
        ("default", True, "confirm"),
        ("auto", True, "confirm"),
    ],
)
def test_permission_matrix(mode, is_mutating, expected):
    tool = type("Tool", (), {"is_mutating": is_mutating})()
    checker = PermissionChecker()
    result = checker.evaluate(tool, {}, mode)
    assert result.decision.value == expected
```

### Guardrail Tests

Add dedicated tests for:

- path traversal attempts
- dangerous shell commands
- stale confirmation requests
- worker requests that exceed delegated scope

These tests should exist even if the harness has no public API yet.

### Retry And Backoff Tests

Transient provider failures are common. Add a simple retry policy and test it.

```python
import asyncio


async def retry_with_backoff(operation, retries: int = 3, base_delay: float = 0.5):
    for attempt in range(retries):
        try:
            return await operation()
        except Exception:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
```

In production, add jitter so many clients do not retry in lockstep.

## Action Plan

1. Add a dedicated `Skill` model and disk-backed registry.
2. Keep skill content separate from tools, memory, and the base prompt.
3. Track dangerous actions with explicit state transitions.
4. Add rollback plans only for actions you can truly reverse.
5. Fix the audit-driven runtime footguns directly in code.
6. Add hardening tests for permissions, hooks, guardrails, and retries.
7. Document mode precedence before worker inheritance makes it ambiguous.

## Validation Checklist

- Skills can be discovered and loaded without polluting the base prompt.
- Dangerous actions are auditable from request to execution or denial.
- The runtime has a max-iteration guard.
- Mode precedence is encoded and testable.
- Session pointer behavior works across platforms.
- Retry logic is limited and visible, not endless.

## Definition Of Done

This chapter is complete when the harness can explain not only what it can do, but how it stays sane when things go wrong. If runtime behavior still depends on unstated precedence rules or optimistic assumptions, the hardening pass is not finished.