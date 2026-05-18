# Middleware In The Agentic Loop

This document explains how middleware works in Mistral Vibe's agentic AI loop.
It covers the middleware contract, ordering, limit checks, context warnings,
auto-compaction, read-only agent reminders, and how each result changes the
active conversation flow.

The core implementation lives in:

- `vibe/core/middleware.py`
- `vibe/core/agent_loop.py`
- `vibe/core/types.py`
- `vibe/core/config/_settings.py`
- `vibe/core/prompts/compact.md`

## What Middleware Is

Middleware is a pre-turn control layer around the LLM call.

It runs before every model turn inside `AgentLoop._conversation_loop()`. It can:

- allow the next LLM call
- stop the agent loop
- inject a runtime message into model context
- trigger compaction before the next LLM call

Middleware does not execute tools and does not approve individual tool calls.
Tool approval is a separate gate in `AgentLoop._should_execute_tool()`.

```text
user message
  -> middleware checks
  -> maybe stop, compact, or inject message
  -> LLM call
  -> parse tool calls
  -> tool permission checks
  -> execute tools
  -> append tool results
  -> middleware checks again before next LLM turn
```

## Middleware Contract

Middleware implements the `ConversationMiddleware` protocol:

```python
class ConversationMiddleware(Protocol):
    async def before_turn(self, context: ConversationContext) -> MiddlewareResult: ...
    def reset(self, reset_reason: ResetReason = ResetReason.STOP) -> None: ...
```

The input is `ConversationContext`:

```python
@dataclass
class ConversationContext:
    messages: MessageList
    stats: AgentStats
    config: VibeConfig
```

Middleware can inspect:

- current messages
- token and cost stats
- active config, including active model settings

It returns `MiddlewareResult`:

```python
@dataclass
class MiddlewareResult:
    action: MiddlewareAction = MiddlewareAction.CONTINUE
    message: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

## Middleware Actions

There are four actions:

| Action | Meaning | AgentLoop effect |
| --- | --- | --- |
| `CONTINUE` | No intervention | Continue to the LLM call |
| `STOP` | End this agent turn | Emit an assistant stop event and return |
| `COMPACT` | Summarize and reset context | Run `compact()` before the LLM call |
| `INJECT_MESSAGE` | Add runtime instruction | Append an injected user message |

`INJECT_MESSAGE` is used for context warnings and mode reminders. The injected
message is added as:

```python
LLMMessage(role=Role.user, content=result.message, injected=True)
```

Injected messages are part of active model context, but they are marked
`injected=True` so other logic can distinguish them from real user prompts.

## Pipeline Ordering

`AgentLoop._setup_middleware()` builds the pipeline in this order:

1. `TurnLimitMiddleware`, only when `max_turns` is set.
2. `PriceLimitMiddleware`, only when `max_price` is set.
3. `AutoCompactMiddleware`, always.
4. `ContextWarningMiddleware`, only when `config.context_warnings` is enabled.
5. `ReadOnlyAgentMiddleware` for `plan`.
6. `ReadOnlyAgentMiddleware` for `chat`.

Ordering matters because the pipeline short-circuits on `STOP` and `COMPACT`.
Injected messages are collected and combined only if no earlier middleware
returned `STOP` or `COMPACT`.

```text
for each middleware:
  result = before_turn(context)
  if result is INJECT_MESSAGE:
    remember it
  if result is STOP or COMPACT:
    return immediately

if injected messages exist:
  return one combined INJECT_MESSAGE result

return CONTINUE
```

This means:

- hard limits win before warnings
- compaction wins before warnings and read-only reminders
- multiple reminders can be combined into one injected message
- stop and compact are exclusive for that pre-turn check

## Where Middleware Runs

Inside `_conversation_loop()`, Vibe appends the user message, then enters a loop.
At the top of every iteration it calls:

```python
result = await self.middleware_pipeline.run_before_turn(self._get_context())
```

Then it handles the result:

```text
STOP
  -> emit AssistantEvent(stopped_by_middleware=True)
  -> return from conversation loop

INJECT_MESSAGE
  -> append injected user message
  -> continue to LLM call

COMPACT
  -> emit CompactStartEvent
  -> run compact()
  -> emit CompactEndEvent
  -> continue to LLM call

CONTINUE
  -> continue to LLM call
```

After the LLM call and any tool calls, the loop checks the last message. If the
last message is a tool message, the agent must continue so the LLM can observe
tool results. Middleware runs again before that next LLM turn.

## AgentStats Used By Middleware

Middleware limit checks use `AgentStats`.

Important fields:

- `steps`
- `session_prompt_tokens`
- `session_completion_tokens`
- `context_tokens`
- `input_price_per_million`
- `output_price_per_million`
- computed `session_cost`

`AgentLoop._update_stats()` updates token stats after each LLM response:

```python
self.stats.session_prompt_tokens += usage.prompt_tokens
self.stats.session_completion_tokens += usage.completion_tokens
self.stats.context_tokens = usage.prompt_tokens + usage.completion_tokens
```

`context_tokens` is the latest request/response context token count from the
backend, not a hand-counted sum of stored messages. After compaction, Vibe calls
`backend.count_tokens()` on the compacted messages and tools to reset
`stats.context_tokens` to the actual compacted context size.

## TurnLimitMiddleware

`TurnLimitMiddleware` is enabled when `AgentLoop` is created with `max_turns`.
This mainly applies to programmatic mode.

Check:

```python
if context.stats.steps - 1 >= self.max_turns:
    return STOP
```

Effect:

- The loop emits an `AssistantEvent` with a stop tag.
- The event has `stopped_by_middleware=True`.
- Programmatic mode converts this into a `ConversationLimitException`.

The `steps - 1` offset exists because the loop increments `stats.steps` for the
incoming user message before middleware runs, then increments again before each
LLM turn. In practice, the middleware prevents the session from exceeding the
configured assistant-turn budget.

Turn limit does not summarize or mutate context. It stops before another LLM
call is made.

## PriceLimitMiddleware

`PriceLimitMiddleware` is enabled when `AgentLoop` is created with `max_price`.
This also mainly applies to programmatic mode.

Check:

```python
if context.stats.session_cost > self.max_price:
    return STOP
```

`session_cost` is computed from cumulative session prompt/completion tokens and
the active model's configured prices:

```text
input_cost = session_prompt_tokens / 1_000_000 * input_price_per_million
output_cost = session_completion_tokens / 1_000_000 * output_price_per_million
```

The code comment notes that this is a rough estimate. If the model changes
mid-session, the current pricing is applied to accumulated tokens.

Price limit does not compact or trim context. It stops before another LLM call.

## AutoCompactMiddleware

`AutoCompactMiddleware` is always installed.

Check:

```python
threshold = context.config.get_active_model().auto_compact_threshold

if threshold > 0 and context.stats.context_tokens >= threshold:
    return COMPACT with old_tokens and threshold metadata
```

Important details:

- `threshold <= 0` disables auto-compaction.
- The threshold comes from the active model config.
- Global `auto_compact_threshold` is applied to models that do not explicitly
  set their own threshold.
- The default global threshold is `200_000`.
- Some agent profiles can override model thresholds. The `lean` profile uses a
  lower custom threshold.

Auto-compaction runs before the next LLM turn. It is reactive: the stats are
updated after an LLM response, then the next pre-turn middleware pass sees the
updated `context_tokens`.

## Auto-Compaction Flow

When middleware returns `COMPACT`, `AgentLoop._handle_middleware_result()` does:

```text
old_tokens = current stats.context_tokens
threshold = active model threshold
old_session_id = current session id

yield CompactStartEvent
summary = await compact()
send auto-compact telemetry
yield CompactEndEvent
```

`compact()` then:

1. Cleans missing tool responses in message history.
2. Saves the current full session to session logs.
3. Reads the `UtilityPrompt.COMPACT` prompt.
4. Appends any extra manual compaction instructions.
5. Increments `stats.steps`.
6. Silently appends the compaction prompt as a user message.
7. Calls `_chat()` with `config.get_compaction_model()`.
8. Extracts the returned summary text.
9. Resets active messages to:

```text
system message
summary message
```

10. Resets the session id, linking the old session as parent.
11. Calls `backend.count_tokens()` on compacted messages and available tools.
12. Sets `stats.context_tokens` to that counted value.
13. Saves the compacted session.
14. Resets middleware state with `ResetReason.COMPACT`.

Compaction is summarization, not lossless pruning. The detailed old messages are
saved in session logs, while active context becomes a model-generated summary.

## Compact Events

Compaction emits two core events:

```python
CompactStartEvent(
    tool_call_id=...,
    current_context_tokens=old_tokens,
    threshold=threshold,
)
```

```python
CompactEndEvent(
    tool_call_id=...,
    old_context_tokens=old_tokens,
    new_context_tokens=new_tokens,
    summary_length=len(summary),
    old_session_id=old_session_id,
    new_session_id=self.session_id,
)
```

The comments in `types.py` note that compact events currently use a
`tool_call_id` workaround so clients can display them like tool activity until
the protocol has a dedicated compact event shape.

## ContextWarningMiddleware

`ContextWarningMiddleware` is installed only when `config.context_warnings` is
enabled.

It warns once when the latest context token count reaches a configured fraction
of the compaction threshold:

```python
if context.stats.context_tokens >= max_context * threshold_percent:
    return INJECT_MESSAGE
```

Vibe installs it with `threshold_percent=0.5`, so the warning triggers at 50% of
the active model's `auto_compact_threshold`.

The injected warning looks like:

```text
<vibe_warning>You have used 50% of your total context (.../... tokens)</vibe_warning>
```

Behavior:

- It injects at most once because `has_warned=True` after the first warning.
- It does nothing when the threshold is `<= 0`.
- It resets `has_warned=False` when the middleware pipeline is reset.
- It does not compact or stop the session.

Because `AutoCompactMiddleware` appears before `ContextWarningMiddleware`, a
session that already crossed the full compaction threshold compacts instead of
injecting a warning.

## ReadOnlyAgentMiddleware

`ReadOnlyAgentMiddleware` enforces runtime reminders for special agent modes.
It is used twice:

- plan mode
- chat mode

It tracks whether the target mode was active on the previous middleware pass.

Cases:

```text
was inactive, now active
  -> inject reminder

was active, still active
  -> continue

was active, now inactive
  -> inject exit message

was inactive, still inactive
  -> continue
```

This is not the only safety mechanism for plan or chat mode. Agent profiles also
apply config overlays that restrict tools. Middleware provides strong in-context
instructions so the model is reminded of the current mode.

## Plan Mode Reminder

Plan mode reminder is generated by `make_plan_agent_reminder()`.

It tells the model:

- plan mode is active
- do not make edits except to the plan file or scratchpad
- do not run non-readonly tools
- research using read-only tools
- ask the user questions when unsure, if `ask_user_question` is available
- write the plan to the generated plan file
- call `exit_plan_mode` when ready, if available

The plan file path is lazily created by `PlanSession` under the Vibe plans
directory. The plan agent profile config allowlists writes only to that plan
file pattern and denies general writes through the edit tools.

## Chat Mode Reminder

Chat mode reminder tells the model:

- chat mode is active
- answer questions and explain code
- use read-only tools only when needed
- do not make edits
- the response itself is the deliverable

The chat profile also configures:

```python
overrides={"bypass_tool_permissions": True, "enabled_tools": CHAT_AGENT_TOOLS}
```

where chat tools are:

```python
["grep", "read_file", "ask_user_question", "task"]
```

So chat mode combines tool-surface restriction with injected behavioral
instructions.

## Middleware Reset

`MiddlewarePipeline.reset()` calls `reset()` on every middleware.

Reset happens in important lifecycle operations:

- `clear_history()`
- `compact()`, with `ResetReason.COMPACT`
- `reload_with_initial_messages()` rebuilds the middleware pipeline when config
  or agent settings change

For stateless limit middleware, reset is a no-op. For warning and read-only
middleware, reset clears internal state:

- `ContextWarningMiddleware.has_warned = False`
- `ReadOnlyAgentMiddleware._was_active = False`

This means after compaction or history reset, context warnings and mode
reminders can fire again when appropriate.

## How Middleware Updates Context

Middleware updates context in only two direct ways:

1. `INJECT_MESSAGE`: appends an injected user message to `MessageList`.
2. `COMPACT`: replaces the active message list with `[system, summary]`.

`STOP` does not update `MessageList` directly. It yields an assistant event to
the UI or caller and ends the turn.

`CONTINUE` does nothing.

Middleware never edits old messages in place. Even warnings and mode reminders
are appended as new injected messages. Compaction is the only middleware-driven
operation that resets the message list.

## Full Pre-Turn Flow

```text
AgentLoop.act(user_msg)
  -> clean message history
  -> create rewind checkpoint
  -> append user message
  -> yield UserMessageEvent

while loop is active:
  -> build ConversationContext(messages, stats, config)
  -> run middleware pipeline

  if STOP:
    -> yield AssistantEvent(stopped_by_middleware=True)
    -> end

  if INJECT_MESSAGE:
    -> append injected user message

  if COMPACT:
    -> yield CompactStartEvent
    -> save full session
    -> ask compaction model for summary
    -> reset messages to system + summary
    -> reset session id with parent link
    -> recount tokens
    -> save compacted session
    -> reset middleware
    -> yield CompactEndEvent

  -> increment step count
  -> call LLM
  -> update stats from LLM usage
  -> append assistant message
  -> parse tool calls
  -> execute approved tools
  -> append tool result messages
  -> save session
  -> continue if last message is role="tool"
```

## Relationship To Tool Approval

Middleware and tool approval solve different problems.

Middleware:

- runs before an LLM turn
- observes conversation-level stats and active mode
- can stop, compact, or inject guidance

Tool approval:

- runs after the model has requested a specific tool call
- evaluates tool config, per-tool permission logic, allowlists, denylists, and
  required permissions
- can execute or skip that one tool call

For example:

```text
AutoCompactMiddleware
  -> "context too large, summarize before another LLM turn"

Bash.resolve_permission
  -> "this specific shell command requires approval"
```

They are complementary guardrails in different phases of the loop.

## Programmatic Mode Limits

Programmatic mode passes `max_turns` and `max_price` into `AgentLoop`, so
middleware can enforce them. If the loop emits a stopped-by-middleware assistant
event, `run_programmatic()` raises `ConversationLimitException`.

This makes headless automation safer:

- `--max-turns N` caps agent looping.
- `--max-price DOLLARS` caps estimated cost.
- auto-compaction can still happen if context grows too large.

## Edge Cases

### Warning Before Compact

If `context_warnings` is enabled and context tokens cross 50% of the threshold,
Vibe injects a warning. If the next LLM turn pushes tokens past 100%, the next
pre-turn middleware pass triggers compaction.

### Immediate Compact

If `stats.context_tokens` is already above threshold before the first LLM turn
of a new user request, compaction happens before answering that user request.
This is covered in tests by setting `context_tokens` above a low threshold and
checking that compaction metadata is sent before user-turn metadata.

### Disabled Auto-Compact

If the active model threshold is `0` or negative, `AutoCompactMiddleware` and
`ContextWarningMiddleware` do nothing for context size.

### Compaction Failure

If compaction fails, the exception propagates. The loop still attempts to save
the session in the `compact()` exception path. Telemetry records compact status
as failure or cancelled.

### Read-Only Reminder After Switch

When leaving plan or chat mode, middleware injects an exit message exactly once.
If the agent later re-enters that mode, the entry reminder is injected again.

## Conceptual Summary

Middleware is Vibe's conversation-level control plane.

It answers questions like:

- Is the agent allowed to take another turn?
- Has the estimated cost exceeded the user-defined budget?
- Is the context window getting too large?
- Should the session be compacted before continuing?
- Does the model need a mode reminder?
- Did the agent just leave a restricted mode?

It does not answer:

- Is this exact file write allowed?
- Is this exact shell command safe?
- Should this MCP tool call execute?

Those are tool permission questions.

The clean separation is:

```text
middleware = conversation and mode guardrails
tool permissions = per-action guardrails
compaction = lossy context reset with durable logs preserved
injected messages = appended runtime instructions
```

## Files To Read

- `vibe/core/middleware.py`: middleware types and builtin checks.
- `vibe/core/agent_loop.py`: middleware setup, pre-turn execution, compaction
  handling, and stats updates.
- `vibe/core/types.py`: `AgentStats`, compact events, assistant/user/tool
  events.
- `vibe/core/config/_settings.py`: model thresholds, global threshold, and
  compaction model config.
- `vibe/core/prompts/compact.md`: summary instructions used for compaction.
- `tests/test_middleware.py`: read-only mode middleware behavior.
- `tests/test_agent_backend.py`: auto-compaction behavior.
