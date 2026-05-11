# 12 — Dangerous Actions and User Confirmation

## Prerequisites

Complete [11-agent-communication.md](11-agent-communication.md) first.

Chapter 07 added a permission layer that can block or require confirmation for mutating tools. But confirmation was a simple `y/N` prompt without context. Now that workers exist, tasks can chain, and actions have broader scope — confirmation needs to be a **first-class system** with:

- clear action descriptions
- scope display
- approval vs clarification distinction
- worker-routed approval via mailbox

---

## What you will build

```
agent/
    confirmation.py    ← NEW: ConfirmationRequest, ConfirmationResult, confirm_action()
    agent.py           ← updated: richer confirmation before dangerous actions
    tools.py           ← updated: tools declare danger_level
main.py                ← updated: richer approval prompt
```

---

## 1. Approval vs clarification — keep them separate

```
APPROVAL:       runtime knows what it wants to do; needs yes or no
                "Allow write_file to overwrite src/app.py? [y/N]"

CLARIFICATION:  runtime doesn't yet know the safe scope; needs more info  
                "Write to staging or production? [staging/production]"
```

These require different user interactions and different model responses:
- After **denial**: model should choose a different strategy
- After **approval**: model proceeds with the confirmed action
- After **clarification**: model narrows the action to the returned scope

---

## 2. Create `agent/confirmation.py`

```python
# agent/confirmation.py

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConfirmationKind(str, Enum):
    APPROVAL       = "approval"       # yes/no for a fully-specified action
    CLARIFICATION  = "clarification"  # user must provide a missing value


@dataclass(frozen=True)
class ConfirmationRequest:
    """
    Describes one action that needs human review before proceeding.

    action      — short label: "write_file", "spawn_worker(auto_mode)", etc.
    description — full human-readable description of what will happen
    scope       — where/what is affected: "src/app.py", "entire workspace", etc.
    reason      — why this needs confirmation
    kind        — approval (y/n) or clarification (open answer)
    choices     — for clarification: list of valid options, if constrained
    """
    action: str
    description: str
    scope: str = ""
    reason: str = ""
    kind: ConfirmationKind = ConfirmationKind.APPROVAL
    choices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConfirmationResult:
    """
    The user's response to a ConfirmationRequest.

    approved    — True if user approved or provided a clarification
    denied      — True if user explicitly said no
    clarification — the user's answer for CLARIFICATION kind
    """
    approved: bool
    denied: bool = False
    clarification: str = ""

    @classmethod
    def allow(cls) -> "ConfirmationResult":
        return cls(approved=True)

    @classmethod
    def deny(cls) -> "ConfirmationResult":
        return cls(approved=False, denied=True)

    @classmethod
    def clarify(cls, answer: str) -> "ConfirmationResult":
        return cls(approved=True, clarification=answer)


# ── CLI confirmation handler ──────────────────────────────────────────────────

async def confirm_action(request: ConfirmationRequest) -> ConfirmationResult:
    """
    Display a confirmation prompt to the user and return their decision.

    Safe default: DENY (user must explicitly type y/yes to approve).
    """
    print()
    print("─" * 60)
    if request.kind == ConfirmationKind.APPROVAL:
        print(f"⚠  Action requires approval")
        print(f"   Action : {request.action}")
        if request.scope:
            print(f"   Scope  : {request.scope}")
        if request.reason:
            print(f"   Reason : {request.reason}")
        print(f"   Detail : {request.description}")
        print("─" * 60)

        try:
            answer = input("   Allow? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(" (interrupted — denied)")
            return ConfirmationResult.deny()

        if answer in {"y", "yes"}:
            return ConfirmationResult.allow()
        return ConfirmationResult.deny()

    else:  # CLARIFICATION
        print(f"❓  Clarification needed")
        print(f"   Action : {request.action}")
        if request.description:
            print(f"   Context: {request.description}")
        if request.choices:
            choices_str = " / ".join(request.choices)
            print(f"─" * 60)
            try:
                answer = input(f"   Choose [{choices_str}]: ").strip()
            except (EOFError, KeyboardInterrupt):
                return ConfirmationResult.deny()
            if answer not in request.choices:
                print(f"   Invalid choice '{answer}'. Action cancelled.")
                return ConfirmationResult.deny()
            return ConfirmationResult.clarify(answer)
        else:
            print("─" * 60)
            try:
                answer = input("   Your answer: ").strip()
            except (EOFError, KeyboardInterrupt):
                return ConfirmationResult.deny()
            if not answer:
                return ConfirmationResult.deny()
            return ConfirmationResult.clarify(answer)


# ── Danger level helper ───────────────────────────────────────────────────────

class DangerLevel(str, Enum):
    SAFE        = "safe"       # no confirmation needed
    LOW         = "low"        # brief confirmation
    HIGH        = "high"       # full approval prompt with scope + reason
    CRITICAL    = "critical"   # denied outright or requires explicit --allow-critical flag


def danger_level_for_tool(tool_name: str, arguments: dict[str, Any]) -> DangerLevel:
    """
    Classify the danger level of a tool call.
    Used to decide confirmation depth.
    """
    safe_tools = {"get_time", "echo", "read_file", "glob", "search_memory",
                  "save_memory", "skill", "ask_user_question", "check_my_mailbox"}
    if tool_name in safe_tools:
        return DangerLevel.SAFE

    # Write to files
    if tool_name == "write_file":
        path = arguments.get("file_path", "")
        # Overwriting source files is HIGH; writing to /tmp is LOW
        if any(ext in (path or "") for ext in [".py", ".ts", ".js", ".go", ".rs"]):
            return DangerLevel.HIGH
        return DangerLevel.LOW

    if tool_name in {"spawn_worker", "send_worker_message"}:
        # Spawning in auto mode is more dangerous
        if arguments.get("mode") == "auto":
            return DangerLevel.HIGH
        return DangerLevel.LOW

    if tool_name == "bash":
        cmd = arguments.get("command", "")
        # Shell is always at least HIGH
        dangerous_patterns = ["rm ", "git reset", "chmod", "curl", "wget"]
        if any(p in cmd for p in dangerous_patterns):
            return DangerLevel.CRITICAL
        return DangerLevel.HIGH

    # Default for unknown mutating tools
    return DangerLevel.LOW
```

---

## 3. Update `Agent.run()` to use rich confirmation

Replace the simple `self._confirm(action, reason) -> bool` with the new `ConfirmationRequest` system:

```python
# agent/agent.py  — updated confirmation handling in run()

from agent.confirmation import (
    ConfirmationRequest, ConfirmationResult, ConfirmationKind,
    confirm_action, danger_level_for_tool, DangerLevel,
)

class Agent:
    def __init__(
        self,
        # ...existing params...
        # Remove old confirm_fn — replaced by confirmation module
    ) -> None:
        # ...existing init...

    async def _get_confirmation(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> ConfirmationResult:
        """Build and display a contextual confirmation request."""
        level = danger_level_for_tool(tool_name, arguments)

        if level == DangerLevel.SAFE:
            return ConfirmationResult.allow()

        if level == DangerLevel.CRITICAL:
            # Never allow — return structured denial so model can adapt
            return ConfirmationResult.deny()

        # Build a descriptive request
        scope = arguments.get("file_path") or arguments.get("command", "")[:60] or "(unspecified)"
        description = self._describe_action(tool_name, arguments)

        request = ConfirmationRequest(
            action=tool_name,
            description=description,
            scope=scope,
            reason="This action modifies the filesystem or has external side effects.",
            kind=ConfirmationKind.APPROVAL,
        )
        return await confirm_action(request)

    def _describe_action(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Generate a human-readable description of what is about to happen."""
        if tool_name == "write_file":
            path = arguments.get("file_path", "?")
            chars = len(arguments.get("content", ""))
            return f"Write {chars} characters to '{path}'"
        if tool_name == "spawn_worker":
            return (
                f"Spawn a {arguments.get('role', 'assistant')} worker: "
                f"{arguments.get('description', arguments.get('prompt', ''))[:80]}"
            )
        if tool_name == "bash":
            return f"Run shell command: {arguments.get('command', '')[:80]}"
        return f"Execute {tool_name} with arguments: {str(arguments)[:80]}"


    async def run(self, user_text: str):
        # ...existing setup and hook calls...

        for tool_call in response.tool_calls:
            # ...hook and permission check...

            if decision.needs_confirmation:
                result = await self._get_confirmation(tool_call.name, tool_call.input)

                if result.denied:
                    denied_msg = "User denied the action."
                    self.messages.append(Message.tool_result(tool_call.id, denied_msg))
                    yield ToolExecutionCompleted(tool_name=tool_call.name, output=denied_msg, is_error=True)
                    continue

                if result.clarification:
                    # Inject clarification as context before executing
                    clarification_note = f"[User clarification: {result.clarification}]"
                    self.messages.append(Message.user(clarification_note))

            # ── Execute (approved) ────────────────────────────────────────────
            yield ToolExecutionStarted(tool_name=tool_call.name, tool_input=tool_call.input)
            # ...rest of execution unchanged...
```

---

## 4. Add a clarification example tool

Some requests genuinely need a scope choice before they can run safely:

```python
# agent/tools.py  — DeployTool example with clarification

class DeployTool(BaseTool):
    """
    Example of a tool that uses CLARIFICATION before executing.
    In a real project this would trigger a deployment pipeline.
    """
    name = "deploy"
    description = "Deploy the application. Requires environment clarification."
    input_schema = {
        "type": "object",
        "properties": {
            "environment": {"type": "string", "enum": ["staging", "production"]},
        },
        "required": [],
    }
    is_mutating = True

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        env = arguments.get("environment", "")
        if not env:
            # Model called deploy without specifying environment
            # The confirmation system should have caught this — but handle it safely
            return ToolResult(
                output="Cannot deploy: environment not specified. Use 'staging' or 'production'.",
                is_error=True,
            )
        return ToolResult(output=f"Deploying to {env}... (dry run — no real deployment in this tutorial)")
```

When the model calls `deploy` without an environment, update `_get_confirmation` to detect this and use `CLARIFICATION`:

```python
# In agent.py _get_confirmation()

if tool_name == "deploy" and not arguments.get("environment"):
    request = ConfirmationRequest(
        action="deploy",
        description="Deploy the application to which environment?",
        kind=ConfirmationKind.CLARIFICATION,
        choices=["staging", "production"],
    )
    result = await confirm_action(request)
    if result.clarification:
        # Inject the clarification into tool arguments
        arguments["environment"] = result.clarification
    return result
```

---

## 5. Worker-routed approval via mailbox

When a worker needs approval, it should not prompt `stdin` directly — the coordinator/user might be managing multiple workers. Instead it sends a `PERMISSION` message through the mailbox:

```python
# agent/swarm.py  — worker confirmation callback

async def worker_confirm(task: TaskRecord, mailbox: InMemoryMailbox, action: str, reason: str) -> bool:
    """
    Worker sends a PERMISSION request and waits for APPROVAL response.
    Times out and denies if no response within 30 seconds.
    """
    perm_msg = AgentMessage.permission_request(
        sender=task.worker_id,
        task_id=task.task_id,
        action=action,
        reason=reason,
    )
    await mailbox.send(perm_msg)
    task.status = TaskStatus.WAITING
    task.notes.append(f"Waiting for approval: {action}")

    # Wait for coordinator to respond
    approved = await mailbox.wait_for_approval(
        recipient=task.worker_id,
        correlation_id=perm_msg.correlation_id,
        timeout=30.0,
    )

    task.status = TaskStatus.RUNNING
    return approved if approved is not None else False   # deny on timeout
```

---

## 6. See it in action

```
you> write the refactored auth code to src/auth.py

──────────────────────────────────────────────────────────────
⚠  Action requires approval
   Action : write_file
   Scope  : src/auth.py
   Reason : This action modifies the filesystem or has external side effects.
   Detail : Write 1247 characters to 'src/auth.py'
──────────────────────────────────────────────────────────────
   Allow? [y/N]: y

  ⚙ write_file(file_path='src/auth.py')
  ✓ write_file → Successfully wrote 1247 characters
```

```
you> deploy the app

──────────────────────────────────────────────────────────────
❓  Clarification needed
   Action : deploy
   Context: Deploy the application to which environment?
──────────────────────────────────────────────────────────────
   Choose [staging / production]: staging

  ⚙ deploy(environment='staging')
  ✓ deploy → Deploying to staging...
```

```
you> run rm -rf /

Permission denied: Command matches denied pattern: 'rm\s+-rf'
(CRITICAL — no confirmation prompt shown)
```

---

## 7. Confirmation vs permission — final picture

```
Tool call arrives
        │
        ▼
PermissionChecker.check()
        │
   ┌────┴──────────────────────┐
   DENY                      CONFIRM
   │                          │
   Return error               Agent._get_confirmation()
   to model                      │
                         ┌───────┴──────────────┐
                      APPROVAL             CLARIFICATION
                         │                      │
                      y/n prompt           choice prompt
                         │                      │
                    ┌────┴────┐            clarification
                  YES        NO            injected into
                   │          │            tool arguments
                execute    return              │
                           "User           execute
                           denied"
```

---

## 8. Common mistakes

### Mistake 1 — Vague confirmation prompt

```python
# WRONG — user has no idea what they are approving
print("Allow action? [y/N]")
```

**Fix:** always show action name, scope (what file/command), and what will happen. The user should be able to make an informed decision.

### Mistake 2 — Defaulting to YES

```python
answer = input("Allow? [Y/n]: ").strip().lower()
if answer != "n":   # ← empty Enter = yes
    return ConfirmationResult.allow()
```

**Fix:** for dangerous actions, empty Enter = NO. The user must explicitly type `y`.

### Mistake 3 — Merging approval and clarification

```python
# WRONG — the response to "which environment?" is not y/n
answer = input("Allow deploy? [y/N]: ")
```

**Fix:** use `ConfirmationKind.CLARIFICATION` for scope questions, `APPROVAL` for go/no-go decisions.

---

## 9. Exercises

**Exercise A — `--skip-confirmation` flag**

Add a `--skip-confirmation` CLI flag for development/testing mode that auto-approves LOW danger level actions. HIGH and CRITICAL still require human input.

**Exercise B — Multi-step clarification**

For `spawn_worker` without a `role` specified: first clarify the role (`researcher`, `tester`, `reviewer`), then clarify the execution mode (`default`, `plan`). Chain two `await confirm_action()` calls.

**Exercise C — Confirmation history**

Record all confirmation requests and their results in a list on the `Agent`. Add a `/confirmations` REPL command that prints the last 10, showing action, scope, decision, and timestamp.

---

## 10. Checklist before moving on

- [ ] `ConfirmationRequest` has action, description, scope, reason, kind, choices
- [ ] `ConfirmationResult` has approved, denied, clarification fields
- [ ] `ConfirmationKind.APPROVAL` shows y/N prompt defaulting to deny
- [ ] `ConfirmationKind.CLARIFICATION` shows choice prompt with validation
- [ ] `DangerLevel` classifies tools: SAFE, LOW, HIGH, CRITICAL
- [ ] CRITICAL actions are denied without prompting
- [ ] SAFE actions are auto-approved without prompting
- [ ] Worker sends `PERMISSION` mailbox message instead of prompting stdin
- [ ] Coordinator can send `APPROVAL` response back via mailbox
- [ ] Clarification answers are injected into tool arguments, not the message history
- [ ] Empty Enter = NO for approval questions (safe default)

---

## Current Nexus Notes

### What Nexus implements (as of this chapter)

`ConfirmationRequest` carries:
- `kind` — `APPROVAL` or `CLARIFICATION`
- `tool_name` — name of the tool being gated
- `reason` — human-readable explanation from `PermissionChecker`
- `arguments` — the **full tool arguments dict** so the UI can show exactly what will be read/written before asking

The agent always stops and yields `confirmation_requested` when `PermissionDecision.CONFIRM` is returned. Auto-mode (`--mode auto`) skips this gate entirely.

### Confirmation display — what the user sees

When a mutating or medium/high-risk tool needs approval, the REPL prints a panel **immediately** as the event streams through `_render_event`, before reading any input:

```
────────────── Approval Required ──────────────
  Tool: write_file
    path: CONTEXT.md
    content: # Codebase Summary…  (1 234 chars)
  Reason: write_file always requires confirmation

  Allow? [y/N]:
```

Key design points:
- The panel is always shown even when `show_tool_calls = false` (the user must see what they're approving)
- Large argument values (e.g. file content) are truncated to 120 chars with `…`
- The input prompt uses `console.input()` (not bare `input()`) so Rich terminal state is not corrupted
- Empty Enter or Ctrl-C → denied (safe default)

For clarifications:

```
───── Clarification Needed — write_file ───────
  Provide a value for 'path' before running 'write_file'.

  Value for 'path':
```

### `ConfirmationRequest.arguments` — why it matters

Without `arguments`, the approval panel can only say *"Allow write_file?"*. With it, it says *"Allow write_file → path: CONTEXT.md, content: …"*. The user can make an informed decision without having to re-read the preceding tool call line.

### auto mode skips ALL confirmations

```bash
nexus --mode auto
# or inside the REPL:
/mode auto
```

In `auto` mode, `PermissionDecision.CONFIRM` tools are executed without prompting. Only `DENY` decisions (plan mode) and high-risk `bash` commands are still blocked.

---

Next: [13-guardrails-and-safety.md](13-guardrails-and-safety.md)

