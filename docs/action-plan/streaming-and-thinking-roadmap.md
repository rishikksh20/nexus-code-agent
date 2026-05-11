# Streaming and Thinking/Reasoning Support — Implementation Roadmap

This document is a step-by-step implementation plan to add **true SSE streaming** and **thinking/reasoning token** support to the Nexus agent. Both features must be activatable via config file, environment variable, and REPL slash command. All tool-call flow, guardrails, and confirmation handling must adapt cleanly.

---

## Current State (Baseline)

| Feature | State |
|---|---|
| HTTP call | Single blocking POST via `urllib`, full JSON body read at once |
| Streaming | `stream_output: bool` in config only controls REPL markdown rendering; `stream: true` is never sent to the API |
| Thinking | `show_thinking_indicator` only prints `⋯ thinking…` as a UI placeholder; no thinking tokens sent or parsed |
| Tool call assembly | Assembled after the full response is available |
| Guardrails/permissions | Applied after a complete `RuntimeResponse` is available |

---

## Scope of Changes

```
nexus/models.py                     ← new fields on RuntimeRequest, RuntimeResponse, Message
nexus/config/defaults.py            ← new config keys: enable_streaming, enable_thinking, thinking_budget_tokens
nexus/config/loader.py              ← map new env vars (AGENT_ENABLE_STREAMING, AGENT_ENABLE_THINKING)
nexus/integrations/openai_compatible.py  ← SSE parsing, thinking block parsing, streaming tool-call assembly
nexus/runtime/agent.py              ← consume async streamed chunks from client; emit thinking events
nexus/runtime/repl.py               ← render thinking blocks inline; handle streamed tool-call events
nexus/runtime/slash_commands.py     ← /provider set enable_streaming true|false, enable_thinking true|false
nexus/observability/logging.py      ← log thinking token counts in hook payloads
tests/test_streaming.py             ← new test file
tests/test_thinking.py              ← new test file
```

---

## Phase 1 — Data Model Changes

### Step 1.1 — Extend `RuntimeRequest` with streaming and thinking flags

**File:** `nexus/models.py`

Add two new fields to `RuntimeRequest`:

```python
@dataclass(slots=True, frozen=True)
class RuntimeRequest:
    model_name: str
    system_prompt: str
    messages: tuple[Message, ...]
    tool_schemas: tuple[dict[str, Any], ...] = ()
    max_output_tokens: int | None = None
    temperature: float = 0.0
    enable_streaming: bool = False   # NEW
    enable_thinking: bool = False    # NEW
    thinking_budget_tokens: int = 5000  # NEW — max tokens the model may spend thinking
```

### Step 1.2 — Add `thinking_content` to `Message`

Thinking blocks must be stored in history so the assistant's reasoning can be re-sent in multi-turn conversations (required by Anthropic; optional but safe for others).

```python
@dataclass(slots=True, frozen=True)
class Message:
    role: Role
    content: str
    name: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    thinking_content: str = ""   # NEW — raw thinking/reasoning block from the model
```

### Step 1.3 — Add `thinking_tokens` to `UsageSnapshot`

```python
@dataclass(slots=True, frozen=True)
class UsageSnapshot:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float = 0.0
    provider: str = ""
    model: str = ""
    thinking_tokens: int = 0   # NEW
```

### Step 1.4 — Add streaming-specific events to `AgentEvent`

The agent loop will emit these new event kinds as chunks arrive:

| `event.kind` | `event.payload` | When emitted |
|---|---|---|
| `thinking_chunk` | `str` — raw thinking text chunk | During model thinking stream |
| `thinking_completed` | `str` — full thinking block | After thinking stream finishes |
| `text_chunk` | `str` — partial response text | During content stream |
| `tool_call_chunk` | `dict` — partial tool-call delta | During tool-call stream |

No changes to the `AgentEvent` dataclass itself — `kind` and `payload` cover all cases.

---

## Phase 2 — Config Changes

### Step 2.1 — New keys in `AgentConfig`

**File:** `nexus/config/defaults.py`

```python
enable_streaming: bool = False        # send stream=true to the API
enable_thinking: bool = False         # enable thinking/reasoning tokens
thinking_budget_tokens: int = 5000    # max tokens for thinking block
```

`stream_output` remains (controls markdown rendering). `enable_streaming` is the new API-level flag.

### Step 2.2 — Environment variable mapping

**File:** `nexus/config/loader.py`

Add to the `AGENT_*` env var resolution block:

```python
"AGENT_ENABLE_STREAMING":       ("enable_streaming",       bool),
"AGENT_ENABLE_THINKING":        ("enable_thinking",         bool),
"AGENT_THINKING_BUDGET_TOKENS": ("thinking_budget_tokens",  int),
```

### Step 2.3 — `.nexus/config.toml` examples

Users can now set these in their workspace config:

```toml
enable_streaming = true
enable_thinking = true
thinking_budget_tokens = 8000
```

Or via environment:

```bash
AGENT_ENABLE_STREAMING=true
AGENT_ENABLE_THINKING=true
AGENT_THINKING_BUDGET_TOKENS=8000
```

---

## Phase 3 — Slash Command Support

### Step 3.1 — Add new settable params to `/provider set`

**File:** `nexus/runtime/slash_commands.py`

Add to `PROVIDER_SETTABLE_PARAMS`:

```python
PROVIDER_SETTABLE_PARAMS: frozenset[str] = frozenset({
    ...existing...,
    "enable_streaming",
    "enable_thinking",
    "thinking_budget_tokens",
})
```

Usage inside the REPL:

```text
/provider set enable_streaming true
/provider set enable_thinking true
/provider set thinking_budget_tokens 8000
```

### Step 3.2 — Update `/provider help` output

Add rows to the help table in `handle_provider`:

```python
("set enable_streaming true|false",  "Toggle SSE streaming for the current session.",      "/provider set enable_streaming true"),
("set enable_thinking true|false",   "Toggle thinking/reasoning tokens.",                  "/provider set enable_thinking true"),
("set thinking_budget_tokens <n>",   "Max tokens the model may spend on reasoning.",       "/provider set thinking_budget_tokens 8000"),
```

---

## Phase 4 — Streaming HTTP Client

This is the largest change. Replace the single `urllib` blocking call with an async SSE reader.

### Step 4.1 — SSE chunk parser utility

**File:** `nexus/integrations/openai_compatible.py` (add near top)

```python
def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """Parse a single SSE `data: ...` line. Returns None for keep-alive or [DONE]."""
    if not line.startswith("data: "):
        return None
    payload = line[6:].strip()
    if payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
```

### Step 4.2 — Streaming wire request builder

In `OpenAICompatibleAdapter.to_wire_request`, honor the new flags:

```python
def to_wire_request(self, request: RuntimeRequest) -> dict[str, Any]:
    ...existing payload building...

    if request.enable_streaming:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}  # OpenAI/Mistral style

    if request.enable_thinking:
        # Anthropic style (Claude):
        payload["thinking"] = {"type": "enabled", "budget_tokens": request.thinking_budget_tokens}
        # Mistral/OpenAI reasoning style (future):
        # payload["reasoning_effort"] = "high"

    return payload
```

> **Provider note:** Thinking/reasoning API fields differ by provider:
> - Anthropic Claude: `thinking: {type: "enabled", budget_tokens: N}` and response has `type: "thinking"` content blocks
> - OpenAI o-series: `reasoning_effort: "low"|"medium"|"high"` and response has `reasoning_content`
> - Mistral (future): TBD, likely `reasoning_effort`
>
> Phase 6 handles provider-specific normalization.

### Step 4.3 — Async streaming HTTP call

Replace `_send_request` with two methods:

```python
async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
    wire_payload = self.adapter.to_wire_request(request)
    if request.enable_streaming:
        return await self._stream_request(wire_payload)
    response_payload = await call_with_backoff(
        lambda: asyncio.to_thread(self._send_request, wire_payload),
        ...
    )
    return self.adapter.from_wire_response(response_payload)

async def _stream_request(self, payload: dict[str, Any]) -> RuntimeResponse:
    """Send a streaming request and aggregate chunks into a RuntimeResponse."""
    body = json.dumps(payload).encode("utf-8")
    # Use asyncio.to_thread so the blocking socket read doesn't block the event loop.
    # A future improvement is to use httpx or aiohttp for true async I/O.
    return await asyncio.to_thread(self._stream_request_sync, body)

def _stream_request_sync(self, body: bytes) -> RuntimeResponse:
    req = urllib_request.Request(
        _chat_completions_url(self.api_base_url),
        data=body,
        headers=_request_headers(self.api_key),
        method="POST",
    )
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_call_accumulators: dict[int, dict] = {}
    usage: UsageSnapshot | None = None

    with urllib_request.urlopen(req, timeout=self.timeout_seconds) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").rstrip("\n\r")
            chunk = _parse_sse_line(line)
            if chunk is None:
                continue
            _accumulate_streaming_chunk(
                chunk,
                content_parts=content_parts,
                thinking_parts=thinking_parts,
                tool_call_accumulators=tool_call_accumulators,
            )
            if "usage" in chunk:
                usage = _parse_usage(chunk["usage"], provider=self.provider_name, model=chunk.get("model", ""))

    return self.adapter.from_streaming_parts(
        content_parts=content_parts,
        thinking_parts=thinking_parts,
        tool_call_accumulators=tool_call_accumulators,
        usage=usage,
    )
```

### Step 4.4 — Chunk accumulator

```python
def _accumulate_streaming_chunk(
    chunk: dict[str, Any],
    *,
    content_parts: list[str],
    thinking_parts: list[str],
    tool_call_accumulators: dict[int, dict],
) -> None:
    for choice in chunk.get("choices", []):
        delta = choice.get("delta", {})

        # --- thinking block (Anthropic content block style) ---
        for block in delta.get("content", []) if isinstance(delta.get("content"), list) else []:
            if block.get("type") == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif block.get("type") == "text":
                content_parts.append(block.get("text", ""))

        # --- plain text delta (OpenAI/Mistral style) ---
        if isinstance(delta.get("content"), str):
            content_parts.append(delta["content"])

        # --- reasoning_content (OpenAI o-series style) ---
        if isinstance(delta.get("reasoning_content"), str):
            thinking_parts.append(delta["reasoning_content"])

        # --- tool_calls delta ---
        for tc_delta in delta.get("tool_calls", []):
            idx = tc_delta["index"]
            acc = tool_call_accumulators.setdefault(idx, {
                "id": "", "name": "", "arguments": ""
            })
            acc["id"] += tc_delta.get("id", "")
            acc["name"] += tc_delta.get("function", {}).get("name", "")
            acc["arguments"] += tc_delta.get("function", {}).get("arguments", "")
```

### Step 4.5 — `from_streaming_parts` on the adapter

```python
def from_streaming_parts(
    self,
    *,
    content_parts: list[str],
    thinking_parts: list[str],
    tool_call_accumulators: dict[int, dict],
    usage: UsageSnapshot | None,
) -> RuntimeResponse:
    content = "".join(content_parts)
    thinking = "".join(thinking_parts)
    tool_calls = tuple(
        ToolCall(
            call_id=acc["id"],
            tool_name=acc["name"],
            arguments=json.loads(acc["arguments"]) if acc["arguments"] else {},
        )
        for acc in tool_call_accumulators.values()
        if acc["name"]
    )
    message = Message(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        thinking_content=thinking,
    )
    return RuntimeResponse(message=message, tool_calls=tool_calls, usage=usage)
```

---

## Phase 5 — Streaming Events in the Agent Loop

### Step 5.1 — Emit chunk events during a streaming call

The current `Agent.run()` awaits the full `complete()` response. For streaming, we need the client to yield chunks. Two approaches:

**Option A (recommended for minimal refactor):** Keep `complete()` returning a full `RuntimeResponse` (chunks aggregated inside the client). Add a separate `stream()` method that yields events as chunks arrive.

**Option B:** Replace `complete()` with an async generator that yields `AgentEvent` chunks directly.

Use **Option A** to keep the non-streaming path unchanged and all existing tests valid:

```python
# nexus/integrations/openai_compatible.py — add alongside complete()
async def stream(self, request: RuntimeRequest):
    """Yield (event_kind, payload) pairs as SSE chunks arrive.
    
    Yields:
        ("thinking_chunk", str)      — thinking text fragment
        ("text_chunk", str)          — content text fragment
        ("tool_call_chunk", dict)    — partial tool-call delta
        ("response_complete", RuntimeResponse)  — final assembled response
    """
    wire_payload = self.adapter.to_wire_request(request)
    body = json.dumps(wire_payload).encode("utf-8")
    req = urllib_request.Request(
        _chat_completions_url(self.api_base_url),
        data=body,
        headers=_request_headers(self.api_key),
        method="POST",
    )
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_call_accumulators: dict[int, dict] = {}
    usage = None

    def _iter_lines():
        with urllib_request.urlopen(req, timeout=self.timeout_seconds) as resp:
            for raw in resp:
                yield raw.decode("utf-8").rstrip("\n\r")

    for line in await asyncio.to_thread(list, _iter_lines()):
        chunk = _parse_sse_line(line)
        if chunk is None:
            continue
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            # thinking
            if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                thinking_parts.append(delta["reasoning_content"])
                yield ("thinking_chunk", delta["reasoning_content"])
            # text
            if isinstance(delta.get("content"), str) and delta["content"]:
                content_parts.append(delta["content"])
                yield ("text_chunk", delta["content"])
            # tool calls
            for tc_delta in delta.get("tool_calls", []):
                idx = tc_delta["index"]
                acc = tool_call_accumulators.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                acc["id"] += tc_delta.get("id", "")
                acc["name"] += tc_delta.get("function", {}).get("name", "")
                acc["arguments"] += tc_delta.get("function", {}).get("arguments", "")
                yield ("tool_call_chunk", {"index": idx, **tc_delta})
        if "usage" in chunk:
            usage = _parse_usage(chunk["usage"], provider=self.provider_name, model=chunk.get("model", ""))

    final = self.adapter.from_streaming_parts(
        content_parts=content_parts,
        thinking_parts=thinking_parts,
        tool_call_accumulators=tool_call_accumulators,
        usage=usage,
    )
    yield ("response_complete", final)
```

### Step 5.2 — Agent loop: branch on streaming

**File:** `nexus/runtime/agent.py`

In `Agent.run()`, detect whether the client supports `stream()`:

```python
enable_streaming = getattr(request, "enable_streaming", False)

if enable_streaming and hasattr(self.model_client, "stream"):
    async for event_kind, payload in self.model_client.stream(request):
        if event_kind == "thinking_chunk":
            yield AgentEvent(kind="thinking_chunk", payload=payload)
        elif event_kind == "text_chunk":
            yield AgentEvent(kind="text_chunk", payload=payload)
        elif event_kind == "tool_call_chunk":
            yield AgentEvent(kind="tool_call_chunk", payload=payload)
        elif event_kind == "response_complete":
            response = payload
            # ... continue with existing tool-call handling using final response
else:
    response = await self.model_client.complete(request)
    # ... existing path unchanged
```

The `RuntimeRequest` must carry both flags through from the `ReplState` config. Update `_stream_turn_live` and `collect_turn_events` in `repl.py` to pass them:

```python
request = RuntimeRequest(
    ...existing...,
    enable_streaming=state.config.enable_streaming,
    enable_thinking=state.config.enable_thinking,
    thinking_budget_tokens=state.config.thinking_budget_tokens,
)
```

---

## Phase 6 — Provider-Specific Thinking Normalization

Different providers use different wire formats for thinking. A thin normalization layer in the adapter handles this:

```python
# nexus/integrations/openai_compatible.py

def _apply_thinking_params(payload: dict, provider: str, budget: int) -> None:
    """Inject the provider-correct thinking parameter into the request payload."""
    if provider == "anthropic":
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif provider in {"openai", "openai-compatible"}:
        # OpenAI o-series uses reasoning_effort; budget_tokens not yet supported
        payload["reasoning_effort"] = "high"
    elif provider == "mistral":
        # Mistral reasoning API TBD — skip silently until announced
        pass
```

Call this from `to_wire_request` when `enable_thinking` is `True`.

### Provider support matrix

| Provider | Streaming | Thinking |
|---|---|---|
| `mistral` (mistral-medium-latest) | ✅ SSE supported | ⚠️ Not yet in public API (skip silently) |
| `openai` (gpt-4o, o3) | ✅ SSE supported | ✅ via `reasoning_effort` (o-series only) |
| `anthropic` (Claude 3.5+) | ✅ SSE supported | ✅ via `thinking` block |
| `openai-compatible` (local) | Depends on server | Depends on server |
| `fake` | N/A (simulated) | N/A (simulated) |

When `enable_thinking` is `True` but the provider does not support it, log a warning and continue without the parameter rather than crashing.

---

## Phase 7 — REPL Rendering Changes

### Step 7.1 — Render thinking blocks

**File:** `nexus/runtime/repl.py`

Add to `_render_event`:

```python
elif event.kind == "thinking_chunk":
    # Print thinking text inline with dim italic style (only when show_tool_calls is on)
    if show_tool_calls:
        console.print(event.payload, end="", style="dim italic")

elif event.kind == "thinking_completed":
    # Print a closing newline after the thinking block
    if show_tool_calls:
        console.print()

elif event.kind == "text_chunk":
    # Print content text fragments without a newline — accumulate in place
    console.print(event.payload, end="")
```

The `model_response` event handler still runs for non-streaming and for the final accumulated response in streaming mode.

### Step 7.2 — `_stream_turn_live` handles streaming events

The existing `_stream_turn_live` already calls `_render_event` per event. No structural change needed — only the new event kinds in `_render_event` (Step 7.1) are required.

### Step 7.3 — Show `[dim]⋯ thinking…[/dim]` only for non-streaming

Currently `thinking_started` shows the placeholder. When streaming is active and `enable_thinking` is true, `thinking_chunk` events provide real content, so suppress the placeholder:

```python
elif event.kind == "thinking_started" and show_tool_calls:
    if not stream_output:   # suppress placeholder when live chunks will show instead
        console.print("[dim]⋯ thinking…[/dim]")
```

---

## Phase 8 — History Serialization

`thinking_content` on `Message` must survive serialization to session snapshots.

**File:** `nexus/runtime/sessions.py` — `SessionSnapshot.to_dict()`:

```python
{"role": message.role, "content": message.content, "name": message.name,
 "thinking_content": message.thinking_content}  # NEW
```

**`SessionSnapshot.from_dict()`:**

```python
messages = [
    Message(
        role=item["role"],
        content=item["content"],
        name=item.get("name"),
        thinking_content=item.get("thinking_content", ""),   # NEW — default empty for old sessions
    )
    for item in raw_messages
]
```

**`apply_events_to_history` in `repl.py`:**

```python
elif event.kind == "model_response":
    state.history.append(event.payload.message)   # already has thinking_content set
```

No additional change needed there.

**Multi-turn re-submission of thinking blocks:**

For providers that require thinking blocks to be re-sent (Anthropic), `to_wire_request` must include them when present:

```python
if message.role == "assistant" and message.thinking_content and provider == "anthropic":
    item["content"] = [
        {"type": "thinking", "thinking": message.thinking_content},
        {"type": "text", "text": message.content},
    ]
```

---

## Phase 9 — Guardrails and Permission Gating

All permission and guardrail checks happen on the **assembled `ToolCall` objects**, not on raw chunks. Because Phase 4/5 fully assembles tool calls from the stream before permission checks run, no changes to `PermissionChecker` or confirmation flow are required.

However, two specific adjustments are needed:

### Step 9.1 — Validate assembled tool-call arguments after streaming

After stream ends, validate that accumulated `arguments` JSON is well-formed before executing:

```python
try:
    args = json.loads(acc["arguments"]) if acc["arguments"] else {}
except json.JSONDecodeError:
    args = {}   # treat malformed args as empty — guardrails will catch missing required fields
```

This is already in `from_streaming_parts` (Phase 4.5).

### Step 9.2 — Streaming incomplete tool calls (network cut-off)

If the stream ends without a `[DONE]` marker (connection dropped), `from_streaming_parts` is called with whatever was accumulated. Partial tool-call arguments will fail JSON parsing → treated as empty → agent will request clarification from the user via the existing `_missing_required_fields` path. No special case needed.

### Step 9.3 — Thinking content in audit trail

**File:** `nexus/observability/logging.py`

When `enable_thinking` is active, add `thinking_tokens` to `model_usage` hook payloads:

```python
"thinking_tokens": response.usage.thinking_tokens if response.usage else 0,
```

---

## Phase 10 — Tests

### New file: `tests/test_streaming.py`

```
- test that stream=true is in the wire payload when enable_streaming=True
- test that stream=false / absent when enable_streaming=False
- test that _parse_sse_line handles keep-alive lines, [DONE], and malformed JSON
- test that _accumulate_streaming_chunk assembles text, thinking, and tool_calls correctly
- test that from_streaming_parts produces a correct RuntimeResponse
- test that a connection-dropped stream (no [DONE]) returns a partial response, not a crash
- test that streaming path emits text_chunk and thinking_chunk AgentEvents
- test that tool-call assembly from chunks matches non-streaming tool-call handling
```

### New file: `tests/test_thinking.py`

```
- test that thinking payload is injected per provider (anthropic, openai, mistral)
- test that thinking_content is stored on Message after a response
- test that thinking_content is round-tripped through session serialization
- test that old session files without thinking_content load correctly (default "")
- test that thinking_tokens is recorded in UsageSnapshot
- test that thinking block is re-sent in multi-turn history for anthropic provider
```

### Updates to existing tests

```
tests/test_agent.py        — add fake model that yields streaming events; assert thinking_chunk emitted
tests/test_sessions.py     — assert thinking_content field survives save/load round-trip
tests/test_config.py       — assert enable_streaming, enable_thinking, thinking_budget_tokens load from toml and env
tests/test_slash_commands.py — assert /provider set enable_streaming true updates state.config
```

---

## Phase 11 — Fake Model Adapter (for offline tests)

**File:** `nexus/integrations/fake_model.py`

Add a `stream()` method to `FakeModelClient` that mirrors the real client interface:

```python
async def stream(self, request):
    response = await self.complete(request)
    # Emit the full content as a single text_chunk, then complete
    if response.message.content:
        yield ("text_chunk", response.message.content)
    if response.message.thinking_content:
        yield ("thinking_chunk", response.message.thinking_content)
    yield ("response_complete", response)
```

This lets all agent loop tests run without a live provider.

---

## Phase 12 — Documentation Updates

After all above is implemented and tests pass, update:

- `README.md` — add `enable_streaming`, `enable_thinking`, `thinking_budget_tokens` to the config section and `/provider set` slash command table
- `docs/action-plan/02-runtime-and-safety/04-session-state-context-and-memory.md` — update Current Nexus Notes
- `docs/openai-code-tutorial/01-1-streaming.md` — add section on SSE chunk parsing and the streaming agent path
- `next_roadmap.md` — mark streaming and thinking as implemented

---

## Implementation Order and Dependencies

```
Phase 1  (models)        — no deps — do first
Phase 2  (config)        — after Phase 1
Phase 3  (slash cmds)    — after Phase 2
Phase 4  (HTTP client)   — after Phase 1 — largest change, do in isolation
Phase 5  (agent loop)    — after Phase 4
Phase 6  (provider norm) — after Phase 4
Phase 7  (REPL render)   — after Phase 5
Phase 8  (history)       — after Phase 1
Phase 9  (guardrails)    — after Phase 4 — minimal changes
Phase 10 (tests)         — after all phases
Phase 11 (fake model)    — after Phase 5 — needed for tests
Phase 12 (docs)          — last
```

Recommended PR order: 1 → 2 → 8 → 4 → 6 → 5 → 3 → 7 → 9 → 11 → 10 → 12

---

## Risk and Mitigation

| Risk | Mitigation |
|---|---|
| Provider sends thinking blocks in a different format than expected | Normalize in `_accumulate_streaming_chunk`; fall back to empty string on unknown format |
| Stream connection drops mid-tool-call | Partial JSON args fall back to empty dict; agent requests clarification via existing path |
| `urllib` blocks the event loop during SSE read | `asyncio.to_thread` wraps all sync I/O; future: replace with `httpx` or `aiohttp` |
| Old sessions without `thinking_content` fail to load | `item.get("thinking_content", "")` default in `from_dict` |
| Thinking enabled for a provider that doesn't support it | Per-provider `_apply_thinking_params` silently skips unsupported providers |
| Tool-call chunk JSON is split across two SSE lines | Accumulate `arguments` string across all chunks before calling `json.loads` — already handled by accumulator pattern |

---

## Definition of Done

- [ ] `enable_streaming = true` in `.nexus/config.toml` → `stream: true` appears in the wire request
- [ ] Text arrives in the REPL character-by-character as chunks stream in
- [ ] `enable_thinking = true` → reasoning block appears in dim italic before the final response
- [ ] `/provider set enable_streaming true` and `/provider set enable_thinking true` work live in the REPL without restart
- [ ] `AGENT_ENABLE_STREAMING=true` environment variable activates streaming
- [ ] Tool calls assembled from streaming chunks pass through the same permission/guardrail gates as non-streaming tool calls
- [ ] Session snapshots round-trip `thinking_content` correctly
- [ ] Old sessions without `thinking_content` still load without error
- [ ] All existing 197 tests still pass
- [ ] New streaming and thinking tests pass
- [ ] `FakeModelClient.stream()` supports offline testing
