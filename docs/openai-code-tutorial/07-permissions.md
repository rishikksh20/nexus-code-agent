# 07 — Permissions: Governing What the Agent May Do

## Prerequisites

Complete [06-memory-and-storage.md](06-memory-and-storage.md) first.

Right now the agent can run any tool with any arguments. No checks, no boundaries. If the model asks to overwrite `/etc/hosts` or run `rm -rf /`, nothing stops it.

This chapter adds a **permission layer** that sits between the model's intent and the actual execution of any action. It runs in code — not in the prompt — which means it enforces real boundaries regardless of what the model was instructed.

---

## What you will build

```
agent/
    permissions.py    ← NEW: PermissionDecision, PermissionPolicy, PermissionChecker
    tools.py          ← updated: tools declare is_mutating
    agent.py          ← updated: checks permission before every tool.execute()
```

---

## 1. Why permissions must live in code, not prompts

A system prompt can say: `"Only write files the user has approved."` But:
- the model can be confused,
- the model can be prompt-injected by malicious file content,
- the model can misunderstand scope,
- the model can be confidently wrong.

A runtime permission check runs **every time**, on **every tool call**, regardless of what the model was told. It is the actual safety boundary.

```
Prompt instruction:  "Please be careful"   → advisory only, not enforced
Permission check:    is_path_allowed(path) → enforced in code every time
```

---

## 2. The three permission outcomes

```
┌──────────────────────────────────────────────────────────┐
│                  Tool call arrives                       │
│                        ▼                                 │
│              Permission checker runs                     │
│                        ▼                                 │
│        ┌───────────────┼───────────────┐                 │
│        ▼               ▼               ▼                 │
│     ALLOW          CONFIRM           DENY                │
│   run now       ask user first    block, tell model      │
└──────────────────────────────────────────────────────────┘
```

- **Allow** — safe action, run immediately (e.g., `read_file`)
- **Confirm** — risky action, pause and ask the user (e.g., `write_file`)
- **Deny** — forbidden action, block and return error (e.g., access to `~/.ssh/`)

Keep all three outcomes distinct. They require different responses from both the model and the user.

---

## 3. Create `agent/permissions.py`

```python
# agent/permissions.py

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Decision ─────────────────────────────────────────────────────────────────

class PermissionOutcome:
    ALLOW   = "allow"
    CONFIRM = "confirm"
    DENY    = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    outcome: str                        # PermissionOutcome constant
    reason: str = ""                    # human-readable explanation

    @classmethod
    def allow(cls) -> "PermissionDecision":
        return cls(outcome=PermissionOutcome.ALLOW)

    @classmethod
    def confirm(cls, reason: str) -> "PermissionDecision":
        return cls(outcome=PermissionOutcome.CONFIRM, reason=reason)

    @classmethod
    def deny(cls, reason: str) -> "PermissionDecision":
        return cls(outcome=PermissionOutcome.DENY, reason=reason)

    @property
    def is_allowed(self) -> bool:
        return self.outcome == PermissionOutcome.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.outcome == PermissionOutcome.CONFIRM

    @property
    def is_denied(self) -> bool:
        return self.outcome == PermissionOutcome.DENY


# ── Policy configuration ──────────────────────────────────────────────────────

@dataclass
class PermissionPolicy:
    """
    Configures which tools, paths, and commands are allowed/denied/confirmed.

    Defaults are deliberately conservative:
    - read-only tools run automatically
    - mutating tools require confirmation
    - sensitive paths are always denied
    """
    # Tools that are always allowed without confirmation
    allow_tools: set[str] = field(default_factory=lambda: {
        "get_time", "echo", "read_file", "glob", "search_memory",
    })

    # Tools that are hard-denied (not even confirmation can unblock these)
    deny_tools: set[str] = field(default_factory=set)

    # Path prefixes that are always denied (relative to filesystem root)
    deny_path_prefixes: list[str] = field(default_factory=lambda: [
        "/etc/",
        "/sys/",
        "/proc/",
        str(Path.home() / ".ssh"),
        str(Path.home() / ".aws"),
        str(Path.home() / ".gnupg"),
    ])

    # Path must be inside this directory for write operations (empty = no restriction)
    write_allowed_root: str = ""

    # Shell command fragments that are always denied
    deny_command_patterns: list[str] = field(default_factory=lambda: [
        r"rm\s+-rf",
        r"rm\s+--no-preserve-root",
        r"git\s+reset\s+--hard",
        r"chmod\s+777",
        r"mkfs\.",
        r"dd\s+if=",
        r":(){:|:&};:",       # fork bomb
        r"curl.*?\|\s*sh",    # curl-pipe-shell
        r"wget.*?\|\s*sh",
    ])


# ── Checker ───────────────────────────────────────────────────────────────────

class PermissionChecker:
    """
    Evaluates tool calls against PermissionPolicy.

    Usage:
        checker = PermissionChecker(policy)
        decision = checker.check(tool_name="write_file", arguments={"file_path": "/tmp/out.txt"})
        if decision.is_denied:
            ...
        elif decision.needs_confirmation:
            ...
    """

    def __init__(self, policy: PermissionPolicy | None = None) -> None:
        self.policy = policy or PermissionPolicy()

    def check(
        self, tool_name: str, arguments: dict[str, Any], cwd: str = "."
    ) -> PermissionDecision:
        """
        Evaluate permission for one tool call.
        Checks in order: hard deny → allow list → path rules → command rules → confirm.
        """
        # 1. Hard deny — no confirmation can override
        if tool_name in self.policy.deny_tools:
            return PermissionDecision.deny(
                reason=f"Tool '{tool_name}' is explicitly denied by policy."
            )

        # 2. Always-allowed tools — low risk, run without asking
        if tool_name in self.policy.allow_tools:
            return PermissionDecision.allow()

        # 3. Path-based deny — check file_path argument
        file_path = arguments.get("file_path", "")
        if file_path:
            path_decision = self._check_path(file_path, tool_name, cwd)
            if path_decision.is_denied:
                return path_decision

        # 4. Command deny patterns — check command argument
        command = arguments.get("command", "")
        if command:
            cmd_decision = self._check_command(command)
            if cmd_decision.is_denied:
                return cmd_decision

        # 5. Everything else that is not in the allow list needs confirmation
        return PermissionDecision.confirm(
            reason=f"Tool '{tool_name}' is a mutating operation that requires approval."
        )

    def _check_path(
        self, file_path: str, tool_name: str, cwd: str
    ) -> PermissionDecision:
        """Check path against deny prefixes and write root restriction."""
        # Resolve relative paths
        path = Path(file_path)
        if not path.is_absolute():
            path = (Path(cwd) / path).resolve()
        else:
            path = path.resolve()

        path_str = str(path)

        # Deny sensitive prefixes
        for prefix in self.policy.deny_path_prefixes:
            if path_str.startswith(str(Path(prefix).resolve())):
                return PermissionDecision.deny(
                    reason=f"Access to '{path_str}' is denied: sensitive path."
                )

        # For write operations, enforce write_allowed_root if configured
        write_tools = {"write_file", "edit_file", "bash"}
        if tool_name in write_tools and self.policy.write_allowed_root:
            allowed = Path(self.policy.write_allowed_root).resolve()
            if not path_str.startswith(str(allowed)):
                return PermissionDecision.deny(
                    reason=(
                        f"Write to '{path_str}' is denied. "
                        f"Writes are restricted to: {allowed}"
                    )
                )

        return PermissionDecision.allow()

    def _check_command(self, command: str) -> PermissionDecision:
        """Check shell command against deny patterns."""
        for pattern in self.policy.deny_command_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return PermissionDecision.deny(
                    reason=f"Command matches denied pattern: '{pattern}'"
                )
        return PermissionDecision.allow()
```

---

## 4. Mark tools as read-only or mutating

Add a class attribute `is_mutating` to `BaseTool`:

```python
# agent/tools.py  — update BaseTool

class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, Any]
    is_mutating: bool = False          # ← new: default safe

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        ...
```

Update each concrete tool:

```python
class GetTimeTool(BaseTool):
    is_mutating = False   # already the default, but explicit is better

class ReadFileTool(BaseTool):
    is_mutating = False

class GlobTool(BaseTool):
    is_mutating = False

class WriteFileTool(BaseTool):
    is_mutating = True    # ← changes the world

class EchoTool(BaseTool):
    is_mutating = False

class AskUserQuestionTool(BaseTool):
    is_mutating = False
```

The permission check can now quickly distinguish read-only from mutating tools without hardcoding names.

---

## 5. Wire permissions into `Agent`

```python
# agent/agent.py  — add permission_checker parameter

from agent.permissions import PermissionChecker, PermissionPolicy, PermissionOutcome

class Agent:
    def __init__(
        self,
        model_client: Any,
        tool_registry: ToolRegistry,
        base_prompt: str = DEFAULT_BASE_PROMPT,
        cwd: str | None = None,
        model_name: str = "demo",
        hook_executor: HookExecutor | None = None,
        project_notes: str = "",
        memory_store=None,
        permission_checker: PermissionChecker | None = None,  # ← new
        confirm_fn=None,                                       # ← new (async callback)
    ) -> None:
        # ...existing init...
        self.permissions = permission_checker or PermissionChecker()
        self._confirm = confirm_fn  # async (action: str, reason: str) -> bool

    async def run(self, user_text: str):
        # ...existing setup...

        for tool_call in response.tool_calls:
            self._tool_call_count += 1

            # ── pre_tool_use hook (can still block) ───────────────────────────
            pre_result = await self.hooks.execute(
                HookEvent.PRE_TOOL_USE,
                pre_tool_payload(tool_call.name, tool_call.input),
            )
            if pre_result.blocked:
                blocked_msg = f"Blocked by hook: {pre_result.block_reason}"
                self.messages.append(Message.tool_result(tool_call.id, blocked_msg))
                yield ToolExecutionCompleted(tool_name=tool_call.name, output=blocked_msg, is_error=True)
                continue

            # ── Permission check ──────────────────────────────────────────────
            decision = self.permissions.check(
                tool_name=tool_call.name,
                arguments=tool_call.input,
                cwd=self.cwd,
            )

            if decision.is_denied:
                denied_msg = f"Permission denied: {decision.reason}"
                self.messages.append(Message.tool_result(tool_call.id, denied_msg))
                yield ToolExecutionCompleted(tool_name=tool_call.name, output=denied_msg, is_error=True)
                continue

            if decision.needs_confirmation:
                # Ask user if we have a confirmation callback
                if self._confirm is not None:
                    approved = await self._confirm(tool_call.name, decision.reason)
                    if not approved:
                        denied_msg = "User denied the action."
                        self.messages.append(Message.tool_result(tool_call.id, denied_msg))
                        yield ToolExecutionCompleted(tool_name=tool_call.name, output=denied_msg, is_error=True)
                        continue
                # If no confirm_fn is set, default to denying mutations
                elif self.permissions.policy.deny_tools or True:
                    denied_msg = f"Action requires confirmation but no confirm callback is set. Denied: {decision.reason}"
                    self.messages.append(Message.tool_result(tool_call.id, denied_msg))
                    yield ToolExecutionCompleted(tool_name=tool_call.name, output=denied_msg, is_error=True)
                    continue

            # ── Execute (permission passed) ───────────────────────────────────
            yield ToolExecutionStarted(tool_name=tool_call.name, tool_input=tool_call.input)
            # ...rest of execution unchanged...
```

---

## 6. Add the confirmation callback to `main.py`

```python
# main.py  — add confirm_fn and pass to Agent

async def ask_for_confirmation(action: str, reason: str) -> bool:
    """
    Pause and ask the user whether to allow a mutating action.

    Deliberately defaults to NO — safe default for dangerous operations.
    """
    print(f"\n⚠  Confirmation required")
    print(f"   Action : {action}")
    print(f"   Reason : {reason}")
    try:
        answer = input("   Allow? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(" (interrupted — denied)")
        return False
    return answer in {"y", "yes"}


def build_agent(project_notes: str = "") -> Agent:
    from agent.permissions import PermissionChecker, PermissionPolicy
    # ...existing setup...

    policy = PermissionPolicy(
        write_allowed_root=__import__("os").getcwd(),  # only allow writes inside cwd
    )
    checker = PermissionChecker(policy=policy)

    return Agent(
        # ...existing args...
        permission_checker=checker,
        confirm_fn=ask_for_confirmation,
    )
```

---

## 7. See it in action

```bash
python main.py
```

```
you> write hello to /etc/hosts
  · Thinking... (turn 1)
  ✗ write_file → Permission denied: Access to '/etc/hosts' is denied: sensitive path.

agent> I cannot write to /etc/hosts — it is a system file outside the allowed workspace.
```

```
you> write hello to output.txt

⚠  Confirmation required
   Action : write_file
   Reason : Tool 'write_file' is a mutating operation that requires approval.
   Allow? [y/N]: y
  ⚙ write_file(file_path='output.txt', content='hello')
  ✓ write_file → Successfully wrote 5 characters to /home/user/project/output.txt
```

```
you> write hello to output.txt
   Allow? [y/N]: n
  ✗ write_file → User denied the action.

agent> Understood. I won't write to that file.
```

---

## 8. Common mistakes

### Mistake 1 — Policy in the prompt only

```python
# WRONG — the model "knows" not to write but nothing prevents it
system_prompt = "Never write to files outside the workspace."
```

**Fix:** enforce workspace boundaries in `_check_path()` with `write_allowed_root`.

### Mistake 2 — Defaulting confirmation to "yes"

```python
# WRONG — auto-approving mutations defeats the purpose
if decision.needs_confirmation:
    proceed()   # skip asking
```

**Fix:** always ask, always default to `No` when the user presses Enter without typing.

### Mistake 3 — Merging deny and confirm outcomes

```python
# WRONG — "blocked" covers both policy deny and user deny
if not decision.is_allowed:
    return ToolResult(output="Blocked.", is_error=True)
```

**Fix:** the model needs different feedback for each case:
- Policy deny → "Permission denied: sensitive path" (model should try something else)
- User deny → "User denied the action" (model should stop or ask differently)

---

## 9. Exercises

**Exercise A — `is_mutating` shortcut**

Update `PermissionChecker.check()` to check `tool.is_mutating` directly. Look up the tool from a `ToolRegistry` reference and skip straight to `CONFIRM` for any mutating tool, without needing to enumerate tool names in the policy.

**Exercise B — Confirmation summary**

Modify `ask_for_confirmation` to show the full `tool_input` dict (truncated to 200 chars) along with the action name. This lets the user see `file_path='output.txt'` instead of just `write_file`.

**Exercise C — Hard-deny `bash`**

Add `"bash"` to `PermissionPolicy.deny_tools`. Observe that even with `confirm_fn` set, the tool is blocked. Then create a `SafeBashTool` that only allows a fixed list of commands and is in `allow_tools` instead.

**Exercise D — Audit denied actions**

In `Agent.run()`, when a permission decision is `DENY`, fire a `HookEvent.NOTIFICATION` event with `level="warning"`. Create a hook that writes denied actions to `denied.log`.

---

## 10. Checklist before moving on

- [ ] `PermissionDecision` has three outcomes: allow, confirm, deny
- [ ] `PermissionPolicy` configures allow lists, deny lists, path prefixes, and command patterns
- [ ] `PermissionChecker.check()` evaluates in order: deny → allow → path → command → confirm
- [ ] `BaseTool.is_mutating` is set explicitly on all tools
- [ ] Permission check runs in `Agent.run()` *before* `tool.execute()`
- [ ] Deny returns an error `ToolResult` to the model with a clear reason
- [ ] Confirm calls `self._confirm` (async callback) — default is deny if not set
- [ ] `write_allowed_root` restricts file writes to the project directory
- [ ] Sensitive paths (`~/.ssh`, `/etc/`, etc.) are hard-denied
- [ ] Shell command patterns (`rm -rf`, fork bomb, curl-pipe-sh) are hard-denied
- [ ] Ephemeral session-scoped grants are saved in `carry_over` and expire on session end

### Improvement: per-session ephemeral permission grants

Global policy applies to every session. Sometimes users want to grant extra permissions for the current session only:

```
you> for this session, auto-approve write_file — I trust you
```

Implement this using `carry_over`:

```python
# In SessionSnapshot.carry_over:
{"ephemeral_allow_tools": ["write_file", "bash"]}

# In PermissionChecker.check() — check ephemeral grants first:
def check(self, tool_name, arguments, cwd=".", mode=..., carry_over: dict | None = None):
    ephemeral_allow = (carry_over or {}).get("ephemeral_allow_tools", [])
    if tool_name in ephemeral_allow:
        return PermissionDecision.allow()   # session grant overrides confirm
    # ...rest of existing logic...
```

The grant lives in the session snapshot only — it is gone when the session ends.

---

Next: [07-1-docker-sandboxing.md](07-1-docker-sandboxing.md) — add true execution isolation, then continue to [08-skills.md](08-skills.md).

