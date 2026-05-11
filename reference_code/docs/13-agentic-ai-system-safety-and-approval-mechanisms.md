# 13. Agentic AI System Safety and Approval Mechanisms: Policy-Driven Execution Guards and Interactive Confirmation

This document describes the current uncommitted delta since the previous commit, focused on a new capability:

- safety and approval for tool execution before they modify the system.

This is a foundational addition to runtime safety that mirrors how real-world AI systems gate dangerous operations.

---

## 1. High-level change in this iteration

The system now introduces a **safety layer between intent and execution**. Before any tool runs, especially those that mutate state (write, shell, network), the runtime evaluates whether the operation is:

- automatically safe and can proceed immediately,
- dangerous and must be rejected,
- or ambiguous and requires human confirmation.

This shifts the runtime from **implicit trust** toward **explicit approval gating**.

---

## 2. Conceptual foundation: the approval problem

An agentic system that can write files, execute shell commands, and modify network state inherits significant responsibility for safety.

Previous stages of this project (`01` through `12`) gave the agent increasingly powerful capabilities:

- reading and modifying files,
- executing shell commands,
- discovering and registering custom tools,
- delegating to sub-agents.

But none of these stages added **guardrails**.

`13` asks: how should we decide whether a tool invocation is safe to execute?

There are several possible answers:

1. **Trust completely** (YOLO): execute anything, assume the model is correct.
2. **Trust blindly** (AUTO): execute everything except known-dangerous patterns.
3. **Ask everything** (ON_FAILURE): execute and only ask if something goes wrong.
4. **Ask sometimes** (AUTO_EDIT): execute safe commands automatically, ask about risky ones.
5. **Ask always** (INTERACTIVE): require human approval for any mutating operation.
6. **Deny everything** (NEVER): reject all operations except provably safe ones.

`13` introduces a **policy-driven approval system** that lets the session choose which model to follow.

---

## 3. Architecture: the approval system as a gating layer

### 3.1 Ownership and scope

Approval is owned by `Session`, not by individual tools or the agent loop.

This is significant because it means:

- approval policy is session-scoped (can vary per invocation context),
- it's configured at bootstrap time (via `config.approval`),
- it's aware of runtime context (working directory, tool names, command content),
- it can integrate with interactive frontends (like the TUI).

### 3.2 The approval flow for a tool invocation

When a tool is about to execute, the flow is:

1. **Tool registry receives invoke request** with tool name, parameters, and approval manager.
2. **Approval manager evaluates the operation** using the configured approval policy.
3. **Decision is returned**: APPROVED, REJECTED, or NEEDS_CONFIRMATION.
4. **If NEEDS_CONFIRMATION**, a callback to the TUI requests interactive approval.
5. **Tool executes only if approved** or callback returns true.

This is a clean layering:

- the agent doesn't need to know approval rules exist,
- tools don't need to implement approval logic,
- the registry becomes the checkpoint.

### 3.3 Why gating happens at the registry, not in tools

An earlier design question was whether tools should implement approval themselves (via `tool.get_approval(...)`).

The current design puts approval in the registry instead. This is better because:

- **single point of policy**: one place to understand how decisions are made,
- **decouples tools from policy logic**: tools describe what they can do, not whether they should,
- **allows cross-tool policy**: some decisions need context from multiple tools or system state,
- **simplifies tool authoring**: new custom tools don't need to know about approval.

---

## 4. Approval policies: the decision rules

### 4.1 Policy as an enum

`ApprovalPolicy` is a configuration-level enum that determines runtime behavior.

Six policies are defined:

1. **YOLO**: approve everything immediately (maximum trust, maximum risk)
2. **AUTO**: approve everything except dangerous patterns (trust with guardrails)
3. **AUTO_EDIT**: approve safe patterns, ask about uncertain ones (balanced)
4. **ON_FAILURE**: approve immediately, ask only if execution fails (pragmatic)
5. **INTERACTIVE**: ask about all mutating operations (cautious)
6. **NEVER**: approve only provably safe read-only operations (paranoid)

### 4.2 Policy selection

The policy is chosen at:

- system config level (global default),
- project config level (override per cwd),
- or command line (override at runtime).

This gives the user or developer a clear precedence: system default < project override < invocation override.

### 4.3 Why multiple policies exist

Different contexts need different risk tolerances:

- **development on trusted laptop**: YOLO or AUTO work fine,
- **CI/CD pipeline**: AUTO_EDIT or INTERACTIVE (some automation, some gates),
- **production deployment agent**: INTERACTIVE or NEVER (human-in-the-loop),
- **research/exploration**: AUTO or ON_FAILURE (fast iteration).

By making policy configurable, the system supports all of these without branching code paths.

---

## 5. Safety assessment: dangerous vs. safe commands

### 5.1 Dangerous command patterns

The approval manager includes a regex-based detector for operations that are almost always destructive:

- recursive file deletion (`rm -rf`, `rmdir`),
- disk formatting and partitioning (`mkfs`, `fdisk`),
- system shutdown and reboots,
- permission changes on system roots,
- network exposure patterns,
- code execution from network sources (`curl | bash`),
- fork bombs.

These patterns trigger an automatic REJECTED decision in most policies (except YOLO).

The list is conservative but not exhaustive. It catches the most obviously dangerous operations without false-alarms.

### 5.2 Safe command patterns

Similarly, the approval manager recognizes commands that are almost always safe:

- read-only operations (ls, cat, grep, find),
- safe git operations (status, log, diff),
- safe package manager queries (npm ls, pip list),
- system information commands (date, whoami, uname),
- text processing tools (awk, sed, cut, sort).

These patterns can be auto-approved even in restrictive policies like NEVER.

### 5.3 Why pattern-based safety is useful but limited

Pattern matching is simple and efficient, but incomplete:

- it catches obvious cases but misses context-specific risks,
- `rm -rf /important/data` is matched by the danger pattern, but `rm -rf ./temp` might still be safe,
- custom scripts can't be pattern-matched reliably.

So pattern matching is a **first-line filter**, not a complete solution. The next line is **context awareness**.

---

## 6. Context awareness: beyond patterns

### 6.1 ApprovalContext: information for decisions

When a tool is about to execute, the approval manager collects context:

- **tool_name**: what operation is being requested,
- **params**: what arguments will be passed,
- **is_mutating**: does it modify state,
- **affected_paths**: which files/directories will be touched,
- **command**: if a shell tool, the actual command text,
- **is_dangerous**: has the tool itself flagged the operation as risky.

### 6.2 Path awareness

One important heuristic is **path locality**:

- if all affected paths are under the current working directory (`cwd`), the operation is local and lower-risk,
- if any path is outside `cwd`, the operation is system-wide and higher-risk.

This simple rule catches cases where a tool tries to modify `/usr/bin` or `/etc` when the session is meant to work only in a project directory.

### 6.3 Tool-provided metadata

Tools can provide additional context via `ToolConfirmation`, which includes:

- **command**: the actual shell invocation (if applicable),
- **affected_paths**: list of files this tool will touch,
- **is_dangerous**: tool's own assessment of risk.

This allows fine-grained, tool-specific safety reasoning.

---

## 7. Integration: approval in the runtime loop

### 7.1 Session initialization

`Session` now creates an `ApprovalManager` during startup:

```
self.approval_manager = ApprovalManager(
    config.approval,      # policy from config
    self.config.cwd       # working directory context
)
```

This means every session has a gating mechanism, not just unsafe ones.

### 7.2 Agent to registry handoff

When the agent decides to invoke a tool, it calls:

```
result = await registry.invoke(
    tool_name,
    params,
    cwd,
    approval_manager      # now passed here
)
```

The approval manager is an optional parameter. If absent, approval is skipped (backward-compatible).

### 7.3 Interactive confirmation callback

The TUI can provide a callback for human approval:

```python
async with Agent(
    config,
    confirmation_callback=self.tui.handle_confirmation
) as agent:
    ...
```

This callback is stored on the agent and forwarded to the session's approval manager. When a decision requires NEEDS_CONFIRMATION, the callback is invoked to ask the user.

### 7.4 Approval failure modes

If approval is REJECTED:

- tool does not execute,
- a ToolResult is returned with error metadata,
- the agent sees the rejection in its context,
- it can retry or choose a different approach.

This preserves the normal tool-result flow; approval rejection looks like any other error to the agent.

---

## 8. Decision logic: how policies translate to decisions

### 8.1 The decision tree

For a given operation, the approval manager follows this logic:

1. **Check mutating status**: if read-only, always APPROVED (no harm done).
2. **Check command danger**: if dangerous patterns match, consult policy:
   - YOLO: APPROVED anyway,
   - any other: REJECTED.
3. **Check path locality**: if all paths are under cwd, consult policy:
   - AUTO, AUTO_EDIT, ON_FAILURE: APPROVED,
   - INTERACTIVE, NEVER: maybe NEEDS_CONFIRMATION.
4. **Check safe patterns**: if matches, most policies auto-approve.
5. **Default**: NEEDS_CONFIRMATION (ask the user).

This tree allows policies to define different thresholds while sharing the same evaluation logic.

### 8.2 Why this structure matters

The structure ensures:

- **consistent decisions across policies**: the same operation is evaluated using the same rules, just with different thresholds,
- **predictable behavior**: users can reason about what their chosen policy does,
- **extensibility**: new policies can be added by just changing the thresholds, not the logic.

---

## 9. Important nuances and limitations

### 9.1 Pattern matching is heuristic, not exhaustive

Dangerous patterns might have false positives or false negatives:

- `rm -rf /tmp/cache` might be safe, but matches the danger pattern,
- a custom script that reformats a disk might not match any pattern,
- obfuscated commands (e.g., aliases, variables) might evade detection.

### 9.2 Approval is synchronous, not integrated with the event stream

Currently, the confirmation callback is synchronous and blocks execution. A future improvement would integrate approval requests into the event stream so the TUI can display them alongside agent reasoning.

### 9.3 Rejected operations appear as errors, not as special events

When a tool is rejected or a user denies approval, it looks to the agent like a normal tool error. The agent doesn't have a specific event type for "approval denied" so it can't easily retry or pivot.

### 9.4 Policy configuration is per-session, not per-tool

All tools in a session use the same approval policy. A future refinement might allow per-tool policies (e.g., "shell commands require approval, but file reads don't").

### 9.5 Confirmation callback is optional but approval always runs

If no callback is provided, NEEDS_CONFIRMATION decisions default to "not approved". This is safe but means interactive policies don't work without a callback.

---

## 10. Conceptual progression from `12` to `13`

The progression now looks like:

1. **`01`-`06`**: core agent runtime and session model
2. **`07`-`09`**: expanding tool surfaces (builtin, local discovery, web)
3. **`10`-`12`**: plugin architecture (custom tools, sub-agents, discovery)
4. **`13`**: **safety layer** gating execution

This is a natural progression:

- once the agent can do things (phases 1-3),
- and can do them flexibly (phase 3),
- the next question is: which things should it be allowed to do?

`13` answers that question via policy and approval.

---

## 11. Delta summary table

| Aspect | Before `13` | After `13` |
|---|---|---|
| Tool execution safety | No checks; trust the agent | Gated by approval manager with configurable policy |
| Dangerous command detection | None | Regex patterns for destructive operations |
| Safe command recognition | None | Regex patterns for provably safe operations |
| Session responsibility | Context and tools | Context, tools, **and approval** |
| Interactive confirmation | Not supported | Supported via callback to TUI |
| Error handling for rejection | N/A | Treated as tool error with metadata |
| Risk assessment | N/A | Path locality, command patterns, tool metadata |

---

## 12. Why this matters conceptually

Approval is not a cosmetic feature. It shifts the system from:

- **"the agent will do what the model decides"**

to:

- **"the agent can attempt what the model decides, subject to policy constraints"**.

This is how real-world AI systems operate in production:

- they run with bounded autonomy,
- they have explicit safety thresholds,
- they escalate decisions that exceed those thresholds,
- they log and audit what was approved.

`13` introduces the first of these concepts (bounded autonomy and explicit thresholds). The others (escalation, logging, auditing) are natural next steps.

---

## 13. Natural continuation points for a future `14`

Natural next steps after this iteration would be:

- logging and auditing of approval decisions (who approved what, when, why),
- per-tool approval policies (different rules for different tool classes),
- approval explanation (showing the agent why a tool was rejected),
- approval statistics (how many operations were auto-approved vs. required confirmation),
- policy templates (predefined policies for common scenarios like CI/CD, development, production),
- approval by signature (trusted scripts or commands that can be pre-approved),
- or rollback support (agent can undo operations that approval later contradicts).

Any of these would continue the movement from **binary approve/reject decisions** toward **sophisticated, auditable safety infrastructure**.

---

## 14. Key takeaways

1. The main addition in this delta is a policy-driven approval system that gates tool execution.

2. `ApprovalManager` evaluates operations against a configured `ApprovalPolicy` and collects safety context before making decisions.

3. Six policies are defined (YOLO, AUTO, AUTO_EDIT, ON_FAILURE, INTERACTIVE, NEVER) to support different risk tolerances and use cases.

4. Safety assessment combines regex-based pattern matching with context awareness (path locality, tool metadata).

5. Approval is owned by `Session` and integrated at the `ToolRegistry.invoke()` checkpoint, not scattered across tools.

6. Interactive confirmation is supported via a callback mechanism, allowing the TUI to ask the user before proceeding.

7. Rejected operations are treated as normal tool errors, preserving the event-based runtime model.

8. The current implementation is foundational; richer features like logging, auditing, per-tool policies, and approval explanations are natural future additions.

9. Conceptually, `13` moves the system from implicit trust toward explicit bounded autonomy, which is essential for production-grade agentic systems.
