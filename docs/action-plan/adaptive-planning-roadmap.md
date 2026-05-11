# Adaptive Planning — Simple Direct Response vs. Plan-Then-Execute

This document is a step-by-step implementation roadmap for giving Nexus the ability to **classify every incoming query** and decide whether to:

1. **Respond directly** — simple queries answered in a single model call with no planning overhead
2. **Plan then execute** — complex queries broken into a reasoning/planning phase (LLM call 1) followed by a separate execution phase (LLM call 2+)

The result is a two-tier agent loop where the model itself decides the route, the user can see and approve the plan before execution begins, and every existing guardrail, permission gate, and hook continues to work unchanged.

---

## Current State (Baseline)

The current `Agent.run()` loop in `nexus/runtime/agent.py`:

```
user message → single LLM call → tool calls or final answer → repeat
```

There is no query classification. The model gets the full tool list and message history on every turn and decides ad-hoc whether to call tools or answer directly. Complex queries that should decompose into steps are handled by the model's internal chain-of-thought, invisible to the runtime. If the model gets confused or does too many things at once, there is no checkpoint.

---

## Design Overview

```
user message
    │
    ▼
┌─────────────────────────────────────┐
│  QueryClassifier  (LLM call 0)      │  ← lightweight, no tools, fast
│  Returns: SIMPLE | COMPLEX          │
└──────────────┬──────────────────────┘
               │
       ┌───────┴───────┐
       │               │
   SIMPLE           COMPLEX
       │               │
       ▼               ▼
  Direct answer    PlanningAgent (LLM call 1, no tools)
  (existing loop)  Produces: Plan  ──→ show plan to user (optional confirm)
                                   │
                                   ▼
                             ExecutionAgent (LLM call 2+, with tools)
                             Executes each step with full guardrails
```

### Key design decisions

- **QueryClassifier is a small, cheap LLM call** — no tool schemas, no history context, just the user message and a one-line instruction. Uses the same provider/model as the main agent to avoid a second client.
- **Planning phase has no tools** — the planner cannot accidentally execute. It reasons and writes a structured plan only.
- **The plan is an observable event** — emitted as `plan_produced` `AgentEvent` so the REPL can display it and optionally ask for approval before execution begins.
- **Execution phase is the existing agent loop** — the plan is injected as a system-prompt addition so tools, permissions, hooks, and confirmation flow are unchanged.
- **Complexity threshold is configurable** — users can force `always-plan`, `never-plan`, or `auto` via config, env var, or `/mode` slash command extension.
- **FakeModelClient supports both phases** for offline/CI testing.

---

## Scope of Changes

```
nexus/models.py                          ← new: Plan, PlanStep, QueryComplexity
nexus/config/defaults.py                 ← new: planning_mode, planning_threshold, show_plan_before_execute
nexus/config/loader.py                   ← new: AGENT_PLANNING_MODE env var
nexus/runtime/agent.py                   ← new: QueryClassifier, PlanningAgent; updated: Agent.run()
nexus/runtime/execution.py               ← update: PlanningMode enum
nexus/runtime/repl.py                    ← new: render plan_produced event
nexus/runtime/slash_commands.py          ← new: /mode planning subcommand; PROVIDER_SETTABLE_PARAMS
nexus/prompts.py                         ← new: build_classifier_prompt(), build_planner_prompt()
nexus/observability/logging.py           ← log plan_produced, plan_approved events
nexus/integrations/fake_model.py         ← fake classify and plan responses
tests/test_planning.py                   ← new
tests/test_agent.py                      ← extend with planning-mode assertions
```

---

## Phase 1 — Data Model

### Step 1.1 — `QueryComplexity` enum

**File:** `nexus/models.py`

```python
from enum import Enum

class QueryComplexity(str, Enum):
    SIMPLE  = "simple"   # respond directly
    COMPLEX = "complex"  # plan then execute
```

### Step 1.2 — `PlanStep` and `Plan`

```python
@dataclass(slots=True, frozen=True)
class PlanStep:
    index: int           # 1-based step number
    description: str     # human-readable action description
    tool_hint: str = ""  # optional — which tool this step is likely to use
    rationale: str = ""  # why this step is needed

@dataclass(slots=True, frozen=True)
class Plan:
    query_summary: str          # one-line restatement of what the user asked
    steps: tuple[PlanStep, ...]
    raw_text: str               # full planner response for display/debug
    complexity: QueryComplexity = QueryComplexity.COMPLEX
```

### Step 1.3 — New `AgentEvent` kinds

No changes to `AgentEvent` struct. New `kind` strings used in `Agent.run()`:

| `event.kind` | `event.payload` | When emitted |
|---|---|---|
| `complexity_classified` | `QueryComplexity` | After classifier call |
| `plan_produced` | `Plan` | After planner call, before execution |
| `plan_approved` | `Plan` | After user approves (or auto-approved) |
| `plan_step_started` | `PlanStep` | Before each step executes |
| `plan_step_completed` | `PlanStep` | After each step completes |

---

## Phase 2 — Config and Environment Variables

### Step 2.1 — New fields in `AgentConfig`

**File:** `nexus/config/defaults.py`

```python
# Planning mode:
#   "auto"        — classify each query and plan only when complex (default)
#   "always"      — always plan even for simple queries
#   "never"       — never plan; use the existing single-pass loop for everything
planning_mode: str = "auto"

# Minimum number of steps a query must appear to need before being classified COMPLEX.
# The classifier uses this as guidance: "a query needing more than N distinct steps is complex."
planning_threshold: int = 2

# Show the plan to the user and ask for approval before executing.
# In auto mode: always shown.
# In headless / --auto-confirm mode: plan is logged but execution proceeds immediately.
show_plan_before_execute: bool = True

# Maximum number of steps in a single plan.
max_plan_steps: int = 10
```

### Step 2.2 — Environment variables

**File:** `nexus/config/loader.py`

```python
"AGENT_PLANNING_MODE":           ("planning_mode",            str),
"AGENT_PLANNING_THRESHOLD":      ("planning_threshold",       int),
"AGENT_SHOW_PLAN_BEFORE_EXEC":   ("show_plan_before_execute", bool),
"AGENT_MAX_PLAN_STEPS":          ("max_plan_steps",           int),
```

### Step 2.3 — `.nexus/config.toml` examples

```toml
planning_mode = "auto"            # "auto" | "always" | "never"
planning_threshold = 2
show_plan_before_execute = true
max_plan_steps = 10
```

Or via environment:

```bash
AGENT_PLANNING_MODE=auto
AGENT_SHOW_PLAN_BEFORE_EXEC=false   # skip plan approval in headless runs
```

---

## Phase 3 — Prompt Templates

**File:** `nexus/prompts.py`

### Step 3.1 — Classifier prompt

The classifier call is intentionally lean: no tool list, no carry-over context, no skill injections. Just the user message and a two-sentence instruction:

```python
CLASSIFIER_SYSTEM = (
    "You classify user requests as SIMPLE or COMPLEX.\n"
    "SIMPLE: the question can be answered directly with at most one tool call or no tool calls.\n"
    "COMPLEX: the request requires multiple steps, multiple tool calls, multi-file edits, or "
    "significant reasoning before acting.\n"
    "Respond with exactly one word: SIMPLE or COMPLEX."
)

def build_classifier_prompt(user_message: str) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for the classifier call."""
    return CLASSIFIER_SYSTEM, user_message
```

### Step 3.2 — Planner prompt

The planner gets the full system context (tools list, workspace knowledge, mode) but **no tool schemas** in the `tools` API field — only their names and descriptions in the system prompt. This prevents the model from accidentally calling them:

```python
PLANNER_SYSTEM_TEMPLATE = """\
You are a planning agent. Your only job is to produce a step-by-step plan.
Do NOT execute any steps. Do NOT call any tools. Just reason and write the plan.

Available tools (for reference only — do not call them):
{tool_descriptions}

Rules:
- Each step must be a clear, atomic action.
- Use at most {max_steps} steps.
- If a step will use a tool, name it in parentheses: (tool: write_file)
- End with a one-line summary of the expected outcome.

Respond in this format:
PLAN:
1. <step description> (tool: <tool_name_if_any>)
2. <step description>
...
OUTCOME: <one-line expected result>
"""

def build_planner_prompt(
    user_message: str,
    tool_descriptions: str,
    max_steps: int,
) -> str:
    return PLANNER_SYSTEM_TEMPLATE.format(
        tool_descriptions=tool_descriptions,
        max_steps=max_steps,
    )
```

---

## Phase 4 — QueryClassifier

**File:** `nexus/runtime/agent.py`

```python
class QueryClassifier:
    """Lightweight single-call classifier. No tools, no tool schemas."""

    def __init__(self, model_client: ModelClient) -> None:
        self.model_client = model_client

    async def classify(self, user_message: str, model_name: str) -> QueryComplexity:
        system_prompt, _ = build_classifier_prompt(user_message)
        request = RuntimeRequest(
            model_name=model_name,
            system_prompt=system_prompt,
            messages=(Message(role="user", content=user_message),),
            tool_schemas=(),          # no tools — pure text response
            temperature=0.0,
            max_output_tokens=10,     # we only need one word back
        )
        response = await self.model_client.complete(request)
        raw = response.message.content.strip().upper()
        if "COMPLEX" in raw:
            return QueryComplexity.COMPLEX
        return QueryComplexity.SIMPLE   # default to simple on ambiguity
```

### Why `max_output_tokens=10`?

The classifier only needs to return `SIMPLE` or `COMPLEX`. Limiting tokens reduces cost and latency. Any response containing "COMPLEX" is treated as complex; all else as simple.

---

## Phase 5 — PlanningAgent

**File:** `nexus/runtime/agent.py`

```python
class PlanningAgent:
    """Produces a structured Plan from a user query. Never calls tools."""

    def __init__(self, model_client: ModelClient) -> None:
        self.model_client = model_client

    async def plan(
        self,
        user_message: str,
        tool_registry: ToolRegistry,
        model_name: str,
        max_steps: int = 10,
    ) -> Plan:
        tool_descriptions = "\n".join(
            f"  - {r.name}: {r.tool.description}"
            for r in tool_registry.records()
        )
        system_prompt = build_planner_prompt(user_message, tool_descriptions, max_steps)
        request = RuntimeRequest(
            model_name=model_name,
            system_prompt=system_prompt,
            messages=(Message(role="user", content=user_message),),
            tool_schemas=(),     # CRITICAL: no tool schemas → model cannot call tools
            temperature=0.3,     # slight temperature for creative decomposition
        )
        response = await self.model_client.complete(request)
        return _parse_plan(response.message.content, user_message)


def _parse_plan(raw_text: str, query_summary: str) -> Plan:
    """Parse the PLAN: / OUTCOME: format into a typed Plan."""
    steps: list[PlanStep] = []
    lines = raw_text.splitlines()
    in_plan = False
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("PLAN:"):
            in_plan = True
            continue
        if stripped.upper().startswith("OUTCOME:"):
            in_plan = False
            continue
        if in_plan and stripped and stripped[0].isdigit():
            # Parse "1. Do something (tool: write_file)"
            dot_idx = stripped.index(".") if "." in stripped else -1
            description = stripped[dot_idx + 1:].strip() if dot_idx >= 0 else stripped
            tool_hint = ""
            if "(tool:" in description.lower():
                start = description.lower().index("(tool:") + 6
                end = description.index(")", start) if ")" in description[start:] else len(description)
                tool_hint = description[start:end].strip()
                description = description[:description.lower().index("(tool:")].strip()
            steps.append(PlanStep(
                index=len(steps) + 1,
                description=description,
                tool_hint=tool_hint,
            ))
    return Plan(
        query_summary=query_summary,
        steps=tuple(steps),
        raw_text=raw_text,
        complexity=QueryComplexity.COMPLEX,
    )
```

---

## Phase 6 — Updated `Agent.run()` Loop

**File:** `nexus/runtime/agent.py`

The main agent loop gains a planning prefix that runs only when `planning_mode` is not `"never"`:

```python
async def run(
    self,
    messages: list[Message],
    context: ToolExecutionContext,
    *,
    system_prompt: str,
    model_name: str,
    mode: ExecutionMode,
    planning_mode: str = "auto",        # NEW
    max_plan_steps: int = 10,           # NEW
    show_plan: bool = True,             # NEW — emit plan_produced event for REPL display
    ...existing params...
) -> AsyncGenerator[AgentEvent, None]:

    user_message = messages[-1].content if messages else ""
    plan: Plan | None = None

    # ── Phase A: classify ──────────────────────────────────────────────────────
    if planning_mode == "auto" and user_message:
        classifier = QueryClassifier(self.model_client)
        complexity = await classifier.classify(user_message, model_name)
        yield AgentEvent(kind="complexity_classified", payload=complexity)
    elif planning_mode == "always":
        complexity = QueryComplexity.COMPLEX
        yield AgentEvent(kind="complexity_classified", payload=complexity)
    else:
        complexity = QueryComplexity.SIMPLE

    # ── Phase B: plan (only for complex queries) ───────────────────────────────
    if complexity is QueryComplexity.COMPLEX:
        planner = PlanningAgent(self.model_client)
        plan = await planner.plan(user_message, self.tool_registry, model_name, max_plan_steps)
        yield AgentEvent(kind="plan_produced", payload=plan)
        # Execution continues after the caller handles plan_produced
        # (REPL will show the plan and optionally ask for approval)
        # Signal that we need approval before proceeding:
        if show_plan:
            yield AgentEvent(kind="confirmation_requested", payload=ConfirmationRequest(
                kind=ConfirmationKind.APPROVAL,
                tool_name="__plan__",
                prompt="Approve this plan and begin execution?",
                reason="Complex query — plan requires approval before execution.",
                payload={"plan_steps": str(len(plan.steps))},
                arguments={"plan": plan.raw_text},
            ))
            return   # caller sends approval; run() is re-entered with plan injected

    # ── Phase C: execute ───────────────────────────────────────────────────────
    # Inject plan into system prompt so the execution model follows the agreed steps.
    if plan is not None:
        system_prompt = system_prompt + "\n\n## Agreed Execution Plan\n\n" + plan.raw_text
    yield AgentEvent(kind="thinking_started")
    # ... existing tool-call loop unchanged from here ...
```

### Plan approval flow

When `plan_produced` is emitted and `show_plan = True`, the REPL handles it the same way as any `confirmation_requested` event:
- it renders the plan using Rich
- it prompts `Approve and execute? [y/N]:`
- if approved: re-invokes the turn with the plan injected into the system prompt and `show_plan=False` so execution runs directly
- if denied: cancels cleanly

In `--auto-confirm` / auto mode: plan is logged but execution proceeds immediately without prompting.

---

## Phase 7 — REPL Rendering

**File:** `nexus/runtime/repl.py`

### Step 7.1 — Render `complexity_classified`

```python
elif event.kind == "complexity_classified" and show_tool_calls:
    label = "complex → planning" if event.payload is QueryComplexity.COMPLEX else "simple → direct"
    console.print(f"[dim]⋯ query classified: {label}[/dim]")
```

### Step 7.2 — Render `plan_produced`

```python
elif event.kind == "plan_produced":
    plan: Plan = event.payload
    console.print()
    console.print(Rule("[bold blue]Execution Plan[/bold blue]", style="blue"))
    console.print(f"  [bold]Goal:[/bold] {plan.query_summary}")
    console.print()
    for step in plan.steps:
        tool_note = f" [dim](tool: {step.tool_hint})[/dim]" if step.tool_hint else ""
        console.print(f"  [cyan]{step.index}.[/cyan] {step.description}{tool_note}")
    console.print()
```

### Step 7.3 — Reuse existing `confirmation_requested` for plan approval

The plan approval uses `tool_name="__plan__"` and `arguments={"plan": ...}`. The existing `confirmation_requested` renderer already shows arguments, so the plan text will appear truncated to 150 chars in the approval box — that's intentional (the full plan was already shown via `plan_produced`).

### Step 7.4 — Render `plan_step_started` / `plan_step_completed`

```python
elif event.kind == "plan_step_started" and show_tool_calls:
    step: PlanStep = event.payload
    console.print(f"[dim blue]▶ Step {step.index}: {step.description}[/dim blue]")

elif event.kind == "plan_step_completed" and show_tool_calls:
    step: PlanStep = event.payload
    console.print(f"[dim]  ✓ Step {step.index} done[/dim]")
```

---

## Phase 8 — Slash Command Integration

**File:** `nexus/runtime/slash_commands.py`

### Step 8.1 — Add `planning_mode` to settable params

```python
PROVIDER_SETTABLE_PARAMS: frozenset[str] = frozenset({
    ...existing...,
    "planning_mode",
    "show_plan_before_execute",
    "max_plan_steps",
})
```

Usage inside the REPL:

```text
/provider set planning_mode auto
/provider set planning_mode always
/provider set planning_mode never
/provider set show_plan_before_execute false
```

### Step 8.2 — Extend `/mode` to cover planning

The existing `/mode` command manages `plan|default|auto` (execution mode). Add `planning` as a sub-namespace:

```text
/mode planning auto        → set planning_mode = "auto"
/mode planning always      → set planning_mode = "always"
/mode planning never       → set planning_mode = "never"
```

Update `handle_mode` in `slash_commands.py`:

```python
if sub == "planning" and len(args) >= 2:
    value = args[1].lower()
    if value not in {"auto", "always", "never"}:
        state.console.print("[red]planning_mode must be auto, always, or never[/red]")
        return
    state.config.planning_mode = value
    state.console.print(f"Planning mode set to: [bold]{value}[/bold]")
    return
```

Update the help table:

```python
("planning auto|always|never", "Set query planning mode for this session.", "/mode planning auto"),
```

---

## Phase 9 — Pass Config Flags Through the Call Stack

The planning flags need to flow from `ReplState.config` into `Agent.run()`. Update the call site in `nexus/runtime/repl.py` where `_stream_turn_live` / `collect_turn_events` invoke `agent.run()`:

```python
# Inside _stream_turn_live / collect_turn_events:
async for event in agent.run(
    state.history,
    context,
    system_prompt=state.current_system_prompt,
    model_name=state.config.model_name,
    mode=state.mode,
    planning_mode=state.config.planning_mode,          # NEW
    max_plan_steps=state.config.max_plan_steps,        # NEW
    show_plan=state.config.show_plan_before_execute,   # NEW
    ...existing...
):
```

---

## Phase 10 — Observability Hooks

**File:** `nexus/observability/logging.py`

Add hook handlers for two new hook event kinds:

```python
# When a plan is produced:
if event_kind == HookEvent.NOTIFICATION and payload.get("event") == "plan_produced":
    log({
        "event": "plan_produced",
        "session_id": payload["session_id"],
        "turn_id": payload.get("turn_id"),
        "step_count": payload["step_count"],
        "query_summary": payload["query_summary"][:120],
    })

# When a plan step completes:
if event_kind == HookEvent.NOTIFICATION and payload.get("event") == "plan_step_completed":
    log({
        "event": "plan_step_completed",
        "session_id": payload["session_id"],
        "step_index": payload["step_index"],
        "tool_hint": payload["tool_hint"],
        "duration_ms": payload["duration_ms"],
    })
```

Emit these from `Agent.run()` via `self.hooks.emit(HookEvent.NOTIFICATION, {...})` at the same points as the new `AgentEvent` yields.

---

## Phase 11 — Fake Model Support

**File:** `nexus/integrations/fake_model.py`

The fake model must return coherent classifier and planner responses for offline tests:

```python
FAKE_CLASSIFY_SIMPLE = "SIMPLE"
FAKE_CLASSIFY_COMPLEX = "COMPLEX"

FAKE_PLAN_RESPONSE = """\
PLAN:
1. Read the relevant files (tool: read_file)
2. Identify the changes needed (tool: grep)
3. Apply the changes (tool: write_file)
OUTCOME: The requested modifications are applied correctly.
"""

class FakeModelClient:
    def __init__(self, responses: list[str] | None = None, *, always_complex: bool = False) -> None:
        self._responses = responses or []
        self._index = 0
        self.always_complex = always_complex

    async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
        # Detect classifier call (max_output_tokens == 10, no tools)
        if request.max_output_tokens == 10 and not request.tool_schemas:
            content = FAKE_CLASSIFY_COMPLEX if self.always_complex else FAKE_CLASSIFY_SIMPLE
            return RuntimeResponse(message=Message(role="assistant", content=content))

        # Detect planner call (no tool_schemas but longer output allowed)
        if not request.tool_schemas and request.temperature == 0.3:
            return RuntimeResponse(message=Message(role="assistant", content=FAKE_PLAN_RESPONSE))

        # Normal completion
        if self._index < len(self._responses):
            content = self._responses[self._index]
            self._index += 1
        else:
            content = "OK"
        return RuntimeResponse(message=Message(role="assistant", content=content))
```

---

## Phase 12 — Tests

### New file: `tests/test_planning.py`

```
- test QueryClassifier returns SIMPLE for "what time is it"
- test QueryClassifier returns COMPLEX for "refactor all Python files in src/ to use dataclasses"
- test QueryClassifier defaults to SIMPLE on ambiguous/empty model response
- test PlanningAgent.plan() returns a Plan with at least one step
- test _parse_plan() extracts steps and tool_hints correctly from well-formed text
- test _parse_plan() handles missing OUTCOME: gracefully
- test Agent.run() with planning_mode="never" skips classification and planning
- test Agent.run() with planning_mode="always" always produces a plan_produced event
- test Agent.run() with planning_mode="auto" and SIMPLE response skips planning
- test Agent.run() with planning_mode="auto" and COMPLEX response emits plan_produced then confirmation_requested
- test plan approval re-injects plan into system_prompt
- test plan denial stops the turn cleanly
- test max_plan_steps limit is respected (plan truncated to configured max)
```

### Updates to existing tests

```
tests/test_agent.py        — add planning_mode="never" to all existing run() calls to preserve current behavior
tests/test_config.py       — assert planning_mode, show_plan_before_execute, max_plan_steps load from toml and env
tests/test_slash_commands.py — assert /mode planning auto changes state.config.planning_mode
tests/test_repl.py         — assert plan_produced event renders correctly (Rich Rule + step list)
```

---

## Phase 13 — Headless Mode Behavior

In headless (`--prompt`) runs:

- `show_plan_before_execute = false` by default when `--auto-confirm` is passed
- If `show_plan_before_execute = true` and a TTY is available, the plan is printed and approval is requested inline (same as REPL)
- If no TTY (piped/CI): plan is logged to observability and execution proceeds — same behavior as other CONFIRM gates in non-interactive mode

---

## Phase 14 — Documentation Updates

After all above phases are implemented and tests pass, update:

- `README.md` — add `planning_mode` to config table and `/mode planning` to slash command table
- `docs/openai-code-tutorial/09-plan-mode-and-auto-mode.md` — add "Adaptive Planning" section distinguishing execution mode from planning mode
- `next_roadmap.md` — mark adaptive planning as implemented

---

## Implementation Order and Dependencies

```
Phase 1  (models)         — no deps — do first
Phase 2  (config)         — after Phase 1
Phase 3  (prompts)        — after Phase 1 — standalone, no runtime deps
Phase 4  (classifier)     — after Phase 1, 3
Phase 5  (planner)        — after Phase 1, 3
Phase 6  (agent loop)     — after Phase 4, 5
Phase 7  (REPL render)    — after Phase 6
Phase 8  (slash commands) — after Phase 2
Phase 9  (call stack)     — after Phase 6, 7
Phase 10 (observability)  — after Phase 6
Phase 11 (fake model)     — after Phase 4, 5 — needed for tests
Phase 12 (tests)          — after all above
Phase 13 (headless)       — after Phase 6, 7
Phase 14 (docs)           — last
```

Recommended commit order: 1 → 2 → 3 → 4 → 5 → 11 → 12 (unit tests for classifier/planner) → 6 → 7 → 8 → 9 → 10 → 12 (integration tests) → 13 → 14

---

## Risk and Mitigation

| Risk | Mitigation |
|---|---|
| Classifier adds latency to every query | `max_output_tokens=10`, no tools, no history — typically < 200 ms; cache result per turn |
| Classifier misclassifies a simple query as complex | User can deny the plan and proceed; `/mode planning never` disables it |
| Planner produces malformed output (no PLAN: header) | `_parse_plan()` returns an empty-steps `Plan`; agent falls back to direct execution with a warning |
| Plan approval interrupts headless runs | `show_plan_before_execute=false` by default with `--auto-confirm`; or piped mode skips approval |
| Plan is stale if the user edits context between planning and execution | Plan is re-injected as system-prompt text on every execution turn; model can deviate if context changed |
| `__plan__` tool name in confirmation_requested confuses permission checker | `PermissionChecker.evaluate()` is only called on real registered tools; `__plan__` is handled before `evaluate()` is reached |
| FakeModelClient cannot distinguish classifier from planner calls | Discriminate on `max_output_tokens == 10` (classifier) vs `temperature == 0.3` (planner) in fake client |

---

## UX Examples

### Simple query (planning_mode = auto)

```
> what is the current time?
⋯ query classified: simple → direct
The current time is 2026-04-28T14:32:01Z.
```

### Complex query (planning_mode = auto)

```
> refactor all Python files in nexus/ to replace print() calls with console.print()

⋯ query classified: complex → planning

──────────────────── Execution Plan ──────────────────────
  Goal: Replace print() with console.print() in all nexus/ Python files

  1. List all Python files in nexus/ (tool: glob)
  2. Search each file for print( calls (tool: grep)
  3. Read each affected file (tool: read_file)
  4. Apply text replacements (tool: replace_text)
  5. Verify no plain print() calls remain (tool: grep)

──────────────────────────────────────────────────────────
  Approve and execute? [y/N]: y

▶ Step 1: List all Python files in nexus/
  ↳ nexus/app.py, nexus/runtime/agent.py, ...
  ✓ Step 1 done
▶ Step 2: Search each file for print( calls
  ...
```

### Always-plan mode

```bash
/mode planning always
# or
AGENT_PLANNING_MODE=always uv run nexus --prompt "what time is it"
```

Even simple queries produce a plan (useful for audit-heavy environments).

### Never-plan mode

```bash
/mode planning never
# or
AGENT_PLANNING_MODE=never uv run nexus
```

Reverts to the original single-pass loop for all queries.

---

## Definition of Done

- [ ] `QueryClassifier` returns `SIMPLE` for one-shot questions and `COMPLEX` for multi-step requests
- [ ] `PlanningAgent` produces a typed `Plan` with numbered steps and optional tool hints
- [ ] Plan is displayed in the REPL with a Rich Rule header before execution begins
- [ ] User can approve or deny the plan; denial cancels the turn cleanly
- [ ] Approval injects the plan into the system prompt and execution proceeds through existing tool/permission loop
- [ ] `planning_mode = "never"` skips all classification and planning — existing behavior preserved
- [ ] `/mode planning auto|always|never` works live in the REPL without restart
- [ ] `AGENT_PLANNING_MODE=always` env var activates always-plan mode
- [ ] `show_plan_before_execute = false` skips plan approval (auto-proceeds, plan still logged)
- [ ] Classifier call uses `max_output_tokens=10` and no tool schemas
- [ ] Planner call uses no tool schemas (model cannot accidentally call tools during planning)
- [ ] `FakeModelClient` supports offline testing of classifier and planner paths
- [ ] All existing 197 tests still pass (add `planning_mode="never"` to all existing `agent.run()` calls)
- [ ] New `tests/test_planning.py` tests pass
