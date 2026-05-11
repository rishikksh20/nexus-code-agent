# 02. Agentic AI System Runtime and Error Paths: CLI, Agent Events, TUI Streaming, and Failure Propagation

This document is a continuation of `docs/01-agentic-ai-system-basics.md`.

`01` explained the foundational model-client pipeline.
`02` explains what changed in the next development step: moving from a minimal event printer to a runtime-style agent flow with terminal UI streaming and explicit agent-level error handling.

---

## 1. What changed at a high level

Between the previous commit and this commit, the project shifts from a simple:

`main.py -> LLMClient -> print StreamEvent`

to a layered runtime flow:

`main.py (CLI)` -> `Agent` -> `LLMClient` -> provider stream -> `AgentEvent` -> `TUI`

This is a significant architectural move because:

- the application no longer renders provider events directly,
- domain-level events are introduced (`AgentEventType`),
- UI rendering is centralized in a dedicated terminal component,
- and failure behavior is handled through an agent event channel, not only raw client exceptions.

---

## 2. Files added and expanded in this step

### New runtime-oriented modules

- `core/agent/__init__.py`
- `core/agent/agent.py`
- `core/agent/events.py`
- `core/ui/__init__.py`
- `core/ui/tui.py`

### Existing modules updated

- `main.py`
- `core/client/llm_client.py`
- `core/client/datatype.py`
- `pyproject.toml`
- `uv.lock`

### Docs continuity

- `docs/01-agentic-ai-system-basics.md` expanded
- `docs/02-agentic-ai-system-runtime-and-error-paths.md` (this file)

---

## 3. Architectural delta: from client events to agent events

## 3.1 Before this step

The app consumed `StreamEvent` directly from `LLMClient.chat_completion(...)`.
That is good for initial prototyping, but it tightly couples UI/application logic to provider-level event structure.

## 3.2 After this step

A dedicated `Agent` layer now translates provider stream events into agent-level events:

- `StreamEventType.TEXT_DELTA` -> `AgentEventType.TEXT_DELTA`
- `StreamEventType.ERROR` -> `AgentEventType.AGENT_ERROR`
- stream completion -> `AgentEventType.TEXT_COMPLETE`
- lifecycle wrappers -> `AGENT_START`, `AGENT_STOP`

This introduces a boundary:

- provider/client internals can evolve,
- while the CLI/TUI can depend on a stable agent event contract.

This is a standard design in larger agentic systems.

---

## 4. `main.py` moved to a runtime CLI controller

`main.py` now defines a `CLI` class with two core methods:

- `run_single(message: str)`
- `_process_message(message: str)`

### 4.1 Runtime behavior in `_process_message`

`_process_message` iterates over `self.agent.run(message)` and dispatches by `AgentEventType`:

- `TEXT_DELTA`: starts assistant stream if needed and prints token deltas via TUI.
- `TEXT_COMPLETE`: stores final response and closes streaming block.
- `AGENT_ERROR`: closes stream if open and prints formatted error via TUI.

This gives the terminal path a deterministic event-to-render mapping.

### 4.2 Important nuance in this commit

`main()` currently checks for `prompt`, but invokes:

```python
CLI().run_single("Hello World!")
```

instead of passing `prompt` through.

Conceptually, the runtime path is now in place, but this line makes prompt input behavior fixed during this iteration.

---

## 5. New agent event model in `core/agent/events.py`

The file introduces:

- `AgentEventType` enum for lifecycle and text streaming states
- `AgentEvent` dataclass carrying `type` and `data`
- helper constructors (`agent_start`, `agent_stop`, `agent_error`, `text_delta`, `text_complete`)

This gives a single envelope for agent runtime state transitions.

### 5.1 Why this matters conceptually

This is the first explicit move from "raw model output" to "agent runtime protocol".

In larger systems, this protocol becomes the backbone for:

- logging
- UI rendering
- telemetry
- replay/debugging
- orchestration across multiple tools/calls

---

## 6. New agent loop in `core/agent/agent.py`

`Agent` now owns the model invocation loop and lifecycle wrapping.

## 6.1 `run(message)` lifecycle

`run` yields:

1. `AGENT_START`
2. all events from `_agentic_loop()`
3. `AGENT_STOP` with final response metadata placeholder

This creates explicit boundaries for one end-to-end agent turn.

## 6.2 `_agentic_loop()` stream translation

`_agentic_loop`:

- constructs system/user message list,
- calls `self.client.chat_completion(..., stream=True)`,
- accumulates `response_text`,
- emits `text_delta` events for incoming chunks,
- emits `agent_error` when client emits `StreamEventType.ERROR`,
- emits one `text_complete` if content was accumulated.

### 6.3 Important behavior note

The loop currently uses a hardcoded user message (`"capital of india ?"`) rather than the `message` from `run(...)`.
This is a known behavior characteristic of this commit snapshot and affects what the model answers.

---

## 7. Error-path reliability fixes in `core/client/llm_client.py`

This commit materially improves error surfacing behavior.

## 7.1 Retry boundary fix

The retry checks were changed to:

```python
if attempt < self._max_attempts - 1:
```

instead of comparing to `_max_attempts` directly.

Why this matters:

- With `range(self._max_attempts)`, attempts are `0..N-1`.
- Comparing with `< self._max_attempts` would always be true inside the loop.
- That can suppress the terminal error-yield branch.

The new condition makes the final attempt correctly emit `StreamEvent(type=ERROR, ...)`.

## 7.2 Remaining code nuance

`llm_client.py` still contains duplicate `except APIConnectionError` blocks in this snapshot.
It does not break runtime semantics severely, but is part of the current code state.

---

## 8. Stream event model adjustments in `core/client/datatype.py`

`StreamEventType` remains the client-level protocol:

- `TEXT_DELTA`
- `MESSAGE_COMPLETE`
- `ERROR`

`StreamEvent` remains the cross-path envelope for streaming and non-streaming calls.

The main conceptual change in this commit is not schema expansion; it is where these events are consumed (now mainly by the `Agent` layer).

---

## 9. New Rich-based terminal layer in `core/ui/tui.py`

A dedicated `TUI` class now controls assistant rendering.

### 9.1 Core TUI lifecycle methods

- `begin_assistant()`
  - prints a visual `Assistant` rule/header
  - marks stream-open state
- `stream_assistant_delta(content)`
  - prints streamed text chunks with `end=""`
- `end_assistant()`
  - finalizes stream and flushes newline to avoid shell prompt concatenation
- `print_error(error)`
  - prints a styled rich error line

### 9.2 Why this is important

This step separates:

- agent state machine concerns
- from terminal rendering concerns

That is foundational for later adding alternative frontends (web UI, logs, tests) without rewriting agent logic.

---

## 10. End-to-end flow after this commit

A single CLI call now follows this path:

1. Click command enters `main(prompt)`.
2. `CLI.run_single(...)` opens `Agent` context manager.
3. `CLI._process_message(...)` consumes `AgentEvent` stream.
4. `Agent.run(...)` emits lifecycle events and delegates to `_agentic_loop()`.
5. `_agentic_loop()` consumes `LLMClient.chat_completion(...)`.
6. Provider events are mapped to agent events.
7. `TUI` renders deltas, completion, and errors.
8. Agent/client resources are closed via async context exit.

This is now a real runtime pipeline, not only a demo script.

---

## 11. Error propagation path in this commit

This commit specifically improves the error visibility chain when model/config errors occur (for example, invalid model names):

- Provider/API raises an SDK exception.
- `LLMClient.chat_completion(...)` retries with backoff.
- Final failed attempt yields `StreamEventType.ERROR`.
- `Agent._agentic_loop()` maps it to `AgentEventType.AGENT_ERROR`.
- `CLI._process_message()` routes it to `TUI.print_error(...)`.
- Terminal displays formatted error output.

Conceptually, this confirms the project now has a full failure channel from provider boundary to UI boundary.

---

## 12. Big-picture significance

This transition is the first meaningful move from:

- "LLM API wrapper example"

to:

- "agent runtime substrate"

because the code now includes:

- lifecycle events,
- a domain event protocol,
- stream rendering abstraction,
- and explicit failure-path handling.

Those are the exact pieces that usually precede adding:

- tool invocation,
- planner loops,
- memory integration,
- and multi-turn orchestration.

---

## 13. Code-level delta summary table

| Area | Previous baseline (`01`) | Current commit delta |
|---|---|---|
| Entry flow | Direct async loop over `LLMClient` events | `CLI` controller consuming `AgentEvent` stream |
| Event contract | Client event model only (`StreamEventType`) | Added agent event model (`AgentEventType`, `AgentEvent`) |
| Runtime layer | No dedicated `Agent` loop | Added `Agent.run` + `_agentic_loop` lifecycle orchestration |
| UI output | Printed raw dataclass events | Added Rich `TUI` with stream header, token deltas, and error printer |
| Error visibility | Could be hidden by retry boundary behavior | Retry boundary fixed so terminal error events are emitted |
| Resource lifecycle | Minimal direct execution | Async context manager path through `Agent.__aenter__/__aexit__` |

---

## 14. Continuation link to next iterations

After this commit, the natural continuation in upcoming iterations is to stabilize message propagation and polish runtime contracts:

- feed CLI prompt end-to-end into `Agent` message list,
- remove hardcoded user message in `_agentic_loop()`,
- remove duplicate exception block in `LLMClient`,
- finalize event typing (`str | None`, state names),
- and optionally include usage/finish metadata in `AGENT_STOP`.

These are incremental hardening steps on top of the runtime architecture introduced here.

