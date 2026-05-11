# 15. Agentic AI System Loop Detection and Loop Breaking: Repetition Guards Against Agentic Hallucination in the Active Agentic Loop

This document is a continuation of:

- `docs/01-agentic-ai-system-basics.md`
- `docs/02-agentic-ai-system-runtime-and-error-paths.md`
- `docs/03-agentic-ai-system-context-management-and-prompt-construction.md`
- `docs/04-agentic-ai-system-tool-calling-and-execution-runtime.md`
- `docs/05-agentic-ai-system-configuration-and-environmental-context-management.md`
- `docs/06-agentic-ai-system-session-runtime-ownership-and-state-boundaries.md`
- `docs/07-agentic-ai-system-builtin-tool-expansion-and-interactive-tool-observability.md`
- `docs/08-agentic-ai-system-search-and-discovery-tool-expansion.md`
- `docs/09-agentic-ai-system-web-discovery-and-tool-result-specialization.md`
- `docs/10-agentic-ai-system-subagent-delegation-and-prompt-surface-specialization.md`
- `docs/11-agentic-ai-system-mcp-tool-integration-and-runtime-management.md`
- `docs/12-agentic-ai-system-context-compaction-summary-restoration-and-tool-output-pruning.md`
- `docs/13-agentic-ai-system-safety-and-approval-mechanisms.md`
- `docs/14-agentic-ai-system-hook-system.md`

`01` established the client and event foundations.
`02` introduced the CLI/agent/TUI runtime shell.
`03` added context management and prompt construction.
`04` introduced structured tool calling.
`05` expanded configuration and environment-aware runtime setup.
`06` moved ownership of key runtime components under `Session`.
`07` expanded builtin tools and interactive observability.
`08` added stronger local discovery tools.
`09` expanded discovery outward through web tooling.
`10` added local custom tool discovery.
`11` added MCP-backed external tool integration.
`12` introduced context compaction and pruning, and also added `LoopDetector` as groundwork for future runtime resilience.
`13` added approval and safety controls for mutating operations.
`14` added hooks as event-driven runtime extension points.
`15` explains the next step in long-running agent reliability: the previously introduced loop-detection groundwork is now wired into the active agentic loop so the system can recognize repetitive behavior and inject a loop-break instruction instead of blindly continuing the same pattern.

In this stage, the code adds:

- session-owned activation of `LoopDetector`,
- response recording in `Agent._agentic_loop()`,
- tool-call recording in `Agent._agentic_loop()`,
- a post-tool-cycle loop check,
- and loop-break prompt injection via `create_loop_breaker_prompt(...)`.

This document serves two purposes:

1. explain what loop detection is, why it matters in agentic systems, and why it helps against looping hallucinations, and
2. show exactly how this repository implements it today, including the current heuristics and edge cases.

---

## 1. High-level change in this iteration

The project shifts from a **tool-rich long-running agent runtime** to a **tool-rich long-running runtime that also watches for self-repetition and nudges itself out of loops**.

Previous effective flow (from `14` + the earlier groundwork in `12`):

`main.py (CLI)` -> `Agent(config)` -> `Session(config)` -> model reasons -> tool calls happen -> context grows -> hooks/safety/compaction may run -> loop-detection logic exists in the codebase but is not yet part of the active turn loop

Current flow (this `15` step):

`main.py (CLI)` -> `Agent(config)` -> `Session(config + LoopDetector)` -> each turn records assistant responses and tool calls as abstract action signatures -> after a tool cycle the runtime checks whether the recent action history suggests repetition -> if a loop is detected, a synthetic loop-break instruction is appended to context and the agent continues with a fresh chance to choose a different approach

That is the main conceptual shift.

This change does **not** add a new external tool or a new major subsystem boundary.
Instead, it makes the existing agent loop more self-regulating.

That matters because many agent failures are not crashes.
They are repetitive failures such as:

- calling the same tool with the same arguments over and over,
- generating the same response again and again,
- bouncing between a tiny sequence of actions without making progress,
- or repeatedly retrying a failing strategy because the model lacks a strong interrupt signal.

This is the class of problem the new runtime wiring is trying to address.

---

## 2. What looping hallucination means in agent systems

When people talk about “looping hallucination” in an agentic runtime, they usually do not mean a traditional factual hallucination only.
They often mean a behavioral hallucination, where the model becomes trapped in a self-reinforcing pattern.

Typical examples include:

- repeatedly calling `grep` with the same pattern and path,
- re-reading the same file without using the information gained,
- repeatedly stating “I need more context” without changing strategy,
- bouncing between response and identical tool usage,
- or issuing the same failing command several turns in a row.

This is dangerous for at least four reasons:

1. **Wasted budget**
   - repeated turns burn tokens and tool time without new value.

2. **User frustration**
   - the agent appears busy but is not making progress.

3. **Operational risk**
   - repeated shell or mutating tool use can become noisy or harmful.

4. **False appearance of intelligence**
   - the system can look active while actually being stuck.

So loop detection is really a form of runtime self-observability and behavioral correction.

---

## 3. Why loop detection is needed even in a tool-capable, context-aware agent

Earlier docs already introduced mechanisms that make the runtime more capable and more robust:

- more tools,
- better session ownership,
- approval/safety policies,
- hooks,
- context compaction/pruning.

But none of those, by themselves, guarantee that the agent will stop repeating itself.

A system can still be:

- well-configured,
- tool-rich,
- safe,
- and observable,

while also being stuck in a local behavioral rut.

That is why loop detection matters as a separate concern.

It does not replace:

- safety checks,
- approval gates,
- context management,
- or tool validation.

It complements them by asking:

> is the agent continuing to take meaningfully new actions, or is it circling the same few moves?

That is the big idea behind this `15` step.

---

## 4. Change scope since the previous commit (`afff4fe` baseline)

### Existing files updated

- `core/agent/agent.py`
- `core/agent/session.py`

### Relevant pre-existing components that are now activated by this delta

- `core/context/loop_detector.py`
- `core/prompts/system.py` (`create_loop_breaker_prompt`)

### Center of gravity in this change

The center of gravity is **wiring**, not invention.

The runtime already had a loop detector helper and a loop-breaker prompt function.
What this commit does is make them part of the live agent execution path.

That is important to say clearly.

This is not “the repository learns about loops for the first time.”
It is:

- the repository had loop-related groundwork,
- and now the agent loop starts using it.

That makes `15` feel like a natural continuation of `12`, while still fitting after the more recent `13` and `14` runtime-control documents.

---

## 5. The core design idea: detect repeated action signatures, not just repeated raw text

The underlying `LoopDetector` in `core/context/loop_detector.py` uses a simple but useful abstraction:

- convert observed behavior into a string signature,
- store a bounded recent history,
- and check for suspicious repetition patterns.

### 5.1 Signature model

Each recorded event becomes a signature string.

For `tool_call` actions, the signature includes:

- the action type,
- the tool name,
- and sorted argument key/value pairs.

For `response` actions, the signature includes:

- the action type,
- and the response text.

So the loop detector is not trying to model deep semantic equivalence.
It is using a deterministic signature heuristic.

That is a reasonable first implementation because it is:

- cheap,
- explainable,
- and easy to integrate into the agent loop.

---

## 6. `Session` now owns an active loop detector

The first actual runtime change is in `core/agent/session.py`.

### 6.1 New session-owned field

`Session.__init__()` now creates:

- `self.loop_detector = LoopDetector()`

This is the correct ownership boundary.

Loop history is:

- specific to one active run/session,
- tied to the current conversation and tool usage,
- and should not leak across unrelated sessions.

So `Session` is the right place to store it.

### 6.2 Why this matters architecturally

This follows the broader pattern established in earlier docs:

- the session owns runtime-scoped state,
- and the agent orchestrates over that session-owned state.

The loop detector belongs to the same family as:

- the context manager,
- the MCP manager,
- the hook system,
- the approval manager,
- and the chat compactor.

It is another form of session-local runtime control.

---

## 7. `LoopDetector` itself: how detection currently works

Although the uncommitted diff only wires it in, understanding the active behavior requires reading `core/context/loop_detector.py`.

### 7.1 Internal state

`LoopDetector` stores:

- `max_exact_repeats = 3`
- `max_cycle_length = 3`
- `_history = deque(maxlen=20)`

So the detector only reasons over the most recent 20 action signatures.

That bounded history is a good tradeoff for an early implementation:

- enough to spot short loops,
- small enough to stay cheap.

### 7.2 Recording tool calls

When `record_action("tool_call", ...)` is called:

- the signature starts with `tool_call`,
- then includes the tool name,
- then includes sorted `key=value` argument fragments.

This means two calls like:

- `read_file(path="main.py")`
- `read_file(path="main.py")`

produce the same signature and are directly comparable.

Sorting argument keys is an important detail because it avoids false differences caused only by dict ordering.

### 7.3 Recording responses

When `record_action("response", text=...)` is called:

- the signature includes the raw response text.

That is simple and effective for exact-repeat detection, though it is intentionally literal rather than semantic.

### 7.4 Exact-repeat detection

If at least `max_exact_repeats` actions are present, the detector checks whether the last 3 signatures are identical.

If so, it returns a message like:

- `Same action repeated 3 tiems`

That typo is in the current implementation and is worth documenting accurately because the runtime currently emits that exact string.

### 7.5 Cycle detection

If the history length is at least `max_cycle_length * 2` (currently 6), the detector checks repeating cycles for lengths 2 through 3.

If the last `cycle_len * 2` entries are two identical adjacent slices, it returns:

- `Detected repeating cycle of length <n>`

This lets the runtime catch patterns such as:

- tool A -> response B -> tool A -> response B

but, as discussed below, only once the gating condition is met.

---

## 8. The active wiring in `Agent._agentic_loop()`

The most important changes are in `core/agent/agent.py`.

This is where the loop detector moves from dormant helper to live runtime control.

### 8.1 Response recording

After the model finishes a streamed text response and `response_text` is non-empty, the agent now does:

- `self.session.loop_detector.record_action("response", text=response_text)`

This means assistant free-text output becomes part of the loop history.

That matters because not all loops are tool loops.
Some are pure repeated-language loops.

### 8.2 Tool-call recording

Before invoking each tool, the agent now does:

- `self.session.loop_detector.record_action("tool_call", tool_name=tool_call.name, args=tool_call.arguments)`

This means the detector sees the agent’s chosen tool behavior, not just the final outcome.

That is the right place to record it.

If the runtime only recorded tool results afterward, it would miss the more important behavioral question:

> did the agent choose the same action again?

### 8.3 Post-tool-cycle loop check

After all tool results for the turn are written back into context, the agent now calls:

- `loop_detection_error = self.session.loop_detector.check_for_loop()`

If that returns a non-`None` description, the runtime does something important:

1. build a loop-break prompt with `create_loop_breaker_prompt(loop_detection_error)`,
2. append that prompt as a new user message to the context manager,
3. `continue` the agentic loop instead of proceeding normally.

This is the heart of the feature.

The system does not just detect the loop.
It responds by injecting an explicit self-correction instruction into the next reasoning round.

---

## 9. Why the loop-break response is implemented as a synthetic user message

This is one of the most interesting design choices in the current implementation.

The runtime could have:

- raised an error,
- terminated the session,
- silently ignored the repeated action,
- or logged a warning out-of-band.

Instead, it does:

- `self.session.context_manager.add_user_message(loop_breaker_prompt)`

That means the model receives the warning inside the same conversational channel it already reasons over.

### 9.1 Why this is clever

This approach preserves the normal agentic loop structure.
The model is not hard-stopped.
It is redirected.

The runtime is essentially saying:

> you appear to be stuck; reflect and choose a different strategy.

That is often a better first intervention than terminating outright.

### 9.2 Relationship to prompt engineering

This also means loop breaking is partly a prompt-level intervention, not only a control-flow intervention.

The repository combines:

- hard runtime observation (`LoopDetector`),
- with soft behavioral steering (`create_loop_breaker_prompt`).

That is a good pattern for agent systems.

---

## 10. The loop-break prompt itself

The injected prompt comes from:

- `core/prompts/system.py -> create_loop_breaker_prompt(loop_description)`

It currently produces a notice like:

```text
[SYSTEM NOTICE: Loop Detected]

The system has detected that you may be stuck in a repetitive pattern:
<loop_description>

To break out of this loop, please:
1. Stop and reflect on what you're trying to accomplish
2. Consider a different approach
3. If the task seems impossible, explain why and ask for clarification
4. If you're encountering repeated errors, try a fundamentally different solution

Do not repeat the same action again.
```

### 10.1 Why this prompt shape matters

This is not merely a warning.
It gives the model a concrete alternative behavior policy:

- reflect,
- vary strategy,
- surface impossibility if needed,
- avoid repeating the same step.

So the prompt is designed to convert detection into changed behavior.

---

## 11. End-to-end active lifecycle after this commit

A typical loop-guarded turn now works like this:

1. the agent starts a normal reasoning turn,
2. the model streams text and/or requests tools,
3. if text is produced, that response is recorded in `LoopDetector`,
4. if tools are requested, each tool call signature is recorded before invocation,
5. tools execute normally and results are written back into context,
6. after the tool phase, the detector checks recent history for exact repeats or short cycles,
7. if no loop is detected, the runtime continues as usual,
8. if a loop is detected, a loop-break user message is added to the context,
9. the `for turn_number in range(max_turns)` loop continues into the next turn,
10. the model gets another attempt, but now with a strong anti-repetition instruction inside context.

This is the core operational picture of the feature.

---

## 12. Example: exact-repeat loop

Imagine the model keeps doing this:

1. call `read_file(path="main.py")`
2. call `read_file(path="main.py")`
3. call `read_file(path="main.py")`

Because the tool name and arguments are identical, the signatures are identical.
After the third repeat, `LoopDetector.check_for_loop()` returns:

- `Same action repeated 3 tiems`

The agent then injects the loop-break prompt and continues the next turn with a warning telling the model not to repeat the same action again.

Conceptually, the next context contains a synthetic user message like:

```text
[SYSTEM NOTICE: Loop Detected]
...
Same action repeated 3 tiems
...
Do not repeat the same action again.
```

That is how the runtime tries to break the loop without killing the session.

---

## 13. Example: short repeating cycle

Consider a pattern like:

1. tool call: `grep(pattern="foo")`
2. response: `Need more info`
3. tool call: `grep(pattern="foo")`
4. response: `Need more info`
5. tool call: `grep(pattern="foo")`
6. response: `Need more info`

Once enough history accumulates, the detector can identify this as:

- `Detected repeating cycle of length 2`

and inject the same loop-break prompt.

This matters because not all loops are “same single action repeated.”
Many are alternating micro-cycles.

---

## 14. Important implementation nuances and edge cases

This section matters because the design is solid, but the current implementation is intentionally simple.

### 14.1 The current diff activates pre-existing groundwork rather than inventing the subsystem from scratch

`LoopDetector` and `create_loop_breaker_prompt(...)` already existed before this uncommitted delta.

What changed since the last commit is that the agent now:

- records actions,
- checks for loops,
- and responds to loop detection inside the live turn loop.

That is the actual scope of the new work.

### 14.2 Cycle detection has a stricter gate than the minimum cycle length might suggest

`check_for_loop()` only enters cycle detection when:

- `len(history) >= max_cycle_length * 2`

With the current constants, that means at least **6 entries** are required before cycle detection is attempted at all.

So a 2-step alternating pattern repeated only twice (4 entries) will **not** yet trigger cycle detection.

This is an important nuance in the current behavior.

### 14.3 Response matching is literal

The response signature includes the full response text.
That means exact-repeat detection for responses is currently literal, not semantic.

Two responses that mean the same thing but differ slightly in wording will not count as identical.

That keeps the implementation simple, but also limits sensitivity.

### 14.4 Tool-call matching is argument-sensitive but shallow

Tool-call signatures are based on:

- tool name,
- sorted argument key/value strings.

That is deterministic and useful, but still shallow.
It does not reason about semantic equivalence beyond identical serialized values.

### 14.5 Loop breaking is advisory, not a hard stop

When a loop is detected, the runtime does **not**:

- abort the session,
- prevent future tool calls absolutely,
- or throw an error.

It injects a corrective prompt and keeps going.

That is a soft intervention strategy.
It may work well in many cases, but it does not mathematically guarantee escape.

### 14.6 Loop detection happens after the tool-result write-back point

The agent checks for loops after tool results are added to context.
That means the loop-breaking prompt is appended only after the tool cycle is already complete.

This is reasonable because the runtime needs to observe the chosen action pattern first, but it also means the repeated tool invocation for that turn has already happened.

### 14.7 Session-local history means resets are naturally scoped

Because the detector is owned by `Session`, loop history lives only for that session.
A fresh session starts with a fresh detector.

That is the right default behavior.

### 14.8 The emitted exact-repeat message currently contains a typo

The string is:

- `Same action repeated 3 tiems`

This is harmless functionally, but worth documenting accurately because it is the literal runtime output today.

### 14.9 Subagents likely inherit the same behavior indirectly

Because `SubagentTool` creates nested `Agent(...)` instances, subagents should also get a session-local loop detector through normal agent/session construction.

That means this feature is likely broader than only the top-level interactive agent, even though that is not the main focus of the current diff.

---

## 15. Why this change matters in the series

This change is important because it advances the project from:

- a runtime that can do many things for many turns,

toward:

- a runtime that can notice when its own behavior is becoming unproductive.

That is a deeper kind of maturity.

Earlier documents mostly expanded capability, safety, extensibility, or observability.
This one expands **self-correction**.

That is a major theme in real agent systems.
The hardest problems are often not:

- missing tools,
- missing APIs,
- or missing prompts,

but rather:

- detecting when the agent is stuck,
- and steering it back toward productive behavior.

`15` is the first explicit step in that direction in this repository.

---

## 16. Delta summary table (`14` -> current uncommitted state)

| Area | `14` baseline | Current uncommitted delta (`15`) |
|---|---|---|
| Loop detector presence | Exists as groundwork from earlier changes | Now owned by `Session` and used in the active agent loop |
| Response observation | Responses only streamed and stored in context | Responses are also recorded as loop-detection signatures |
| Tool-call observation | Tool calls executed normally | Tool calls are also recorded as loop-detection signatures |
| Loop check timing | No active runtime loop check | Post-tool-cycle check inside `Agent._agentic_loop()` |
| Loop response | None | Injects synthetic loop-break user message and continues next turn |
| Break mechanism | N/A | Uses `create_loop_breaker_prompt(...)` for behavioral redirection |
| Intervention style | No dedicated hallucination-loop guard | Soft prompt-based strategy shift rather than hard stop |
| Session runtime control | Hooks, safety, compaction, MCP, etc. | Adds active repetition-awareness to that control surface |

---

## 17. Natural continuation points for a future `16`

Natural next steps after this iteration would be:

- tightening cycle detection so shorter repeating cycles can trigger earlier,
- adding semantic similarity checks for responses rather than exact text matching only,
- surfacing loop-detection events in the UI as explicit observability artifacts,
- integrating loop state with hooks or approval policies,
- counting repeated tool failures separately from repeated successful tool use,
- adding configurable thresholds for exact repeats and cycle length,
- and optionally escalating from soft prompt intervention to hard-stop behavior after repeated loop-break failures.

That would continue the transition from:

- **basic repetition detection and prompt-based steering**

into:

- **fully observable and policy-aware anti-stall behavior for long-running agents**.

---

## 18. Key takeaways

1. The main delta since `docs/14-...` is the activation of loop detection inside the live agentic loop, not the introduction of loop-detection concepts from scratch.
2. `Session` now owns an active `LoopDetector`, keeping repetition history scoped to a single runtime session.
3. `Agent._agentic_loop()` now records both responses and tool calls as abstract action signatures, which gives the runtime a cheap behavioral history.
4. After tool-result write-back, the runtime checks for exact repeats and short cycles and injects a loop-break instruction when repetition is detected.
5. The break mechanism is intentionally soft: it adds a corrective user message through `create_loop_breaker_prompt(...)` rather than terminating the session.
6. The implementation is useful and explainable, but currently simple: response matching is literal, tool-call matching is shallow, cycle detection needs at least six history entries with current settings, and exact-repeat messages include a small typo.
7. This iteration marks the point where the repository begins turning long-running agent execution from merely capable into modestly self-correcting against repetitive hallucination patterns.

