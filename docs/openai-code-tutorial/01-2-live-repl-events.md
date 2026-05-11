# 01-2 — Live REPL Events: Streaming Events and Multi-Turn History

## Prerequisites

Complete [01-1-streaming.md](01-1-streaming.md) first.

---

## What this chapter covers

Two closely related improvements that make multi-turn conversations work
reliably with Mistral (and any OpenAI-compatible provider):

1. **Multi-turn history correctness** — fixing HTTP 400
   `"Assistant message must have either content or tool_calls, but not none."`
2. **Live event streaming** — printing tool calls and responses as they happen
   rather than buffering everything until the full turn completes.

---

## Part 1 — HTTP 400: The Missing Tool Calls in History

### Root cause

When an assistant turn uses tools, the wire response looks like:

```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [{ "id": "call-1", "type": "function", "function": {...} }]
}
```

If you store that message in history as only `Message(role="assistant",
content="")`, the next request replays:

```json
{ "role": "assistant", "content": "" }
```

That's invalid — Mistral requires the assistant message to carry its original
`tool_calls` array so it can correlate tool results. The result is HTTP 400.

The same issue affects tool-result messages: Mistral requires `tool_call_id`
on every tool message, not just a `name` field.

### Fix: extend `Message` with `tool_calls` and `tool_call_id`

```python
# nexus/models.py
@dataclass(slots=True, frozen=True)
class Message:
    role: Role
    content: str
    name: str | None = None
    # Populated on assistant messages that called tools.
    tool_calls: tuple[ToolCall, ...] = ()
    # Populated on tool-result messages; must match the corresponding call_id.
    tool_call_id: str | None = None
```

Both fields default to empty / `None` so existing code that creates plain
`Message(role="user", content="...")` objects is unaffected.

### Fix: `from_wire_response` — put tool_calls on the Message

```python
# nexus/integrations/openai_compatible.py
def from_wire_response(self, payload):
    ...
    tool_calls = tuple(
        ToolCall(call_id=tc["id"], tool_name=tc["function"]["name"],
                 arguments=json.loads(tc["function"]["arguments"]))
        for tc in message_payload.get("tool_calls") or ()
    )
    message = Message(
        role="assistant",
        content=message_payload.get("content") or "",
        tool_calls=tool_calls,   # ← carries through to history
    )
```

### Fix: `to_wire_request` — serialize tool_calls and tool_call_id

```python
def to_wire_request(self, request):
    messages = [{"role": "system", "content": request.system_prompt}]
    for message in request.messages:
        if message.role == "assistant" and message.tool_calls:
            # Re-emit the original tool_calls array; content may be null.
            item = {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": tc.call_id,
                        "type": "function",
                        "function": {
                            "name": tc.tool_name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in message.tool_calls
                ],
            }
        elif message.role == "tool":
            item = {"role": "tool", "content": message.content}
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
        else:
            item = {"role": message.role, "content": message.content}
            if message.name:
                item["name"] = message.name
        messages.append(item)
```

### Fix: agent and history application

Every place that creates a tool-result `Message` must set `tool_call_id`:

```python
# nexus/runtime/agent.py — inside the tool execution loop
result = await tool.execute(tool_call.call_id, tool_call.arguments, context)
history.append(Message(
    role="tool",
    content=result.output,
    name=result.tool_name,
    tool_call_id=result.call_id,   # ← required by Mistral / OpenAI
))
```

```python
# nexus/runtime/repl.py — apply_events_to_history
elif event.kind == "tool_result":
    state.history.append(Message(
        role="tool",
        content=event.payload.output,
        name=event.payload.tool_name,
        tool_call_id=event.payload.call_id,
    ))
```

---

## Part 2 — Live Event Streaming in the REPL

### Problem: buffered rendering

The original REPL flow was:

```
user input
  └─► collect_turn_events()   # eagerly consumes the full async generator
        └─► agent.run()         # yields: thinking_started → tool_call_requested
              ...                #          → tool_result → model_response
  └─► render_events()         # prints everything at once, after the turn finishes
  └─► apply_events_to_history()
```

The user sees nothing until every tool call completes and the model has
finished generating. For long-running tasks this is frustrating.

### Solution: stream events to the console inline

```
user input
  └─► _stream_turn_live()
        async for event in agent.run():
          _render_event(console, event, ...)   # prints immediately
          batch.append(event)
        # handle confirmation if needed, loop or return
  └─► apply_events_to_history(events)
```

Each event type is rendered as soon as it arrives:

| Event kind             | Output (when `show_tool_calls = true`)       |
|------------------------|----------------------------------------------|
| `thinking_started`     | `⋯ thinking…` (dim)                          |
| `tool_call_requested`  | `⚙ tool_name {arguments}` (cyan dim)         |
| `tool_result`          | `  ↳ <first 300 chars of output>` (dim)      |
| `tool_denied`          | `✗ denied: <reason>` (red bold)              |
| `model_response`       | Full markdown content (with word-by-word     |
|                        | typewriter effect when `stream_output=true`) |

### `_render_event` helper

Extracted from the old `render_events` loop so it can be called per-event:

```python
def _render_event(console, event, *, stream_output, show_tool_calls):
    if event.kind == "thinking_started" and show_tool_calls:
        console.print("[dim]⋯ thinking…[/dim]")
    elif event.kind == "model_response":
        content = event.payload.message.content
        if not content:
            return
        console.print()
        if stream_output:
            _stream_markdown(console, content)
        else:
            console.print(Markdown(content))
        console.print()
    elif event.kind == "tool_call_requested" and show_tool_calls:
        console.print(
            f"[dim cyan]⚙ {event.payload.tool_name}[/dim cyan] "
            f"[dim]{event.payload.arguments}[/dim]"
        )
    elif event.kind == "tool_result" and show_tool_calls:
        preview = str(event.payload.output)[:300]
        console.print(f"[dim]  ↳ {preview}[/dim]")
    elif event.kind == "tool_denied":
        console.print(f"[red bold]✗ denied:[/red bold] {event.payload.reason}")
```

`render_events` (used by headless mode) becomes a thin wrapper:

```python
def render_events(console, events, *, stream_output, show_tool_calls):
    for event in events:
        _render_event(console, event, stream_output=stream_output,
                      show_tool_calls=show_tool_calls)
```

### Confirmation flow in the streaming path

`_stream_turn_live` consumes one batch of `agent.run()` events at a time,
renders them, then checks whether a `confirmation_requested` event appeared
at the end of the batch:

- **No confirmation** → record telemetry and return all collected events.
- **`APPROVAL` + `auto_confirm=True`** → add the tool to `approved_tools`, loop.
- **`APPROVAL` + callback** → call the callback; if approved, loop; else return.
- **`CLARIFICATION`** → append the user's clarification to history, loop.

This preserves the existing confirmation semantics while streaming every other
event immediately.

### Headless mode is unchanged

`collect_turn_events` and `render_events` are still exported and used by
`nexus/cli/headless.py`. Only the interactive REPL uses `_stream_turn_live`.

---

## Sequence diagram — corrected multi-turn tool call

```
User            REPL              Agent            Provider (Mistral)
 |               |                 |                      |
 |-- "what time"|                 |                      |
 |               |-- agent.run() --|                      |
 |               |                 |--- complete() ------>|
 |               |                 |<-- {"tool_calls":[…]}|
 |               |<- tool_call_    |                      |
 |               |   requested     |                      |
 |               |<- tool_result   |                      |
 |               |                 |-- (tool executed)    |
 |               |                 |                      |
 |               |                 |--- complete() ------>|
 |               |                 |  history includes:   |
 |               |                 |  • user message      |
 |               |                 |  • assistant msg     |
 |               |                 |    + tool_calls[]  ← |  (fixed)
 |               |                 |  • tool result       |
 |               |                 |    + tool_call_id  ← |  (fixed)
 |               |                 |<-- {"content":"…"}   |
 |               |<- model_        |                      |
 |               |   response      |                      |
 |<- (rendered   |                 |                      |
 |    live)      |                 |                      |
```

---

## Configuration

Both features are controlled by existing config keys:

```toml
# .nexus/config.toml or ~/.nexus/config.toml

# Show tool_call_requested, tool_result, thinking_started events.
show_tool_calls = true

# Use word-by-word typewriter rendering for model_response content.
stream_output = true
```

Setting `show_tool_calls = false` silences tool events (only the final
model response is printed). `stream_output = false` prints markdown in one
block without the typewriter effect.

---

## Next: [02-tools.md](02-tools.md)
