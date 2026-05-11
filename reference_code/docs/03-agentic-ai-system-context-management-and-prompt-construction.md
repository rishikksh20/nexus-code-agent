# 03. Agentic AI System Context Management and Prompt Construction: Memory Scaffolding, Token Accounting, and System Prompt Assembly

This document is a continuation of:

- `docs/01-agentic-ai-system-basics.md`
- `docs/02-agentic-ai-system-runtime-and-error-paths.md`

`01` established the model client and event basics.
`02` introduced runtime layering (`CLI -> Agent -> TUI`) and agent-level event routing.
`03` explains the next architectural step: introducing a context manager, explicit system prompt assembly, and token-aware text utilities.

---

## 1. High-level change in this iteration

The project shifts from a **single-turn runtime pipeline** to a **state-aware runtime scaffold**.

Previous effective flow (from `02`):

`main.py (CLI)` -> `Agent` -> `LLMClient` -> provider -> `AgentEvent` -> `TUI`

Current flow (this `03` step):

`main.py (CLI)` -> `Agent` -> `ContextManager` -> prompt/message assembly -> `LLMClient` -> provider -> `AgentEvent` -> `TUI`

The big conceptual shift is that the request is no longer assembled ad-hoc inside `Agent._agentic_loop()`. Message state now has a dedicated owner (`ContextManager`), which is the first prerequisite for durable multi-turn behavior.

---

## 2. Change scope since the previous commit

### New packages/modules introduced

- `core/context/__init__.py`
- `core/context/manager.py`
- `core/prompts/__init__.py`
- `core/prompts/system.py`
- `core/utils/__init__.py`
- `core/utils/text.py`

### Existing files updated

- `core/agent/agent.py`
- `main.py`
- `pyproject.toml`
- `uv.lock`

The center of gravity for this commit is clearly the new context/prompt utilities and their wiring into `Agent`.

---

## 3. Architectural delta: from in-method message creation to context-owned messages

## 3.1 Prior state (`02` baseline)

In `02`, `Agent._agentic_loop()` built a local `messages = [...]` list directly. That made the agent loop tightly coupled to request assembly and prevented message history growth.

## 3.2 Current state (`03`)

`core/agent/agent.py` now imports and instantiates `ContextManager`:

- `self.context_manager = ContextManager()` in `Agent.__init__`
- user input is persisted via `self.context_manager.add_user_message(message)`
- provider payload is read from `self.context_manager.get_messages()`
- assistant output is written back via `self.context_manager.add_assistant_message(response_text or None)`

This introduces a read/write boundary around conversation state.

### Why this matters

This is the first move toward a conversational memory model:

- writes before inference (`add_user_message`),
- read for model call (`get_messages`),
- write after inference (`add_assistant_message`).

Even though this is still minimal, the lifecycle structure is now correct for multi-turn expansion.

---

## 4. `Agent` integration details (`core/agent/agent.py`)

### 4.1 Constructor-level dependency expansion

`Agent` now owns two subsystems:

- `LLMClient` for provider communication
- `ContextManager` for conversation assembly and storage

That separates concerns more clearly than the previous in-loop hardcoded message list.

### 4.2 Input message handling now persists context

In `run(message: str)`:

1. emits `AgentEvent.agent_start(message)`
2. persists the user input in context manager (`add_user_message`)
3. streams response through `_agentic_loop()`
4. emits `agent_stop` at end

This is a conceptual progression from "process one input" to "update conversation state then infer".

### 4.3 Provider call payload now comes from context manager

The call changed from static list to:

```python
message=self.context_manager.get_messages()
```

This is the key runtime behavior change in this commit.

### 4.4 Assistant response is now stored

After streaming completes, `_agentic_loop()` persists assistant output:

```python
self.context_manager.add_assistant_message(response_text or None)
```

This closes the message-loop cycle and prepares history for subsequent turns.

---

## 5. New `ContextManager` abstraction (`core/context/manager.py`)

`ContextManager` is a dedicated state container for model-facing message payloads.

## 5.1 `MessageItem` as internal message unit

`MessageItem` dataclass introduces a structured internal representation:

- `role`
- `content`
- `token_count`

`to_dict()` acts as serialization boundary to provider-compatible payload.

This dual representation pattern is important:

- internal object for runtime metadata (`token_count`),
- external dict for API schema (`role`, `content`).

## 5.2 System prompt initialization

On init:

```python
self._system_prompt = get_system_prompt()
```

So every payload from `get_messages()` prepends the generated system instruction block when present.

## 5.3 Message append API

Two explicit append methods define ownership semantics:

- `add_user_message(content)`
- `add_assistant_message(content, tool_calls=None)`

Both compute token counts during insertion.

## 5.4 Payload assembly contract

`get_messages()` returns:

1. system message (if available)
2. each stored message converted through `item.to_dict()`

This guarantees payload structure is produced from one place, not scattered across runtime logic.

---

## 6. System prompt composition module (`core/prompts/system.py`)

This commit introduces a large prompt-construction module that builds system instructions from sections.

## 6.1 Composition strategy

`get_system_prompt()` collects parts and joins with blank lines:

- identity section
- AGENTS.md behavior section
- security section
- operational guidelines section

This gives a deterministic, template-driven prompt assembly pattern instead of a single hardcoded string.

## 6.2 Conceptual significance

The model is now primed with a full behavior specification generated by code, not inline constants.

This enables:

- modular prompt evolution,
- easier policy changes by section,
- future conditional assembly (tool-aware, user-instruction-aware, memory-aware).

## 6.3 Helper prompts included

The module also includes reusable helper generators:

- `get_compression_prompt()` for session handoff/continuation
- `create_loop_breaker_prompt(loop_description)` for loop mitigation

Even if not fully integrated yet, they signal movement toward agent orchestration utilities.

---

## 7. Token utilities introduced (`core/utils/text.py`)

This commit adds token-aware text helpers and introduces `tiktoken` dependency.

## 7.1 Tokenizer resolution

`get_tokenizer(model)`:

- tries `tiktoken.encoding_for_model(model)`
- falls back to `cl100k_base`

This provides model-aware behavior with resilient fallback.

## 7.2 Token counting path

`count_tokens(text, model="gpt-4")` uses tokenizer length, otherwise `estimate_tokens` fallback.

This function is now used by `ContextManager` when storing messages.

## 7.3 Truncation primitives

`truncate_text(...)` supports token-budget truncation with two strategies:

- line-preserving truncation (`_truncate_by_lines`)
- character-level binary search truncation (`_truncate_by_chars`)

This is foundational for future context-window management and prompt compression.

---

## 8. CLI behavior correction (`main.py`)

The prompt flow regression noted in `02` is corrected here.

Previous behavior used a hardcoded input:

```python
CLI().run_single("Hello World!")
```

Current behavior passes the actual CLI argument:

```python
CLI().run_single(prompt)
```

This aligns runtime input behavior with user intent and allows context manager history to reflect the real prompt.

---

## 9. Dependency update (`pyproject.toml`)

`pyproject.toml` now includes:

- `tiktoken>=0.12.0`

This dependency is required by `core/utils/text.py` for tokenizer-backed counting and truncation.

---

## 10. End-to-end request lifecycle after this commit

A single user prompt now executes as:

1. Click parses `prompt` in `main.py`.
2. `CLI.run_single(prompt)` enters `Agent` context.
3. `Agent.run(prompt)` emits start event and stores user message in `ContextManager`.
4. `ContextManager.get_messages()` builds payload with system prompt + history.
5. `LLMClient.chat_completion(..., stream=True)` sends request.
6. Stream deltas are mapped to `AgentEvent.text_delta` and rendered by `TUI`.
7. Final aggregated text is stored back via `add_assistant_message`.
8. `text_complete` and `agent_stop` close the turn.

Compared to `02`, the critical improvement is that request construction now depends on managed state, not local literals.

---

## 11. Conceptual progression from `02` to `03`

`02` introduced the runtime shell (events, TUI, error propagation).

`03` introduces the first memory/prompt substrate behind that shell:

- context state ownership
- generated system prompt
- token metadata capture
- truncation utilities for future window control

So the project progression is now:

1. **`01`**: client/event fundamentals
2. **`02`**: runtime/event routing and rendering
3. **`03`**: context and prompt infrastructure

This is a coherent layering sequence for agent systems.

---

## 12. Code-level nuance and implications

### 12.1 Message schema boundary is explicit

`MessageItem.to_dict()` is now the canonical serialization point before provider call. Any schema enrichment or tool-call structure can be added there later.

### 12.2 Token metadata is captured but not yet enforced

`token_count` is computed and stored per message, but there is no active context-budget pruning yet. This is a staging step toward budget-aware history management.

### 12.3 Prompt builder is modular but mostly static in this snapshot

`get_system_prompt()` composes fixed sections with some commented extension hooks (environment, developer/user instructions, memory/tools), indicating intended future dynamic assembly.

### 12.4 Truncation helpers are present but not wired into `ContextManager`

The utilities exist; enforcement policies (max prompt tokens, rolling window, message dropping/compression) are not yet integrated in `get_messages()`.

---

## 13. Delta summary table (`02` -> current)

| Area | `02` baseline | Current (`03`) delta |
|---|---|---|
| Message assembly | Local list inside `Agent._agentic_loop()` | Centralized in `ContextManager.get_messages()` |
| Conversation state | Implicit/transient | Explicit stored message history (`MessageItem`) |
| System instruction source | Basic inline prompt approach | Structured prompt composition in `core/prompts/system.py` |
| Token awareness | Not represented in context state | Per-message `token_count` via `count_tokens` |
| Context window tooling | Absent | Added `truncate_text` and helpers |
| CLI input wiring | Hardcoded runtime input in prior step | Uses actual `prompt` in `main.py` |
| Dependencies | `click`, `openai` | Added `tiktoken` for token-aware utilities |

---

## 14. Big-picture significance

This commit does not just add utility files; it changes where intelligence about prompt construction lives.

The system now has:

- a runtime layer (`02`) and
- a context/prompt substrate (`03`).

That combination is the foundation needed before advanced agent features become practical:

- bounded long conversations,
- retrieval-augmented context injection,
- tool transcripts in history,
- selective compression/summarization,
- and policy-controlled prompt assembly.

In short, `03` marks the shift from a streaming runtime prototype toward a stateful agent architecture.

---

## 15. Continuation pointer for next document

Natural next-step topics for `04` would be:

- context-window policy enforcement (using `truncate_text` in real flow),
- explicit model name propagation into `ContextManager` for accurate tokenization,
- selective message retention/summarization strategy,
- and tool-call message schema expansion in `MessageItem`.

That would complete the transition from "context scaffolding present" to "context budgeting actively enforced."
