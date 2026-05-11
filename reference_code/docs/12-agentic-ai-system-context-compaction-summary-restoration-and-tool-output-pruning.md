# 12. Agentic AI System Context Compaction, Summary Restoration, and Tool Output Pruning: Token-Budget Survival, Turn-to-Turn Continuity, and Incremental Context Shedding

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

`01` established the client, event, and core runtime abstractions.
`02` introduced the CLI/agent/TUI loop.
`03` introduced context management and prompt construction.
`04` added the first tool-calling runtime.
`05` made runtime behavior configuration-aware.
`06` moved major runtime ownership under `Session`.
`07` expanded the builtin tool surface and interactive observability.
`08` added local search/discovery tools.
`09` extended discovery to the web and specialized more tool results in the terminal.
`10` added local custom tool discovery from `.ai-agent/tools`.
`11` added MCP-backed external tool integration and session-scoped remote tool management.
`12` explains the next runtime boundary problem the project begins to address: **how the agent keeps working when its conversation history becomes too large**, and **how it sheds stale tool-output weight without losing the recent working set**.

In this stage, the code adds:

- a `ChatCompactor` in `core/context/compaction.py`,
- session-owned compaction support through `Session.chat_compactor`,
- token-usage tracking and compression/pruning heuristics in `ContextManager`,
- summary-based context restoration via `ContextManager.replace_with_summary(...)`,
- tool-output pruning via `ContextManager.prune_tool_outputs(...)`,
- turn-loop integration in `Agent._agentic_loop(...)`,
- and an adjacent `LoopDetector` module for future anti-repetition control.

This document serves two purposes:

1. explain what context compaction and pruning are and why they matter in agent systems, and
2. show exactly how they are implemented in this repository, including the current design tradeoffs and edge cases.

---

## 1. High-level change in this iteration

The project shifts from a **session-managed tool-capable agent runtime** to a **session-managed tool-capable runtime that actively manages context pressure over time**.

Previous effective flow (from `11`):

`main.py (CLI)` -> `Agent(config)` -> `Session(config)` -> `ContextManager` keeps accumulating user / assistant / tool messages -> model continues until stop or max turns

Current flow (this `12` step):

`main.py (CLI)` -> `Agent(config)` -> `Session(config + ChatCompactor)` -> each turn observes recent token usage -> if the conversation is too large, compress prior history into a structured continuation summary -> replace full history with a compact restoration sequence -> continue the next turn -> after turns, prune older tool-result payloads that are no longer worth carrying at full size

That is the main conceptual shift.

Earlier docs focused on:

- how the agent gets more capabilities,
- how the runtime owns those capabilities,
- and how the terminal makes tool execution legible.

This iteration focuses on a different problem:

> how does a long-running agent avoid collapsing under its own accumulated conversation state?

That is a real system problem for any agent with:

- long multi-turn work,
- many tool calls,
- verbose tool outputs,
- or large code/document inspections.

So `12` is not about new external capabilities.
It is about keeping existing capabilities usable over longer sessions.

---

## 2. Why context compaction and pruning are needed

Before looking at this repository’s implementation, it helps to separate two related ideas:

- **compaction**, and
- **pruning**.

### 2.1 What compaction means

Compaction means:

- take a large existing conversation history,
- compress it into a structured summary,
- and continue work using that summary instead of replaying the full transcript.

The key goal is **continuity**.

A compacted session should still know:

- the original goal,
- what has already been completed,
- the current codebase state,
- what remains to be done,
- and the immediate next step.

The agent loses raw transcript detail, but keeps enough state to continue intelligently.

### 2.2 What pruning means

Pruning means:

- selectively remove or shrink less valuable historical context,
- without fully resetting the conversation.

In this repo, the pruning target is specifically:

- **older tool result content**.

That is a good target because tool outputs are often the heaviest part of the context:

- large file reads,
- long directory listings,
- command output,
- fetch/search results,
- big diffs.

Many of those outputs are important at the moment they are produced, but much less important several turns later.

### 2.3 Why agent systems need both

Compaction and pruning solve different levels of the same problem.

- **Pruning** is incremental cleanup.
- **Compaction** is full continuity rescue when context pressure is already too high.

A robust long-running agent often needs both:

1. prune bulky old artifacts when possible,
2. compact the whole working history when the session still gets too large.

That is the direction this repository is starting to take in `12`.

---

## 3. Change scope since the previous commit (`1efb962` baseline)

### New modules introduced

- `core/context/compaction.py`
- `core/context/loop_detector.py`

### Existing files updated

- `core/agent/agent.py`
- `core/agent/session.py`
- `core/context/manager.py`

### Center of gravity in this change

The center of gravity is the introduction of a **context-pressure management path** inside the turn loop.

If `11` was about *expanding the set of tools the agent can access*, then `12` is about *making that growing runtime sustainable across longer sessions with more accumulated context*.

---

## 4. Architectural delta: from passive message storage to active context management

### 4.1 Prior state (`11` baseline)

Before this change, `ContextManager` primarily acted as:

- a system prompt holder,
- a structured message accumulator,
- and a way to turn those messages back into model-facing chat input.

That is useful, but passive.

It means the session only grows.
Over time, an agent that reads many files, runs many tools, or iterates deeply will accumulate:

- more messages,
- more tool outputs,
- more tokens.

### 4.2 Current state (`12`)

Now the runtime begins to manage that growth explicitly.

The main additions are:

- token-usage tracking in `ContextManager`,
- a compression trigger via `needs_compression()`,
- a compactor component that summarizes full history,
- a summary-restoration path that rewrites context,
- and a pruning pass that sheds stale tool-output payloads after turns.

This is a meaningful architectural shift.

`ContextManager` is no longer just a transcript container.
It is becoming a **budget-aware working-memory layer**.

---

## 5. New session-owned compaction component: `core/context/compaction.py`

The new `ChatCompactor` is the heart of the compression path.

### 5.1 Why a dedicated compactor class exists

The compression logic could have been inlined directly into `Agent._agentic_loop(...)`.
That would have worked, but it would have mixed concerns.

Instead, the code introduces a dedicated component:

- `ChatCompactor`

This keeps three responsibilities separate:

- `Agent` decides **when** to compact,
- `ChatCompactor` decides **how** to summarize,
- `ContextManager` decides **how** to rewrite stored state after compaction.

That is a good design split.

### 5.2 Session ownership

`Session` now creates:

- `self.chat_compactor = ChatCompactor(self.client)`

This fits the architecture introduced in `06`.

Compaction is session-scoped because it depends on:

- the active model client,
- the current conversation state,
- and the current session lifecycle.

It would not make sense as a global singleton.

---

## 6. How conversation history is prepared for compaction

The first important method in `ChatCompactor` is:

- `_format_history_for_compaction(messages)`

This method takes the current model-facing message list and turns it into a textual artifact that the model can summarize.

### 6.1 System messages are skipped

If a message has:

- `role == "system"`

it is ignored for compaction formatting.

That is a sensible choice because the system prompt already exists separately and does not need to be re-summarized as conversation history.

### 6.2 Tool results are truncated and labeled

Tool messages are rendered like:

- `[Tool Result (<tool_call_id>)]: ...`

and their content is truncated to **2000 characters**.

If the original tool content exceeds that, the compactor adds:

- `... [tool output truncated]`

This is an important nuance.

The compactor is not summarizing raw full tool output directly. It is summarizing a **pre-trimmed representation** of tool output.

That reduces the cost of the compaction request itself.

### 6.3 Assistant messages preserve both prose and tool-call intent

Assistant messages are handled in two parts:

1. assistant natural-language content is included and truncated to **3000 characters**,
2. tool calls are listed separately under `Assistant called tools:`.

Tool call arguments are also truncated, to **500 characters**.

This is a strong design decision because for continuation quality, it is often not enough to know only the assistant’s prose.
It also matters:

- which tools the assistant invoked,
- in what general form,
- and with roughly which parameters.

That helps future continuation avoid repeating already-performed actions.

### 6.4 User messages are truncated separately

All remaining messages fall into the user-like path and are truncated to **1500 characters**.

So the compactor uses role-sensitive truncation sizes:

- user: 1500 chars,
- tool: 2000 chars,
- assistant: 3000 chars,
- tool arguments: 500 chars.

That suggests an intentional priority ordering:

- assistant reasoning/actions are kept slightly richer,
- tool results still matter a lot,
- user prompts matter but are somewhat shorter,
- tool arguments are useful but should stay compact.

### 6.5 Output structure

The formatted history is joined with:

- `\n\n---\n\n`

which produces a readable block-by-block compaction input.

This is not only formatting convenience.
It makes the compaction prompt easier for the model to parse as a sequence of discrete conversational artifacts.

---

## 7. Compression prompt design: the repository asks for a continuation document, not a generic summary

The compactor does not just say “summarize this chat.”
It uses a specialized system prompt from:

- `core/prompts/system.py -> get_compression_prompt()`

### 7.1 Why this prompt matters

The prompt explicitly asks the model to produce a structured continuation payload with sections such as:

- `## ORIGINAL GOAL`
- `## COMPLETED ACTIONS (DO NOT REPEAT THESE)`
- `## CURRENT STATE`
- `## IN-PROGRESS WORK`
- `## REMAINING TASKS`
- `## NEXT STEP`
- `## KEY CONTEXT`

This is one of the most important design choices in the change set.

A generic summary is often too vague to drive continuation.
A continuation-oriented summary is much more useful because it preserves action state, not just narrative state.

### 7.2 Big-picture significance

The repo is not treating compaction as a logging or archival task.
It is treating compaction as a **handoff protocol from one large context state to a smaller resumable state**.

That is exactly the right mental model for agent continuation.

---

## 8. How compression is executed in code

The main execution path is:

- `ChatCompactor.compress(context_manager)`

### 8.1 Input source

The compactor calls:

- `context_manager.get_messages()`

That means it compacts the **model-facing view** of the conversation rather than the raw dataclass objects.

That is a good boundary because it aligns compaction with what the model actually saw.

### 8.2 Small-history guard

If there are fewer than 3 messages, compression returns:

- `(None, None)`

That is a simple early-exit guard to avoid unnecessary compaction attempts when there is barely any conversation state to summarize.

### 8.3 Compression request shape

The compactor sends a two-message request to the main client:

1. a `system` message containing `get_compression_prompt()`,
2. a `user` message containing the formatted prior history.

So compression is itself just another model call, but one with a highly constrained prompt surface.

### 8.4 Non-streaming usage

The compactor calls:

- `self.client.chat_completion(..., stream=False)`

That is a good fit here because compaction is an internal maintenance step, not a user-facing streaming response.

### 8.5 Return value

On success, the method returns:

- `(summary, usage)`

where `usage` is a `TokenUsage` object from the compaction request.

That allows the runtime to account for compaction as a real token-cost event.

---

## 9. Agent loop integration: compression happens at the start of a turn

The next important part is in:

- `core/agent/agent.py -> Agent._agentic_loop()`

### 9.1 Trigger point

At the start of each turn, before gathering tool schemas and before asking the model for the next response, the agent now checks:

- `self.session.context_manager.needs_compression()`

This is a good place for the check.

Compression is not done mid-stream while the model is responding.
It is done before the next round-trip, when the runtime can still safely rewrite context.

### 9.2 What happens when compression is needed

If compression is triggered, the agent:

1. calls `await self.session.chat_compactor.compress(...)`,
2. receives `(summary, usage)`,
3. if a summary exists, replaces the current context with a restoration sequence,
4. records compaction usage through `set_latest_usage(...)` and `add_usage(...)`.

That means compaction is treated as a first-class runtime action, not a hidden side effect.

### 9.3 Why start-of-turn compaction is sensible

This gives a clean sequence:

- previous turn ended,
- usage was recorded,
- next turn starts,
- context pressure is evaluated,
- history is compacted if necessary,
- then normal agent reasoning resumes.

It keeps the turn model understandable.

---

## 10. Usage tracking: compression is triggered by recent reported token usage, not by recomputing the full message store

A major nuance of this implementation is that compression is not triggered by counting all stored message tokens directly every turn.

Instead, the trigger depends on:

- `ContextManager._latest_usage.total_tokens`

### 10.1 New usage fields in `ContextManager`

`ContextManager` now stores:

- `_latest_usage = TokenUsage()`
- `total_usage = TokenUsage()`

And exposes:

- `set_latest_usage(usage)`
- `add_usage(usage)`

### 10.2 Trigger rule

`needs_compression()` uses:

- `context_limit = self.config.model.context_window`
- `current_tokens = self._latest_usage.total_tokens`
- compression threshold = `context_limit * 0.8`

So compression begins once the latest reported total tokens exceed **80% of the configured model context window**.

### 10.3 Why this is an important nuance

This means compression is based on the **model provider’s last reported usage for the full request** rather than on a local recomputation of all saved messages.

That has strengths:

- it reflects what the model call actually consumed,
- it avoids manually reconstructing exact API token accounting.

But it also has implications:

- if usage is missing, stale, or delayed, compression may not trigger at exactly the expected moment,
- the trigger reflects the last request, not an independent real-time estimate of the current in-memory transcript.

This is a perfectly reasonable early design, but it is worth documenting because it affects behavior.

---

## 11. How usage gets recorded in the turn loop

`Agent._agentic_loop()` now captures:

- `usage: TokenUsage | None = None`

and updates it when the client emits:

- `StreamEventType.MESSAGE_COMPLETE`

### 11.1 No-tool path

If the model produces no tool calls, the agent now:

1. stores the latest usage,
2. adds it into cumulative usage,
3. prunes old tool outputs,
4. and returns.

### 11.2 Tool-call path

If the model does produce tool calls, the agent:

1. executes tools,
2. writes tool results back into context,
3. stores the latest usage,
4. adds it into cumulative usage,
5. then prunes old tool outputs.

So regardless of whether the turn ends with tool use or plain response, the context manager now gets a token-usage update and a pruning pass.

This is important because it makes pruning a recurring maintenance step rather than a rare manual cleanup step.

---

## 12. Summary restoration: how the repository resumes after compaction

Compaction alone is not enough.
After the full chat history is summarized, the runtime needs a new compact context that still drives correct continuation.

That is handled by:

- `ContextManager.replace_with_summary(summary)`

### 12.1 Full reset of stored messages

The method begins with:

- `self._messages = []`

So it does not partially keep old history.
It performs a clean rewrite.

### 12.2 Restoration is not a single summary blob

This is one of the best details in the implementation.
The method rebuilds conversation state using **three synthetic messages**:

1. a `user` message with a structured “Context Restoration” block,
2. an `assistant` acknowledgment message,
3. a final `user` continuation instruction.

That is much more deliberate than simply storing the summary as a note.

### 12.3 First restoration message: structured user handoff

The first synthetic user message includes:

- a heading: `# Context Restoration (Previous Session Compacted)`
- a warning that the previous conversation was compacted,
- a critical instruction not to repeat completed actions,
- the generated summary,
- and a resume instruction.

This creates a strong continuation surface for the next model step.

### 12.4 Second restoration message: assistant acknowledgment

The assistant acknowledgment says, in effect:

- I understand the original goal,
- I understand what is already completed,
- I understand current state,
- I will continue only the remaining work.

This is an interesting design choice.

It gives the new compacted context a mini “handshake” that reinforces continuation behavior.
The repo is trying to bias the next turn away from redundant work.

### 12.5 Third restoration message: explicit continue instruction

The final synthetic user message says:

- continue with remaining work only,
- do not repeat completed actions,
- proceed with the next step from the restored context.

So the restoration sequence is effectively:

- handoff,
- acknowledgment,
- restart signal.

That is stronger than a bare summary and helps preserve trajectory.

### 12.6 Example restored message sequence

Conceptually, after compaction the session message list becomes something like:

```text
system: [normal system prompt]
user: # Context Restoration ... [structured summary]
assistant: I've reviewed the context from the previous session...
user: Continue with the REMAINING work only...
```

That is the compact continuation state the next model call receives.

---

## 13. Tool-output pruning: what gets removed and why

The second major part of this change is:

- `ContextManager.prune_tool_outputs()`

This is not full compaction.
It is a cheaper, narrower cleanup pass.

### 13.1 Why prune tool outputs specifically

Tool results are often huge and often temporary in value.
Examples include:

- long file contents,
- shell command output,
- web fetch results,
- search listings,
- directory trees.

These are useful for immediate reasoning, but several turns later the exact raw payload is often less important than the assistant’s follow-up reasoning that consumed it.

So tool outputs are a good first pruning target.

### 13.2 Protection and minimum thresholds

The pruning logic uses two class constants:

- `PRUNE_PROTECT_TOKENS = 40_000`
- `PRUNE_MINIMUM_TOKENS = 20_000`

These thresholds create a two-part heuristic:

1. keep the most recent ~40k tokens of tool output untouched,
2. only bother pruning if at least ~20k tokens beyond that can be cleared.

That prevents overly aggressive micro-pruning and preserves the newest working set.

### 13.3 Minimum conversation maturity rule

Before pruning, the method counts user messages.
If there are fewer than 2 user messages, it returns `0` and does nothing.

This is a nice safety guard.
It avoids pruning too early in a session before there is enough conversational history to justify shedding context.

### 13.4 Reverse scan behavior

The method scans messages in reverse order.
For each `tool` message with a `tool_call_id`:

- it computes or reuses the token count,
- adds that to the running recent-tool-output budget,
- and once the running total exceeds `PRUNE_PROTECT_TOKENS`, marks older tool messages for pruning.

This means the newest tool outputs are preserved and older ones become pruning candidates.

### 13.5 One-way incremental boundary via `pruned_at`

`MessageItem` now has:

- `pruned_at: datetime | None`

During reverse scanning, if the method hits a tool message that is already pruned, it stops scanning entirely.

That is a subtle but important behavior.

It means pruning is incremental and frontier-based.
The system does not keep re-scanning indefinitely through the entire old transcript on every turn.
Instead, it treats the first already-pruned tool message as a boundary between:

- the older already-managed region,
- and the newer unpruned region.

### 13.6 Actual pruning action

For each selected tool message, the method replaces content with:

- `[Old tool result content cleared]`

Then it:

- recomputes token count for that placeholder,
- stamps `pruned_at = datetime.now()`.

This is a good approach because the message object itself still exists.
So the transcript still records that a tool result happened, but not its full payload.

### 13.7 Example pruning behavior

Suppose the message history contains many older tool outputs totaling 90k tokens.
The newest 40k tokens of tool output stay intact.
Older tool outputs beyond that become candidates.
If those candidates total at least 20k tokens, they are replaced with the placeholder.

Conceptually:

```text
old tool result A -> [Old tool result content cleared]
old tool result B -> [Old tool result content cleared]
recent tool result C -> kept intact
recent tool result D -> kept intact
```

That preserves recency while reducing transcript weight.

---

## 14. How compaction and pruning work together in the runtime

The runtime now has two layers of pressure management.

### 14.1 Step 1: usage-driven full compaction when nearing context limits

At the start of a turn:

- if latest usage > 80% of context window,
- summarize full history,
- replace conversation with compact restoration sequence.

### 14.2 Step 2: routine pruning after turns

At the end of the turn lifecycle:

- prune older tool outputs if enough stale tool payload exists.

### 14.3 Why that ordering makes sense

Pruning happens regularly and cheaply.
Compaction happens only when the session is already near the model’s context ceiling.

So the architecture is:

- **prune often, compact when necessary**.

That is the right general direction for long-lived agent systems.

---

## 15. Adjacent addition: `LoopDetector` exists but is not yet integrated into the active runtime

This change set also introduces:

- `core/context/loop_detector.py`

Even though the user request is primarily about compaction and pruning, this file is worth mentioning because it sits in the same “long-running agent resilience” category.

### 15.1 What `LoopDetector` does

It records action signatures and can detect:

- exact repeats,
- short repeating cycles.

### 15.2 Why it is related conceptually

Compaction/pruning address **context growth**.
Loop detection addresses **behavioral repetition**.

Both are runtime sustainability controls.

### 15.3 Current state in this repo

At the moment, `LoopDetector` is not yet wired into `Agent._agentic_loop()` or `Session`.
So it is better understood as adjacent groundwork for a later continuation rather than part of the active compaction/pruning path.

---

## 16. Important implementation nuances and edge cases

This section matters because the design is strong, but the current implementation has several details worth calling out.

### 16.1 Compression is triggered from latest provider-reported usage, not recomputed local transcript size

This means:

- compaction depends on the last model call’s `usage.total_tokens`,
- not on summing all stored `MessageItem.token_count` values.

That keeps the trigger simple and provider-aligned, but it can diverge slightly from the local transcript’s intuitive size.

### 16.2 Compaction uses the same main client/model

`ChatCompactor` is created with the same `LLMClient` used for normal chat.
So compaction is not currently offloaded to:

- a cheaper model,
- a dedicated summarization model,
- or a different provider.

That simplifies architecture but means compaction consumes the same model budget path as normal reasoning.

### 16.3 Compaction is continuation-oriented, not lossless

Because `_format_history_for_compaction(...)` truncates:

- tool outputs,
- assistant responses,
- user messages,
- tool arguments,

compaction is inherently lossy.

That is expected, but important.
The goal is resumability, not perfect archival reconstruction.

### 16.4 Pruning only targets tool messages

The current pruning logic does **not** prune:

- user messages,
- assistant prose,
- assistant tool-call metadata.

So if assistant responses themselves become extremely large, pruning alone will not help with that portion of the transcript.

### 16.5 Pruning is recency-preserving but coarse

The logic preserves the most recent 40k tokens of tool output and replaces older candidates with a fixed placeholder.
That is simple and effective, but it is coarse-grained.
There is no ranking by semantic importance, only by recency.

### 16.6 Already-pruned boundary stops reverse scanning

Because the reverse scan breaks when it encounters an already-pruned tool message, pruning behaves like a rolling frontier rather than a full historical sweep.

That improves efficiency, but it also means the pruning algorithm intentionally does not keep revisiting very old already-managed regions.

### 16.7 Compression failure is fail-soft

If compaction fails for any reason, `ChatCompactor.compress(...)` returns `(None, None)`.

That means the turn loop simply proceeds without rewriting context.
This is resilient, but it also means context pressure can remain unresolved if compaction repeatedly fails.

### 16.8 `replace_with_summary(...)` fully discards prior raw messages

Once summary restoration succeeds, the prior `_messages` list is replaced entirely.
That is powerful, but it also means there is no retained raw transcript inside `ContextManager` after compaction.

So this is a true working-context rewrite, not a layered cached archive.

### 16.9 Current usage totals are tracked but not yet surfaced in the UI

`ContextManager.total_usage` is maintained, but it is not yet exposed through the terminal runtime.

So token accounting is beginning to exist as runtime state, but not yet as operator-facing observability.

### 16.10 `LoopDetector` is groundwork, not active control

Although introduced in the same delta, loop detection is not yet part of the active agent turn logic.
So the runtime currently addresses context size more directly than repetitive execution behavior.

---

## 17. End-to-end lifecycle after this commit

A typical long-running session can now behave like this:

1. user starts a task,
2. the agent accumulates user messages, assistant replies, and tool results,
3. each model completion reports token usage,
4. the context manager stores the latest usage and cumulative usage,
5. after turns, old bulky tool outputs may be pruned if enough stale tool weight exists,
6. on a later turn, if latest usage exceeds 80% of the model context window, compression is triggered,
7. the compactor formats the prior history into a continuation-oriented source document,
8. the model produces a structured continuation summary,
9. the context manager replaces the full prior message history with a restoration triplet,
10. the next turn proceeds from the compact restored state rather than the raw full transcript.

This is the new big picture.

---

## 18. Example: how a long coding session would be compacted

Imagine a user asks the agent to:

- inspect many files,
- run several shell commands,
- write code,
- fix tests,
- and continue iterating for many turns.

Over time the transcript may include:

- many `read_file` tool outputs,
- shell output from tests,
- diffs from file edits,
- assistant planning text.

Once usage crosses the 80% threshold, the compactor asks the model to emit something like:

```markdown
## ORIGINAL GOAL
Implement feature X and make tests pass.

## COMPLETED ACTIONS (DO NOT REPEAT THESE)
- Updated `core/foo.py` to add parser support.
- Added tests in `tests/test_foo.py`.
- Ran `pytest tests/test_foo.py` and fixed the first failure.

## CURRENT STATE
The parser change is present. One edge-case test still fails in `tests/test_foo.py`.

## IN-PROGRESS WORK
The agent was investigating a mismatch in error handling.

## REMAINING TASKS
- Fix the remaining edge-case failure.
- Re-run the targeted tests.

## NEXT STEP
Open `core/foo.py` and inspect the branch handling empty input.

## KEY CONTEXT
Do not revert earlier parser changes. The user wants the minimal fix.
```

Then `replace_with_summary(...)` turns that into the new compact context scaffold and the agent continues from there.

That is the intended continuation experience.

---

## 19. Why this change matters in the series

This change is important because the earlier documents mostly expanded what the runtime could do.
This one begins to address whether the runtime can **keep doing it for long enough**.

That is a different maturity step.

The project is moving from:

- a tool-rich, increasingly extensible session runtime,

toward:

- a tool-rich, extensible runtime with the beginnings of working-memory management.

That is a major shift because long-running agent usefulness depends on more than tool availability.
It also depends on:

- context survivability,
- non-repetition,
- and token-budget discipline.

`12` is the point where those concerns start becoming first-class runtime behavior.

---

## 20. Delta summary table (`11` -> current uncommitted state)

| Area | `11` baseline | Current uncommitted delta (`12`) |
|---|---|---|
| Context behavior | Passive accumulation of messages | Adds token-aware compaction trigger and tool-output pruning |
| New session component | MCP manager for external tools | Adds `ChatCompactor` for context survival |
| New context module | No compaction module | Adds `core/context/compaction.py` |
| New resilience module | No loop-resilience helper | Adds `core/context/loop_detector.py` groundwork |
| Usage tracking | Tool/runtime flow only | Stores latest usage and cumulative usage in `ContextManager` |
| Compression trigger | None | Trigger at 80% of model context window using latest reported total tokens |
| Compression output | N/A | Structured continuation summary with explicit do-not-repeat sections |
| Post-compaction state | Full transcript retained | Transcript replaced with restoration triplet (user / assistant / user) |
| Pruning target | None | Older tool-result messages beyond recency budget |
| Pruning heuristics | N/A | Protect recent 40k tool tokens; only prune if at least 20k older tool tokens can be cleared |
| Turn-loop maintenance | No context maintenance step | Start-of-turn compaction check + end-of-turn pruning pass |

---

## 21. Natural continuation points for a future `13`

Natural next steps after this iteration would be:

- surfacing token usage and compaction/pruning statistics in the CLI or TUI,
- making compaction use a dedicated cheaper summarization model,
- improving pruning beyond recency-only heuristics,
- pruning or compressing large assistant messages when necessary,
- wiring `LoopDetector` into the active agent loop with loop-break prompts,
- preserving a hidden archival transcript while still rewriting the working context,
- and making compaction trigger from both provider-reported usage and local transcript estimates.

That would continue the transition from:

- **basic working-context survival mechanisms**

into:

- **fully observable, policy-aware, long-horizon agent memory management**.

---

## 22. Key takeaways

1. The main delta since `docs/11-...` is the addition of context-pressure management through full-history compaction and older tool-output pruning.
2. `ChatCompactor` turns the existing conversation into a continuation-oriented summary rather than a generic recap, which is the right design for resumable agent work.
3. `ContextManager` is no longer only a message accumulator; it now tracks usage, decides when compression is needed, rewrites state after compaction, and prunes stale tool payloads.
4. `Agent._agentic_loop()` now actively participates in context maintenance by checking compression at turn start and pruning after turns.
5. Summary restoration in this repo is intentionally structured as a synthetic handoff/acknowledgment/continue sequence, not just a raw summary blob.
6. Tool-output pruning is conservative and recency-based: keep the newest tool-output working set, replace sufficiently old bulky results with placeholders, and leave assistant/user messages intact.
7. The implementation is a strong first step, but it is still early: trigger logic depends on latest reported usage, compaction is lossy by design, and loop detection is present but not yet wired into active control.

