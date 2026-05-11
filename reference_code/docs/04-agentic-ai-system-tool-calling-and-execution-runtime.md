# 04. Agentic AI System Tool Calling and Execution Runtime: Schemas, Invocation, Streaming Events, and Terminal Tool UX

This document is a continuation of:

- `docs/01-agentic-ai-system-basics.md`
- `docs/02-agentic-ai-system-runtime-and-error-paths.md`
- `docs/03-agentic-ai-system-context-management-and-prompt-construction.md`

`01` established the client and internal event basics.
`02` introduced the runtime shell (`CLI -> Agent -> TUI`) and agent-level event routing.
`03` added managed context, prompt construction, and token-aware utilities.
`04` explains the next major architectural step: the runtime is no longer only **context-aware**; it is now becoming **tool-capable**.

In this stage, the code adds:

- a tool abstraction layer,
- a registry of available tools,
- a first builtin tool (`read_file`),
- streaming support for tool-call events in the client layer,
- agent-side execution of requested tools,
- context support for tool-result messages,
- and a richer terminal UI for tool execution.

---

## 1. High-level change in this iteration

The project shifts from a **state-aware chat runtime** to an **action-capable agent runtime**.

Previous effective flow (from `03`):

`main.py (CLI)` -> `Agent` -> `ContextManager` -> prompt/message assembly -> `LLMClient` -> provider -> `AgentEvent` -> `TUI`

Current flow (this `04` step):

`main.py (CLI)` -> `Agent` -> `ContextManager` -> `LLMClient` (+ tool schemas) -> provider emits text/tool events -> `Agent` executes requested local tools -> tool results are added back to context -> `AgentEvent` -> `TUI`

That is the big conceptual shift.

The model is no longer treated only as a text generator. It is now treated as a planner that can request external actions through a structured tool interface.

This matters because tool use is one of the core boundaries between a chatbot and an actual agent runtime. A tool-capable agent can do work against the environment instead of only describing what it would do.

---

## 2. Change scope since the previous commit

### New packages/modules introduced

- `core/tools/__init__.py`
- `core/tools/base.py`
- `core/tools/registry.py`
- `core/tools/builtin/__init__.py`
- `core/tools/builtin/read_file.py`
- `core/utils/paths.py`

### Existing files updated

- `main.py`
- `core/agent/agent.py`
- `core/agent/events.py`
- `core/client/datatype.py`
- `core/client/llm_client.py`
- `core/context/manager.py`
- `core/ui/tui.py`
- `pyproject.toml`
- `uv.lock`

### Center of gravity in this commit

The center of gravity is the introduction of a **tool runtime**.

If `03` was about *what messages the model sees*, then `04` is about *what actions the model can request and how the runtime executes them*.

---

## 3. Architectural delta: from context-aware assistant to tool-capable agent

### 3.1 Prior state (`03` baseline)

At the end of `03`, the system had:

- a `ContextManager` that owned message history,
- a generated system prompt,
- token-aware message accounting,
- an `Agent` that streamed assistant text,
- and a `TUI` that rendered streaming text and errors.

But the effective contract was still:

> send messages -> receive text -> render text

There was no formal action layer between the model and the local environment.

### 3.2 Current state (`04`)

The system now inserts a tool layer across multiple parts of the stack:

- **tool definitions** live in `core/tools/base.py`
- **tool registration and lookup** live in `core/tools/registry.py`
- **builtin tool implementations** live in `core/tools/builtin/`
- **tool-call streaming events** live in `core/client/datatype.py`
- **provider request tool schema wiring** lives in `core/client/llm_client.py`
- **tool execution orchestration** lives in `core/agent/agent.py`
- **tool result messages in context** live in `core/context/manager.py`
- **tool visualization** lives in `main.py` and `core/ui/tui.py`

So the runtime is no longer purely message-driven. It is now event-driven **and** action-driven.

---

## 4. Big-picture runtime model after `04`

A single request can now conceptually unfold like this:

1. the user enters a prompt,
2. `Agent` stores it in `ContextManager`,
3. `Agent` asks `LLMClient` for a streamed response,
4. the provider can emit either:
   - text deltas,
   - or tool-call information,
5. the client normalizes those chunks into internal `StreamEvent` objects,
6. the `Agent` collects completed tool calls,
7. the `Agent` executes the matching local tools through `ToolRegistry`,
8. the results are emitted as `AgentEvent` objects for the UI,
9. tool outputs are also appended back into context as `tool` messages.

That is a significant conceptual upgrade because the model is now participating in a protocol, not just returning prose.

---

## 5. New tool abstraction layer (`core/tools/base.py`)

`core/tools/base.py` defines the internal contract for every tool in the system.

This file is one of the most important additions in this iteration because it establishes the **common language for action execution**.

### 5.1 `ToolKind`

`ToolKind` categorizes tools by operational class:

- `READ`
- `WRITE`
- `SHELL`
- `NETWORK`
- `MEMORY`
- `MCP`

This is not just labeling. It creates a structural way to reason about tool safety, UI styling, and future policy decisions.

For example:

- a `READ` tool can use one visual style,
- a `SHELL` tool may later need more caution,
- a `NETWORK` tool may later need extra approval or sandboxing.

### 5.2 `ToolInvocation`

```python
@dataclass
class ToolInvocation:
    cwd: Path
    params: dict[str, Any]
```

This object bundles the execution environment for a tool call:

- `cwd` tells the tool its working directory context,
- `params` carries validated user/model-supplied arguments.

That design is useful because it makes tool execution explicit and context-aware without leaking the whole agent runtime into each tool.

### 5.3 `ToolResult`

```python
@dataclass
class ToolResult:
    success: bool
    output: str
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
```

This is the canonical return type for local tool execution.

It is doing several jobs at once:

- `success` tells the runtime whether the tool worked,
- `output` holds the user/model-facing payload,
- `error` carries failure explanation,
- `metadata` carries structured side information,
- `truncated` tells downstream consumers that the returned payload was shortened.

That is a good agent-oriented design because tools do not just return “a string.” They return both content and execution state.

### 5.4 `to_model_output()`

`ToolResult.to_model_output()` converts a tool result into a text block suitable for model consumption.

This is a subtle but important bridge. It means the system distinguishes between:

- the **runtime result object** used by Python code, and
- the **serialized tool transcript** that can later be sent back to the model.

That is foundational for multi-step tool loops.

### 5.5 Validation and schema export

The `Tool` base class now includes:

- `validate_params(...)`
- `get_confirmation(...)`
- `to_openai_schema(...)`

A useful tool system needs two directions of translation:

1. **from Python to provider schema** so the model knows what tools exist and what parameters they expect,
2. **from provider arguments back to validated Python parameters** so local execution stays structured and safe.

That is exactly what this base layer starts to provide.

### 5.6 Pydantic-backed schemas

The base tool contract supports Pydantic schemas. That is why `pydantic` is now added as a dependency in `pyproject.toml`.

This gives the tool layer:

- typed argument definitions,
- automatic validation,
- reusable JSON schema generation,
- and cleaner parameter error messages.

So the tool system is not based on ad-hoc dictionaries alone. It is starting from explicit typed contracts.

---

## 6. Tool registration and discovery (`core/tools/registry.py`)

Once tools exist as classes, the runtime needs a place to store, discover, and execute them. That is the role of `ToolRegistry`.

### 6.1 Registry responsibilities

`ToolRegistry` handles:

- registration (`register`),
- lookup (`get`),
- enumeration (`get_tools`),
- schema export (`get_schemas`),
- and execution (`invoke`).

So this file is the operational center of the local tool runtime.

### 6.2 Why the registry matters architecturally

Without a registry, the `Agent` would need hardcoded conditionals for each tool name. That does not scale.

A registry makes tools pluggable and keeps the agent loop generic. The agent can now reason in terms of:

- “what tools are available?”
- “what schemas should I advertise?”
- “invoke the tool named X with params Y”

rather than directly depending on individual tool classes.

### 6.3 `invoke(...)`

`invoke(...)` standardizes the execution sequence:

1. find the tool by name,
2. validate params,
3. create a `ToolInvocation`,
4. execute the tool,
5. wrap failures in `ToolResult.error_result(...)` if needed.

That gives the rest of the runtime one predictable execution pathway.

### 6.4 Default registry creation

`create_default_registry()` loads builtin tools from `core/tools/builtin/__init__.py`.

This means the runtime now has the concept of a **default local tool set**, which is an important step toward a configurable agent environment.

---

## 7. Builtin tool package and first concrete tool

The new builtin tool package currently exposes one tool:

- `ReadFileTool`

This matters conceptually because `04` is not just adding a framework with no execution path. It ships the first end-to-end concrete tool implementation.

---

## 8. `ReadFileTool` walkthrough (`core/tools/builtin/read_file.py`)

`ReadFileTool` is the first fully integrated local action in the system.

### 8.1 Why `read_file` is a good first tool

A file-reading tool is an ideal first step for a coding agent because it is:

- highly useful,
- naturally scoped to the workspace,
- mostly deterministic,
- easier to reason about than shell/network tools,
- and directly relevant to code understanding tasks.

So this tool is not random; it is a strong foundational tool for a programming-oriented agent.

### 8.2 Pydantic input contract

`ReadFileParams` defines the arguments:

- `path`
- `offset`
- `limit`

That means the model is not just told “there is a read_file tool.” It is also told the parameter contract in machine-readable form.

This is an important shift from freeform text instructions to structured callable interfaces.

### 8.3 Execution flow

`ReadFileTool.execute(...)` performs a careful sequence:

1. parse args into `ReadFileParams`,
2. resolve path relative to working directory,
3. validate existence and file type,
4. reject oversized files,
5. reject binary files,
6. read file contents with encoding fallback,
7. slice by offset/limit,
8. add line numbers,
9. token-count output,
10. truncate if needed,
11. return structured `ToolResult` with metadata.

This is a useful example because it shows how tools in this project are intended to behave:

- structured inputs,
- defensive checks,
- token-aware outputs,
- and rich metadata.

### 8.4 Token-aware output limiting

The tool reuses `count_tokens(...)` and `truncate_text(...)` from `core/utils/text.py`.

That is an important continuity point from `03`. The token-awareness introduced there is now being used not just for prompt construction, but also for tool outputs.

### 8.5 Metadata design

The tool returns metadata such as:

- `path`
- `total_lines`
- `shown_start`
- `shown_end`

That enables the UI layer to render the result more intelligently.

This is a good example of why `ToolResult.metadata` exists:

- the model may mainly care about text,
- but the UI may care about richer presentation context.

---

## 9. New path utilities (`core/utils/paths.py`)

The tool runtime also introduces a small but important path utility module.

It provides:

- `resolve_path(...)`
- `display_path_rel_to_cwd(...)`
- `ensure_parent_directory(...)`
- `is_binary_file(...)`

Even a simple read tool needs utility support for:

- relative vs absolute paths,
- workspace-friendly display,
- binary detection,
- and future file-writing helpers.

So `core/utils/paths.py` is more than convenience code. It is part of the environmental safety and usability layer for tool execution.

---

## 10. Client event model expansion (`core/client/datatype.py`)

The internal event/data contract has expanded significantly in this iteration.

This is where the project begins to represent tool calling as a first-class protocol.

### 10.1 `StreamEventType` grows beyond text/error

Previously the client event vocabulary centered on:

- `TEXT_DELTA`
- `MESSAGE_COMPLETE`
- `ERROR`

Now it also includes:

- `TOOL_CALL_START`
- `TOOL_CALL_DELTA`
- `TOOL_CALL_COMPLETE`

That is a major conceptual change. The model response channel is no longer only text. It is now a multiplexed event stream containing both language output and action requests.

### 10.2 `ToolCallDelta`

`ToolCallDelta` models partial streamed tool-call information:

- `call_id`
- `name`
- `arguments_delta`

This matters because tool calls can arrive incrementally just like text. A provider may stream:

- function name first,
- then arguments piece by piece,
- then a completion signal.

### 10.3 `ToolCall`

`ToolCall` represents the assembled call:

- `call_id`
- `name`
- `arguments`

This is the object the `Agent` can actually execute.

So the distinction is important:

- `ToolCallDelta` models the stream protocol,
- `ToolCall` models the actionable completed unit.

### 10.4 `ToolResultMessage`

`ToolResultMessage` represents tool output in a form that can be turned into a model-facing message.

This shows the architecture is already thinking in terms of the full loop:

1. model requests a tool,
2. runtime executes the tool,
3. runtime packages the result back into conversation state,
4. model can later consume that result.

### 10.5 `parse_tool_call_arguments(...)`

This helper parses the provider's tool-call argument payload. It tries JSON first and falls back to:

```python
{"raw_arguments": arguments}
```

That is a pragmatic normalization step. The runtime needs a way to turn raw streamed argument text into a Python dictionary before local execution can happen.

---

## 11. `LLMClient` becomes tool-aware (`core/client/llm_client.py`)

In `03`, `LLMClient` knew how to send messages and normalize text/error responses. In `04`, it also knows how to:

- advertise available tools to the provider,
- interpret streamed tool-call chunks,
- accumulate tool-call arguments,
- and emit tool-call events.

### 11.1 Request schema now includes tools

`chat_completion(...)` now accepts:

```python
tools: list[dict[str, Any]] | None = None
```

and conditionally adds:

- `tools`
- `tool_choice = "auto"`

into request kwargs.

This is the first moment where the provider is given a machine-readable action surface.

### 11.2 `_build_tool(...)`

`_build_tool(...)` translates internal tool schema dictionaries into the API shape expected by the provider.

So the client now bridges:

- the runtime's internal tool representation,
- and the provider's OpenAI-style function/tool schema.

### 11.3 Stream parsing now handles tool calls

Inside `_stream_response(...)`, the client:

- still yields `TEXT_DELTA` for text,
- now watches `delta.tool_calls`,
- accumulates tool call fragments by index,
- and finally emits `TOOL_CALL_COMPLETE` events with parsed arguments.

So the client is no longer just a text-stream adapter. It is becoming a protocol translator for a richer provider response format.

### 11.4 Why the final completed tool object matters

The `Agent` does not want to reason about partial provider chunk shapes. It wants a fully assembled call like:

- tool name,
- call id,
- arguments dictionary.

That is why the completed `ToolCall` event is the key handoff point between the provider-facing layer and the runtime layer.

---

## 12. Agent orchestration changes (`core/agent/agent.py`)

`Agent` is where the new tool protocol becomes actual local behavior.

### 12.1 Constructor now owns a tool registry

`Agent.__init__` now creates:

```python
self.tool_registry = create_default_registry()
```

So the `Agent` now owns three subsystems:

- `LLMClient`
- `ContextManager`
- `ToolRegistry`

That is a meaningful milestone. The agent is no longer just a wrapper around model calls and context state. It also owns a capability surface.

### 12.2 Tool schemas are advertised before the model call

Inside `_agentic_loop()`:

```python
tool_schemas = self.tool_registry.get_schemas()
```

and then:

```python
tools=tool_schemas if tool_schemas else None
```

are passed to `self.client.chat_completion(...)`.

So the `Agent` decides what local capabilities are exposed to the model for that turn.

### 12.3 Tool requests are collected separately from text

During the streamed response loop, the agent still accumulates text deltas into `response_text`. But it now also collects completed tool calls into:

```python
tool_calls: list[ToolCall] = []
```

That means the agent is now bifurcating the model output into two categories:

- conversational text,
- actionable tool requests.

### 12.4 Local execution happens after the provider stream finishes

After the streamed response completes, the agent iterates over collected tool calls and invokes them through the registry.

So the current runtime design is:

- stream first,
- collect completed tool requests,
- execute locally after the model stream ends.

That is a valid early design because it simplifies execution order.

### 12.5 Agent-level tool events are emitted

Before and after each local tool execution, the agent emits:

- `AgentEvent.tool_call_start(...)`
- `AgentEvent.tool_call_complete(...)`

This is important because it separates:

- provider-level tool-call streaming events,
- and agent-level runtime execution events.

The UI mostly cares about the second category because those correspond to actual local work being performed.

### 12.6 Tool results are added back into context

After execution, the agent creates `ToolResultMessage` objects and then writes them into `ContextManager` through `add_tool_result(...)`.

This is one of the most important additions in the entire commit.

The runtime is no longer only storing:

- system messages,
- user messages,
- assistant messages,

but also:

- tool-result messages.

That is a key building block for multi-step agent reasoning.

---

## 13. Context manager expands beyond plain chat history (`core/context/manager.py`)

`ContextManager` has grown in a meaningful way since `03`.

### 13.1 `MessageItem` schema expansion

`MessageItem` now includes:

- `tool_call_id`
- `tool_calls`

in addition to:

- `role`
- `content`
- `token_count`

That means message history is no longer shaped only for ordinary chat turns. It is now beginning to model function/tool protocol messages as well.

### 13.2 Serialization boundary grows richer

`to_dict()` now conditionally includes:

- `tool_call_id`
- `tool_calls`

when present.

So the provider-facing message schema is now richer than a simple `{role, content}` pair.

### 13.3 `add_tool_result(...)`

This new method appends a `role="tool"` message with the tool call id and token count.

That means `ContextManager` is now responsible for preserving action outcomes alongside natural-language turns.

---

## 14. Agent event vocabulary expands (`core/agent/events.py`)

The agent-level event system has also evolved.

### 14.1 New event types

`AgentEventType` now includes:

- `TOOL_CALL_START`
- `TOOL_CALL_COMPLETE`

Notice the distinction:

- `StreamEventType` is about provider/client streaming semantics,
- `AgentEventType` is about application/runtime semantics.

That layering is a good architectural pattern.

### 14.2 Event payload design

`tool_call_complete(...)` includes:

- `call_id`
- `name`
- `success`
- `output`
- `error`
- `metadata`
- `truncated`

So the UI and any future tracing layer receive the full tool outcome, not just a generic success/failure flag.

---

## 15. CLI runtime handling expands (`main.py`)

`main.py` has evolved from a text-only event consumer into a small orchestration-aware renderer.

### 15.1 Previous role

In `03`, the CLI was mostly concerned with:

- streamed assistant text,
- final text completion,
- agent errors.

### 15.2 Current role

Now `CLI._process_message(...)` also handles:

- `AgentEventType.TOOL_CALL_START`
- `AgentEventType.TOOL_CALL_COMPLETE`

and routes them into the `TUI`.

This matters because the terminal runtime is no longer just showing “what the model says.” It is also showing “what the agent is doing.”

---

## 16. Terminal UI becomes tool-aware (`core/ui/tui.py`)

`TUI` has expanded dramatically in this iteration.

### 16.1 New state tracked by the UI

`TUI` now stores:

- `_tool_args_by_call_id`
- `cwd`
- `_max_block_tokens`
- `model_name`

This shows the UI is no longer stateless text rendering. It now remembers tool arguments and uses runtime context to present tool results intelligibly.

### 16.2 Tool start rendering

`tool_call_start(...)` renders a Rich panel showing:

- tool name,
- short call id,
- arguments in an ordered table,
- and a status like `running`.

So users can now observe the agent's action plan in-flight.

### 16.3 Tool completion rendering

`tool_call_complete(...)` renders a second panel showing:

- success/failure state,
- output blocks,
- syntax-highlighted file content for `read_file`,
- truncated notices,
- and explicit error text for failed tool calls.

The runtime now visually distinguishes between:

- assistant prose,
- a tool invocation,
- and the tool's returned artifact.

### 16.4 Why this matters in agent systems

Tool-capable agents need observability. Without action visibility, the user cannot tell:

- what the model asked to do,
- whether the tool ran,
- whether it succeeded,
- what it returned,
- or whether the system is stuck.

The richer TUI is therefore not just presentation polish. It is part of the agent's transparency model.

---

## 17. End-to-end request lifecycle after this commit

A tool-using request now looks like this:

1. the user submits a prompt in `main.py`,
2. `CLI.run_single(...)` opens an `Agent` context,
3. `Agent.run(...)` stores the user message in `ContextManager`,
4. `Agent._agentic_loop()` gathers tool schemas from `ToolRegistry`,
5. `LLMClient.chat_completion(...)` sends both messages and tools to the provider,
6. the provider streams text and/or tool-call chunks,
7. `LLMClient._stream_response(...)` converts those chunks into internal `StreamEvent`s,
8. `Agent` forwards text as `AgentEvent.text_delta(...)`,
9. `Agent` accumulates completed `ToolCall` objects,
10. after the provider stream ends, the agent invokes local tools through `ToolRegistry.invoke(...)`,
11. `AgentEvent.tool_call_start(...)` and `tool_call_complete(...)` are emitted,
12. `TUI` renders those tool panels,
13. tool results are written back into `ContextManager` as `role="tool"` messages.

Compared to `03`, the lifecycle now includes both **language generation** and **runtime action execution**.

---

## 18. Conceptual progression from `03` to `04`

`03` introduced the prompt/context substrate. `04` builds the next layer on top of that substrate: a local capability system.

So the progression now looks like this:

1. **`01`**: client/event fundamentals
2. **`02`**: runtime/event routing and UI shell
3. **`03`**: context management and prompt construction
4. **`04`**: tool schemas, tool execution, and tool-aware runtime rendering

That is a natural agent-architecture sequence.

---

## 19. Important code-level nuances and implications

This section is especially important because the current implementation is meaningful but still transitional.

### 19.1 Tool support is partially closed-loop, not fully closed-loop

The runtime now:

- receives tool requests,
- executes them,
- stores their results in context.

But the same `run(...)` call does **not yet automatically perform a second model completion pass** after inserting tool outputs.

So the current behavior is best understood as:

- **tool execution is implemented**,
- **tool-result persistence is implemented**,
- **automatic post-tool reasoning in the same turn is not fully implemented yet**.

### 19.2 Provider-level tool streaming vs agent-level tool execution are separate layers

The client layer can emit:

- `TOOL_CALL_START`
- `TOOL_CALL_DELTA`
- `TOOL_CALL_COMPLETE`

But the agent currently mainly consumes completed tool calls for execution and then emits its own higher-level agent events.

So:

- low-level events belong to the provider protocol,
- high-level events belong to runtime execution and UI behavior.

### 19.3 `ContextManager.add_assistant_message(...)` hints at future richer assistant messages

`add_assistant_message(...)` accepts `tool_calls: list[dict[str, Any]] | None = None`, but that pathway is not yet deeply integrated into assistant-message serialization.

That suggests the architecture is preparing for assistant messages that explicitly store tool call metadata, but the current implementation is still in transition.

### 19.4 `ToolResultMessage.to_openai_message()` is scaffolding for a fuller loop

This method exists even though the current write-back path uses `ContextManager.add_tool_result(...)` directly.

That tells you the architecture is already thinking about more formal provider-compatible tool result message construction, even if the current integration is not complete.

### 19.5 The tool system is intentionally narrow right now

There is only one builtin tool (`read_file`). That is a good sign, not a weakness.

Early tool runtimes are easier to debug and reason about when they grow from a small, high-value, well-bounded capability set.

---

## 20. Delta summary table (`03` -> current)

| Area | `03` baseline | Current (`04`) delta |
|---|---|---|
| Core runtime identity | Context-aware assistant | Tool-capable agent runtime |
| Tool abstraction | Absent | Added `Tool`, `ToolKind`, `ToolInvocation`, `ToolResult` |
| Tool registration | Absent | Added `ToolRegistry` and builtin discovery |
| Builtin capabilities | None | Added `ReadFileTool` |
| Provider request shape | Messages only | Messages + tool schemas |
| Client streaming contract | Text/error events | Text + tool-call start/delta/complete events |
| Agent orchestration | Stream assistant text only | Collect tool calls and invoke local tools |
| Context state | system/user/assistant messages | system/user/assistant/tool message support |
| UI rendering | Assistant text + error output | Rich tool start/completion panels |
| Path/environment utilities | Minimal | Added path resolution, relative display, binary detection |
| Dependencies | `click`, `openai`, `tiktoken` | Added `pydantic` for typed tool schemas |

---

## 21. Big-picture significance

This commit marks a very important transition in the project.

With `03`, the system had the beginnings of memory and prompt discipline. With `04`, it gains the beginnings of **agency through external actions**.

That does not mean it is already a complete autonomous agent. But it now contains several core building blocks that real agent runtimes need:

- explicit capability advertisement,
- typed tool interfaces,
- execution results as structured runtime objects,
- context support for tool outputs,
- and UI visibility into agent actions.

Those are not superficial additions. They are part of the architecture required for:

- retrieval and file inspection workflows,
- code editing loops,
- tool-augmented reasoning,
- multi-step task execution,
- and eventually more autonomous behavior.

In short, `04` is the point where the system starts crossing from “chat runtime with memory” into “agent runtime with local actions.”

---

## 22. Continuation pointer for next document

Natural next-step topics for `05` would be:

- closing the tool loop by re-calling the model after tool outputs are added to context,
- representing assistant tool-call messages more explicitly in `ContextManager`,
- handling multiple tool rounds inside one agent run,
- adding more builtin tools beyond `read_file`,
- introducing tool confirmation or approval flows,
- and tightening provider-stream/tool-call normalization behavior.

That would complete the transition from:

- **tool-capable runtime**

to:

- **multi-step tool-using agent loop**.

