# Chapter 5: Permissions, Hooks, And Execution Modes

## Objective

Turn the harness into a controlled runtime instead of a blindly obedient executor. This chapter incorporates several of the most important refinements from `openai-code-tutorial`:

- explicit permission outcomes
- hook-based lifecycle extension
- execution modes that shape how much autonomy the harness has
- clean separation between approval and clarification

It also aligns with the stronger architectural lesson from `agentic-framework-tutorial`: the runtime must enforce policy in code.

## Add A Real Permission Model

Start with a small enum and a decision object.

```python
from dataclasses import dataclass
from enum import Enum


class PermissionDecision(Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(slots=True, frozen=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str
```

Now create a checker that looks at the tool, the arguments, and the current execution mode.

```python
from models import ToolCall                    # wherever BaseTool lives in your package
from runtime.execution_modes import ExecutionMode


class PermissionChecker:
    def evaluate(self, tool: BaseTool, arguments: dict, mode: ExecutionMode) -> PermissionResult:
        if tool.is_mutating and mode is ExecutionMode.PLAN:
            return PermissionResult(PermissionDecision.DENY, "Mutating tools are blocked in plan mode.")
        if tool.is_mutating and mode is not ExecutionMode.AUTO:
            return PermissionResult(PermissionDecision.CONFIRM, "Mutating tool requires approval.")
        return PermissionResult(PermissionDecision.ALLOW, "Read-only tool or auto mode.")
```

In the current Nexus runtime, the `PermissionChecker` is significantly extended beyond the minimal sketch above:

**Tool-specific policies (evaluated before the generic is_mutating check):**

- `write_file`, `write_file`, `modify_file`, `replace_text` — the requested path is inspected and writes outside the workspace root or inside `.nexus/` managed state are hard-denied in all modes including `AUTO`.
- `write_file` (HIGH RISK) — always returns `CONFIRM` regardless of mode (except `PLAN` which returns `DENY`). Even `AUTO` mode cannot bypass confirmation for a full-file overwrite.
- `bash` — the command string is passed through `classify_bash_risk()` before a decision is made:
  - `LOW` risk (read-only commands such as `cat`, `grep`, `git status`) → `ALLOW` in all modes
  - `MEDIUM` risk (targeted writes: `mkdir`, `mv`, `git commit`, output redirection) → `DENY` in plan, `CONFIRM` in default, `ALLOW` in auto
  - `HIGH` risk (destructive or privileged: `rm -rf`, `sudo`, pipe-to-shell) → `DENY` in plan, `CONFIRM` in default **and** auto

Those hard denies and high-risk overrides remain active even in `AUTO` mode and cannot be configured away.

This checker is not cosmetic. The agent loop must consult it before every tool execution.

## Separate Approval From Clarification

This is a specific improvement from `openai-code-tutorial` that deserves to be kept.

Approval means:

- the action is fully specified
- the only question is whether to proceed

Clarification means:

- a required value is still missing
- the user must supply information, not just say yes or no

Represent that distinction explicitly.

```python
from enum import Enum


class ConfirmationKind(Enum):
    APPROVAL = "approval"
    CLARIFICATION = "clarification"


@dataclass(slots=True)  # NOT frozen: payload dict is built dynamically
class ConfirmationRequest:
    kind: ConfirmationKind
    prompt: str
    payload: dict[str, str]
```

## Add Hooks For Lifecycle Control

Hooks let you add logging, policy, telemetry, or notifications without bloating the agent loop.

```python
from collections import defaultdict
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any


class HookEvent(Enum):
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    STOP = "stop"
    NOTIFICATION = "notification"


HookHandler = Callable[[dict[str, Any]], Awaitable[None]]


class HookExecutor:
    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[HookHandler]] = defaultdict(list)

    def register(self, event: HookEvent, handler: HookHandler) -> None:
        self._handlers[event].append(handler)

    async def emit(self, event: HookEvent, payload: dict[str, Any]) -> None:
        for handler in self._handlers[event]:
            await handler(payload)
```

Later you can add hook result objects, timeouts, and blocking semantics. Start small but keep the interface explicit.

## Add Execution Modes

This is one of the most practical additions in the OpenAI-oriented tutorial. A single runtime mode can influence both prompt framing and permission policy.

```python
from enum import Enum


class ExecutionMode(Enum):
    PLAN = "plan"
    DEFAULT = "default"
    AUTO = "auto"
```

### Suggested Semantics

- `PLAN`: explain steps, inspect state, but do not mutate local state
- `DEFAULT`: allow read-only actions immediately and ask before mutations
- `AUTO`: allow more actions without repeated prompts, but still respect hard-deny policies

Never let `AUTO` bypass hard safety boundaries such as forbidden paths or sandbox rules.

## Wire Policy Into The Agent Loop

The loop should now look roughly like this:

```python
decision = permission_checker.evaluate(tool, tool_call.arguments, mode)

if decision.decision is PermissionDecision.DENY:
    yield {"event": "tool_denied", "reason": decision.reason}
    continue

if decision.decision is PermissionDecision.CONFIRM:
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        prompt=f"Allow tool '{tool.name}'?",
        payload={"reason": decision.reason},
    )
    yield {"event": "confirmation_requested", "value": request}
    # The agent pauses here. The REPL (see below) must ask the user and
    # then re-invoke the loop with an 'approved' flag or skip the tool.
    continue

await hooks.emit(HookEvent.PRE_TOOL_USE, {"tool_name": tool.name})
result = await tool.execute(tool_call.call_id, tool_call.arguments, context)
await hooks.emit(HookEvent.POST_TOOL_USE, {"tool_name": tool.name, "result": result.output})
```

## Handle Confirmation In The REPL

When the agent emits `confirmation_requested`, the REPL is responsible for asking the user and then deciding whether to re-run the tool. The simplest pattern is a retry loop.

```python
async def repl_turn(agent: Agent, history: list[Message], context: ToolExecutionContext) -> None:
    pending_approvals: dict[str, bool] = {}  # tool_name -> approved

    async for event in agent.run(history, context, approved_tools=pending_approvals):
        if event["event"] == "model_response":
            response = event["value"]
            history.append(response.message)
            print(response.message.content)

        elif event["event"] == "confirmation_requested":
            req: ConfirmationRequest = event["value"]
            answer = input(f"  {req.prompt} [y/N]: ").strip().lower()
            if answer == "y":
                # Record approval so the next iteration can run the tool
                tool_name = req.payload.get("tool_name", "")
                pending_approvals[tool_name] = True
                # Re-run the full turn with approval in place
                await repl_turn(agent, history, context)
                return

        elif event["event"] == "tool_result":
            result = event["value"]
            history.append(Message(role="tool", content=result.output, name=result.tool_name))
```

This is a simplified walkthrough. In a mature implementation you would use a continuation object or separate approved-set passed into the agent loop rather than a full recursive re-run. The key point is that the REPL, not the agent loop, owns the user interaction.

## Action Plan

1. Add permission outcomes as executable runtime decisions.
2. Enforce policy before every tool call.
3. Model approval and clarification as different request types.
4. Introduce lifecycle hooks for cross-cutting concerns.
5. Add execution modes and make them affect both prompts and permissions.
6. Keep all hard-deny policies outside prompt text.

## Validation Checklist

- Mutating tools do not run in plan mode.
- The UI can distinguish approval from clarification.
- Hooks can observe tool activity without editing the core loop.
- Execution mode changes are visible in runtime state.
- Hard-denied actions stay denied even in auto mode.
- Argument-aware path policy is enforced in runtime code for mutating file writes.

## Definition Of Done

This chapter is done when your harness is no longer relying on trust alone. The model can suggest actions, but the runtime makes the final decision.

## Current Nexus Notes

The Nexus permission system (`nexus/runtime/permissions.py`) now handles four categories of tools beyond the minimal sketch in this chapter:

**1. Path-policy hard denials** apply to `write_file`, `write_file`, `modify_file`, and `replace_text`. Writes outside the workspace root or inside `.nexus/` managed state are `DENY` regardless of mode.

**2. `write_file` (HIGH RISK)** always returns `CONFIRM` — even in `AUTO` mode. A full-file overwrite is considered too irreversible to auto-approve.

**3. `bash` dynamic risk** — the command string is classified by `classify_bash_risk()`:
- `low` (read-only commands) → `ALLOW` in all modes including plan
- `medium` (targeted writes, redirects, package installs) → `DENY` in plan, `CONFIRM` in default, `ALLOW` in auto
- `high` (rm -rf, sudo, pipe-to-shell, killall …) → `DENY` in plan, `CONFIRM` in both default **and** auto

**4. Standard mutating logic** applies to `modify_file`, `replace_text`, `write_file`, and any plugin/MCP tools: `DENY` in plan, `CONFIRM` in default, `ALLOW` in auto.

All read-only tools (`get_time`, `read_file`, `glob`, `grep`, `ls`) are `ALLOW` unless `auto_confirm_read_only` is disabled, in which case they are `CONFIRM` in `DEFAULT` mode.