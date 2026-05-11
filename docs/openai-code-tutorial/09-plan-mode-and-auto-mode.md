# 09 — Plan Mode and Auto Mode: Controlling Agent Autonomy

## Prerequisites

Complete [08-skills.md](08-skills.md) first.

Your agent can now do a lot: read/write files, remember facts, enforce permissions, load skills on demand. The next question is no longer *what* the agent can do — it is *how freely* it may do it.

This chapter adds **execution modes** — a first-class runtime concept that controls how much autonomy the agent has in a given session.

---

## What you will build

```
agent/
    modes.py          ← NEW: ExecutionMode enum, mode_permits()
    permissions.py    ← updated: mode-aware decisions
    prompts.py        ← updated: mode guidance in prompt
    agent.py          ← updated: stores current mode
    models.py         ← updated: SessionSnapshot includes mode
main.py               ← updated: --mode flag, /mode command
```

---

## 1. What modes mean in code

Modes are **not** personality switches. They are concrete runtime contracts that change:

| What changes | `plan` | `default` | `auto` |
|---|---|---|---|
| Mutating tools | Blocked | Requires confirmation | Allowed (within policy) |
| Read-only tools | Allowed | Allowed | Allowed |
| Prompt guidance | "Inspect and propose" | "Confirm before writing" | "Complete task efficiently" |
| Blast radius | Minimal | Medium | Higher — be careful |

The goal: one `ExecutionMode` value affects both the permission checker and the prompt builder. No forking the loop, no separate tool registries.

---

## 2. Create `agent/modes.py`

```python
# agent/modes.py

from __future__ import annotations

from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    """
    Controls how much autonomy the agent has.

    DEFAULT  — balanced: reads freely, writes with confirmation
    PLAN     — inspect-only: no mutations allowed
    AUTO     — broad execution: mutations allowed within policy
    """
    DEFAULT = "default"
    PLAN    = "plan"
    AUTO    = "auto"


def mode_permits_mutation(mode: ExecutionMode) -> bool:
    """Returns True if this mode allows mutating tools to proceed to the confirm step."""
    return mode != ExecutionMode.PLAN


def mode_requires_confirm(mode: ExecutionMode) -> bool:
    """Returns True if mutating tools need user confirmation in this mode."""
    return mode == ExecutionMode.DEFAULT


def mode_prompt_guidance(mode: ExecutionMode) -> str:
    """
    Returns a short instruction block that tells the model how to behave in this mode.
    Injected into the system prompt each turn.
    """
    if mode == ExecutionMode.PLAN:
        return (
            "# Execution Mode: PLAN\n"
            "You are in PLAN mode. You may inspect files, reason about the problem, "
            "and propose concrete steps — but you must NOT modify any files or run "
            "commands with side effects. Use read_file and glob freely. "
            "End each turn by summarizing what you found and what you would do next."
        )
    elif mode == ExecutionMode.AUTO:
        return (
            "# Execution Mode: AUTO\n"
            "You are in AUTO mode. You may complete the task with minimal interruptions. "
            "Proceed through steps efficiently. You must still respect permission policy — "
            "forbidden paths and command patterns remain blocked. "
            "Prefer smallest necessary actions. Report what you did at the end."
        )
    else:
        return (
            "# Execution Mode: DEFAULT\n"
            "You are in DEFAULT mode. Read-only operations proceed automatically. "
            "Any file write or shell command will pause for user confirmation. "
            "Use ask_user_question if you need clarification before acting."
        )
```

---

## 3. Update `PermissionChecker` to honour the mode

```python
# agent/permissions.py  — update check() to accept mode

from agent.modes import ExecutionMode, mode_permits_mutation, mode_requires_confirm

class PermissionChecker:
    def check(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        cwd: str = ".",
        mode: ExecutionMode = ExecutionMode.DEFAULT,   # ← new
    ) -> PermissionDecision:
        # Hard deny — always, regardless of mode
        if tool_name in self.policy.deny_tools:
            return PermissionDecision.deny(
                reason=f"Tool '{tool_name}' is explicitly denied by policy."
            )

        # Allow-list check
        if tool_name in self.policy.allow_tools:
            return PermissionDecision.allow()

        # Path deny check
        file_path = arguments.get("file_path", "")
        if file_path:
            path_decision = self._check_path(file_path, tool_name, cwd)
            if path_decision.is_denied:
                return path_decision

        # Command deny check
        command = arguments.get("command", "")
        if command:
            cmd_decision = self._check_command(command)
            if cmd_decision.is_denied:
                return cmd_decision

        # Mode-based decision for mutating tools
        if not mode_permits_mutation(mode):
            return PermissionDecision.deny(
                reason=f"Tool '{tool_name}' is a mutating operation. Mutations are blocked in PLAN mode."
            )

        if mode_requires_confirm(mode):
            return PermissionDecision.confirm(
                reason=f"Tool '{tool_name}' requires confirmation in DEFAULT mode."
            )

        # AUTO mode — allow (policy already passed)
        return PermissionDecision.allow()
```

---

## 4. Store mode in `Agent` and pass it to the permission check

```python
# agent/agent.py  — add mode parameter

from agent.modes import ExecutionMode, mode_prompt_guidance

class Agent:
    def __init__(
        self,
        # ...existing params...
        mode: ExecutionMode = ExecutionMode.DEFAULT,    # ← new
    ) -> None:
        # ...existing init...
        self.mode = mode

    def _build_system_prompt(self, user_text: str = "") -> str:
        carry_over = self._snapshot.carry_over if self._snapshot else {}
        memory_text = ""
        if self.memory_store and user_text:
            memory_text = self.memory_store.retrieve(user_text, max_entries=3)
        skills_summary = self.skill_registry.summary() if self.skill_registry else ""

        # Include mode guidance as a section
        return build_runtime_prompt(
            cwd=self.cwd,
            tool_names=self.tool_registry.names(),
            project_notes=self.project_notes,
            carry_over=carry_over,
            user_text=user_text,
            memory_text=memory_text,
            skills_summary=skills_summary,
            mode_guidance=mode_prompt_guidance(self.mode),    # ← new
            base_prompt=self.base_prompt,
        )

    async def run(self, user_text: str):
        # ...in the permission check section, pass self.mode:
        decision = self.permissions.check(
            tool_name=tool_call.name,
            arguments=tool_call.input,
            cwd=self.cwd,
            mode=self.mode,              # ← new
        )
```

---

## 5. Add `add_mode_guidance()` to `ContextBuilder`

```python
# agent/prompts.py  — add method and update build_runtime_prompt

class ContextBuilder:
    # ...existing methods...

    def add_mode_guidance(self, guidance: str) -> "ContextBuilder":
        if guidance and guidance.strip():
            self._sections.append(guidance.strip())
        return self


def build_runtime_prompt(
    *,
    cwd: str,
    tool_names: list[str],
    project_notes: str = "",
    carry_over: dict[str, Any] | None = None,
    user_text: str = "",
    memory_text: str = "",
    skills_summary: str = "",
    mode_guidance: str = "",                   # ← new
    base_prompt: str = DEFAULT_BASE_PROMPT,
) -> str:
    builder = ContextBuilder(cwd=cwd)
    builder.add_base(base_prompt)
    builder.add_environment()
    builder.add_tools(tool_names)
    if mode_guidance:
        builder.add_mode_guidance(mode_guidance)   # ← new (after tools)
    if skills_summary:
        builder.add_skills(skills_summary)
    if project_notes:
        builder.add_project_notes(project_notes)
    if memory_text:
        builder.add_memory(memory_text)
    if carry_over:
        builder.add_task_focus(carry_over)
    if user_text:
        builder.add_user_goal(user_text)
    return builder.build()
```

---

## 6. Persist mode in `SessionSnapshot`

```python
# agent/models.py  — update SessionSnapshot

@dataclass
class SessionSnapshot:
    # ...existing fields...
    mode: str = "default"                     # ← new

    @classmethod
    def new(cls, *, cwd: str, model: str, system_prompt: str, mode: str = "default") -> "SessionSnapshot":
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return cls(
            # ...existing fields...
            mode=mode,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            # ...existing fields...
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionSnapshot":
        return cls(
            # ...existing fields...
            mode=data.get("mode", "default"),
        )
```

Update `Agent.snapshot()` to save the mode and `Agent.restore()` to restore it:

```python
def snapshot(self, carry_over: dict | None = None) -> SessionSnapshot:
    if self._snapshot is None:
        self._snapshot = SessionSnapshot.new(
            cwd=self.cwd, model=self.model_name,
            system_prompt=self.base_prompt,
            mode=self.mode.value,                # ← save mode
        )
    self._snapshot.mode = self.mode.value        # ← sync on every save
    # ...rest of snapshot() unchanged...

def restore(self, snapshot: SessionSnapshot) -> None:
    self._snapshot = snapshot
    self.messages = [Message.from_dict(m) for m in snapshot.messages]
    self._turn_count = snapshot.usage.get("turns", 0)
    self._tool_call_count = snapshot.usage.get("tool_calls", 0)
    self.cwd = snapshot.cwd
    self.mode = ExecutionMode(snapshot.mode)     # ← restore mode
```

---

## 7. Update `main.py` — `--mode` flag and `/mode` command

```python
# main.py  — updated with mode support

from agent.modes import ExecutionMode

# Add to argparse
parser.add_argument(
    "--mode", choices=["default", "plan", "auto"], default="default",
    help="Execution mode: default (confirm mutations), plan (inspect only), auto (run freely)."
)

# In main() — pass mode to build_agent
mode = ExecutionMode(args.mode)
agent = build_agent(project_notes=project_notes, mode=mode)
print(f"Mode: {mode.value.upper()}\n")

# In build_agent() — accept and pass mode
def build_agent(project_notes: str = "", mode: ExecutionMode = ExecutionMode.DEFAULT) -> Agent:
    # ...existing setup...
    return Agent(
        # ...existing args...
        mode=mode,
    )

# In repl() — add /mode command
if user_input.startswith("/mode"):
    parts = user_input.split()
    if len(parts) == 2 and parts[1] in ("default", "plan", "auto"):
        agent.mode = ExecutionMode(parts[1])
        print(f"Mode changed to: {agent.mode.value.upper()}\n")
    else:
        print(f"Current mode: {agent.mode.value.upper()}")
        print("Usage: /mode [default|plan|auto]\n")
    continue
```

---

## 8. See modes in action

```bash
# Start in plan mode — model can inspect but not write
python main.py --mode plan
```

```
Mode: PLAN

you> refactor src/auth.py to use JWT
  · Thinking... (turn 1)
  ⚙ read_file(file_path='src/auth.py')
  ✓ read_file → ...
  ✗ write_file → Permission denied: Mutations are blocked in PLAN mode.

agent> Here is what I would do (PLAN MODE — no changes made):
  1. Replace session.create() calls with jwt.encode()
  2. Add PyJWT dependency to requirements.txt
  3. Update the auth middleware to validate tokens
  Switch to DEFAULT or AUTO mode to execute these changes.
```

```bash
# Switch to auto mode — model proceeds without asking
python main.py --mode auto
```

```
Mode: AUTO

you> refactor src/auth.py to use JWT
  · Thinking... (turn 1)
  ⚙ read_file(file_path='src/auth.py')
  ✓ read_file → ...
  ⚙ write_file(file_path='src/auth.py', content='...')
  ✓ write_file → Successfully wrote 847 characters
  ⚙ write_file(file_path='requirements.txt', content='...')
  ✓ write_file → Successfully wrote 23 characters
```

```
you> /mode plan
Mode changed to: PLAN
you> /mode
Current mode: PLAN
```

---

## 9. Mode transitions on resume

When a session is resumed with `--continue`, the saved mode is restored:

```bash
python main.py --mode auto   # session saved in AUTO mode
# ... later ...
python main.py --continue    # restores AUTO mode
Mode: AUTO  (restored from session)
```

This is intentional — the user chose AUTO deliberately and should get it back. If you prefer to always downgrade to DEFAULT on resume (safer), add this to `Agent.restore()`:

```python
def restore(self, snapshot: SessionSnapshot) -> None:
    # ...existing restore...
    # Downgrade to DEFAULT on resume for safety:
    if self.mode == ExecutionMode.AUTO:
        self.mode = ExecutionMode.DEFAULT
        print("Note: AUTO mode downgraded to DEFAULT on resume. Use /mode auto to re-enable.")
```

---

## 10. Common mistakes

### Mistake 1 — Different loops per mode

```python
# WRONG — duplicated code, hard to maintain
if mode == "plan":
    await run_plan_loop()
elif mode == "auto":
    await run_auto_loop()
```

**Fix:** one loop, mode affects only `permission_checker.check(..., mode=self.mode)` and the prompt section.

### Mistake 2 — Mode without prompt guidance

```python
# WRONG — model has no idea it is in plan mode
# Permissions block writes but the model keeps trying new write approaches
```

**Fix:** always include `mode_prompt_guidance(self.mode)` in the system prompt. The model should understand *why* its writes are failing.

### Mistake 3 — AUTO mode with no policy limits

```python
# WRONG — AUTO mode bypasses all checks
if mode == ExecutionMode.AUTO:
    return PermissionDecision.allow()   # unconditional!
```

**Fix:** hard denies (sensitive paths, forbidden commands) must remain even in AUTO mode. Only user-confirmation step is skipped, not hard policy.

---

## 11. Exercises

**Exercise A — `/mode` shows current state**

When the user types `/mode` with no argument, print not just the mode name but also what it means:
```
Current mode: DEFAULT
  → Reads run automatically. Writes require your approval.
```

**Exercise B — Mode in status line**

Add the current mode to the session startup message:
```
Session: abc123  |  Mode: PLAN  |  Tools: 8
```

**Exercise C — Require confirmation to enter AUTO mode**

When the user types `/mode auto`, require them to type `y` to confirm, with a brief explanation of what AUTO mode allows. Never silently enter it.

---

## 12. Checklist before moving on

- [ ] `ExecutionMode` enum has `DEFAULT`, `PLAN`, `AUTO`
- [ ] `mode_permits_mutation(mode)` returns False for PLAN, True for others
- [ ] `mode_requires_confirm(mode)` returns True for DEFAULT, False for AUTO
- [ ] `mode_prompt_guidance(mode)` returns a clear instruction block for each mode
- [ ] `PermissionChecker.check()` accepts a `mode` argument
- [ ] PLAN mode blocks all mutating tools with a clear "PLAN mode" reason
- [ ] Hard policy denies (sensitive paths, forbidden patterns) are unaffected by mode
- [ ] `Agent` stores `self.mode` and passes it to the permission check each call
- [ ] `SessionSnapshot` saves and restores mode
- [ ] `--mode` CLI flag and `/mode` REPL command both work
- [ ] Mode guidance is included in the system prompt every turn
- [ ] Mode changes emit a `ModeChangedEvent` so they are observable in the hook/event system

### Improvement: mode transitions as events

When mode changes via `/mode auto`, nothing is logged and no hook fires. Fix this:

```python
# agent/events.py  — add ModeChangedEvent
@dataclass(slots=True, frozen=True)
class ModeChangedEvent:
    old_mode: str
    new_mode: str
    changed_by: str   # "user" | "config" | "restore"

# main.py  — in the /mode REPL handler:
old = agent.mode.value
agent.mode = ExecutionMode(parts[1])
print(f"Mode changed to: {agent.mode.value.upper()}")
# Emit so AuditTrail and hooks can observe it:
if agent.audit:
    agent.audit.record("mode_changed", {"old": old, "new": agent.mode.value, "changed_by": "user"})
```

---

Next: [10-swarms-and-delegation.md](10-swarms-and-delegation.md)

