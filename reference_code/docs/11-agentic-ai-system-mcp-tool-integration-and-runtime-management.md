# 11. Agentic AI System MCP Tool Integration and Runtime Management: Session-Scoped Model Context Protocol Clients, Dynamic External Tool Registration, and Generic Interactive Tool Observability

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

`01` established the client, streaming event, and basic runtime flow.
`02` introduced the CLI/agent/TUI loop and agent-side event routing.
`03` added context management and prompt construction.
`04` introduced the first structured tool runtime.
`05` added configuration loading and environment-aware bootstrap.
`06` moved runtime ownership under `Session`.
`07` expanded the builtin local tool surface and made tool runs much more visible in the interactive UI.
`08` added stronger repository search/discovery primitives.
`09` expanded discovery outward through web-oriented tools and richer result rendering.
`10` added convention-based custom tool discovery from `.ai-agent/tools` so the runtime could be extended without editing core builtin modules.
`11` explains the next step in that extension story: the runtime now adds **MCP-backed external tools** and a dedicated management layer that connects to Model Context Protocol servers, discovers their remote tool surfaces, and registers those tools into the existing agent runtime.

In this stage, the code adds:

- MCP server configuration through `Config.mcp_servers`,
- a concrete MCP client wrapper in `core/tools/mcp/client.py`,
- a session-scoped manager in `core/tools/mcp/mcp_manager.py`,
- a `Tool` adapter for remote MCP tools in `core/tools/mcp/mcp_tool.py`,
- registry support for dynamically registered MCP tools,
- session startup/shutdown integration for MCP lifecycle management,
- and dependency support through `fastmcp`.

This document serves two purposes:

1. explain the MCP concept and why it matters architecturally, and
2. show exactly how MCP is implemented in this repository, including important nuances and current limitations.

---

## 1. High-level change in this iteration

The project shifts from a **locally extensible tool runtime** to a **locally extensible + externally connected tool runtime**.

Previous effective flow (from `10`):

`main.py (CLI)` -> `Agent(config)` -> `Session(config)` -> builtin tools + discovered local custom tools from `.ai-agent/tools` -> model uses one in-process tool surface -> `AgentEvent` -> `TUI`

Current intended flow (this `11` step):

`main.py (CLI)` -> `Agent(config)` -> `Session(config)` -> builtin tools + discovered local custom tools + MCP server manager -> connect to configured MCP servers -> discover remote server tools -> wrap them as local `Tool` objects -> register them into `ToolRegistry` -> expose them to the model as namespaced tools -> render them through the existing tool event/UI flow

That is the main conceptual shift.

`10` gave the runtime a plugin model for **local Python-defined tools**.
`11` extends the same idea to **external tool servers**.

So this change is not only “more tools.”
It is a change in *where tools can live*:

- before: inside the repo or user config directory,
- now: also behind an MCP transport boundary.

That makes the agent runtime more modular and more realistic for advanced tool ecosystems.

---

## 2. What MCP means in this repository

At a conceptual level, Model Context Protocol (MCP) allows the agent runtime to talk to external tool servers using a standard transport and tool-discovery model.

In this repository, that idea is implemented in a simple layered way:

1. `Config` describes one or more MCP servers.
2. `MCPClient` knows how to connect to one configured server.
3. `MCPManager` owns many `MCPClient` instances for the current session.
4. `MCPManager` discovers the tools exposed by each connected server.
5. Each discovered remote tool is wrapped in an `MCPTool`, which implements the repo’s existing `Tool` interface.
6. Those `MCPTool` instances are registered into `ToolRegistry`.
7. The rest of the agent loop treats them like normal tools.

That last point is the most important architectural property.

The agent loop does **not** get a separate MCP-only execution path.
Instead, MCP is adapted into the same tool abstraction already used by builtin and discovered custom tools.

That keeps the runtime coherent.

---

## 3. Change scope since the previous commit (`36c5eb7` baseline)

### New package/modules introduced

- `core/tools/mcp/__init__.py`
- `core/tools/mcp/client.py`
- `core/tools/mcp/mcp_manager.py`
- `core/tools/mcp/mcp_tool.py`

### Existing files updated

- `core/agent/agent.py`
- `core/agent/session.py`
- `core/tools/registry.py`
- `pyproject.toml`
- `uv.lock`

### Center of gravity in this change

The center of gravity is the introduction of a **session-owned MCP lifecycle** and an **adapter layer that turns remote MCP tools into normal agent tools**.

If `10` was about *discovering new local tool definitions*, then `11` is about *connecting to external tool providers and projecting their capabilities into the same runtime surface*.

---

## 4. Configuration model: MCP becomes a first-class part of `Config`

The foundation for this work already exists in `core/config/config.py` through `MCPServerConfig` and `Config.mcp_servers`.

### 4.1 `MCPServerConfig`

`MCPServerConfig` supports two transport styles:

- **stdio** transport
  - `command`
  - `args`
  - `env`
  - `cwd`
- **HTTP/SSE** transport
  - `url`

It also includes:

- `enabled`
- `startup_timeout_sec`

The `@model_validator` enforces an important boundary:

- each server must specify **either** `command` **or** `url`,
- but not both.

That is an important design choice because it keeps transport selection explicit and unambiguous before the runtime even reaches the MCP connection layer.

### 4.2 Why this matters architecturally

This means MCP is not bolted on as an ad-hoc runtime toggle.
It is part of the formal configuration surface, which keeps it aligned with earlier docs in the series:

- `05` introduced configuration as a runtime control plane,
- `06` made `Session` the owner of runtime state,
- `10` allowed local tool discovery from configured runtime roots,
- `11` extends that same runtime-control idea to external protocol-backed tool servers.

### 4.3 Example configuration shapes

The following examples are consistent with the current config model in this repo.

#### Example: stdio MCP server

```toml
[mcp_servers.docs]
enabled = true
command = "python"
args = ["-m", "my_docs_mcp_server"]
startup_timeout_sec = 10
```

#### Example: SSE MCP server

```toml
[mcp_servers.search]
enabled = true
url = "http://localhost:8000/sse"
startup_timeout_sec = 15
```

With a config like this, the session will try to create one MCP client per configured server entry.

---

## 5. New low-level transport client: `core/tools/mcp/client.py`

`MCPClient` is the transport-facing wrapper for a single configured MCP server.

### 5.1 Supporting data structures

The file introduces two small but important models:

- `MCPServerStatus`
  - `DISCONNECTED`
  - `CONNECTING`
  - `CONNECTED`
  - `ERROR`
- `MCPToolInfo`
  - `name`
  - `description`
  - `input_schema`
  - `server_name`

These structures separate:

- connection state,
- discovered remote tool metadata,
- and the eventual local tool adapter layer.

That separation keeps the client focused on remote protocol concerns rather than agent-facing tool execution behavior.

### 5.2 Transport creation

`MCPClient._create_transport()` chooses between:

- `StdioTransport` when `config.command` is present,
- `SSETransport` otherwise.

For stdio transport, it:

1. copies the host environment,
2. overlays `config.env`,
3. uses either `config.cwd` or the session cwd,
4. and points logs at `os.devnull`.

That is a pragmatic implementation.

It means the repo treats stdio MCP servers similarly to subprocess-backed shell tools, but with a dedicated MCP protocol client rather than raw command execution.

### 5.3 Connect path

`connect()` performs four key actions:

1. guards against reconnecting an already-connected client,
2. marks the status as `CONNECTING`,
3. opens the underlying `fastmcp.Client` context,
4. calls `list_tools()` and stores the discovered tools in `_tools`.

For each remote tool, the client records an `MCPToolInfo` object that preserves:

- the remote tool name,
- its human description,
- its input schema,
- and the MCP server identity it came from.

That is the raw material later used to synthesize local `Tool` objects.

### 5.4 Remote tool invocation

`call_tool(tool_name, arguments)` delegates directly to `fastmcp.Client.call_tool(...)` and then flattens the returned content into plain text.

The method returns a normalized dictionary:

- `output`
- `is_error`

That normalization is small but important.
It means the rest of the repository does not need to know the exact `fastmcp` response shape.

### 5.5 Why this client layer matters

The runtime could have called `fastmcp` directly from the tool adapter.
That would have worked, but it would have mixed concerns.

Instead, this repo now has a cleaner three-step chain:

- transport client (`MCPClient`),
- manager (`MCPManager`),
- tool adapter (`MCPTool`).

That is a more maintainable architecture.

---

## 6. New session-level coordinator: `core/tools/mcp/mcp_manager.py`

`MCPManager` is the central coordinator for MCP servers within one agent session.

### 6.1 What `MCPManager` owns

It owns:

- the active `Config`,
- a mapping of server name -> `MCPClient`,
- an `_initialized` flag,
- connection startup,
- tool registration,
- shutdown,
- and server-status inspection.

This is exactly the kind of responsibility that fits naturally under the `Session` abstraction introduced in `06`.

### 6.2 `initialize()`

`initialize()` is intended to:

1. exit early if already initialized,
2. read `config.mcp_servers`,
3. create one `MCPClient` per enabled server,
4. connect them concurrently via `asyncio.gather(...)`,
5. and then mark the manager as initialized.

The startup path uses `asyncio.wait_for(...)` per client with `startup_timeout_sec`, which is a good runtime-safety detail.

It prevents session startup from hanging forever on one slow or dead server.

### 6.3 `register_tools(...)`

This is the bridge from remote server state to local agent tool state.

For every connected client, `register_tools(...)`:

1. iterates through `client.tools`,
2. creates an `MCPTool` wrapper for each one,
3. prefixes its local name as `"{server_name}__{tool_name}"`,
4. registers it into the registry using `register_mcp_tool(...)`.

The namespacing choice is especially important.

It avoids collisions between:

- builtin tools,
- locally discovered custom tools,
- and remote tools from different MCP servers.

So if a server named `docs` exposes a remote `search` tool, the agent-visible tool name becomes:

- `docs__search`

That is a strong and developer-friendly design choice.

### 6.4 `shutdown()` and lifecycle symmetry

`shutdown()` disconnects all clients concurrently, clears the client map, and resets `_initialized`.

That gives the MCP layer a lifecycle symmetry:

- startup in session initialization,
- cleanup in session exit.

That is exactly what you want for external resource management.

### 6.5 `get_all_servers()`

The manager also exposes lightweight observability data:

- server name,
- status,
- tool count.

Even though the current CLI does not yet surface this in a dedicated MCP status view, the method is a useful starting point for future operator-facing diagnostics.

---

## 7. New tool adapter: `core/tools/mcp/mcp_tool.py`

`MCPTool` is the class that makes a remote MCP tool look like a normal local `Tool`.

This is the most important abstraction boundary in the entire change set.

### 7.1 Constructor role

`MCPTool` stores:

- the active `Config`,
- the owning `MCPClient`,
- the discovered `MCPToolInfo`,
- and the final locally registered tool name.

It also copies the remote tool description into the local tool description surface.

### 7.2 Schema adaptation

`MCPTool.schema` is a property that converts the discovered MCP input schema into the repo’s expected tool-schema shape:

- `type: object`
- `properties`
- `required`

This is a key part of the adapter design.

The MCP server speaks in terms of remotely discovered schema.
The local agent runtime needs something that can be turned into the model’s tool schema surface.
`MCPTool.schema` is the bridge.

### 7.3 Tool kind

`MCPTool.kind = ToolKind.MCP`

That matters for two reasons:

1. it gives MCP tools a distinct semantic category inside the tool system,
2. and it lets the UI style them via the existing `tool.mcp` theme entry.

So even though MCP tools are routed through the normal tool pipeline, they still carry a distinct identity.

### 7.4 Execution path

`execute(...)` delegates to `self._client.call_tool(...)`, then maps the result into a normal `ToolResult`:

- MCP error -> `ToolResult.error_result(...)`
- MCP success -> `ToolResult.success_result(...)`

This means the rest of the agent loop does not need any special MCP branching.

The agent still only understands:

- tool start,
- tool complete,
- success vs failure,
- output text,
- metadata.

That is exactly the point of the adapter.

---

## 8. Registry changes: MCP becomes a second dynamic tool bucket

`core/tools/registry.py` now grows beyond a single `_tools` mapping.

### 8.1 Separate MCP storage

The registry now stores:

- `_tools` for builtin/custom/subagent tools,
- `_mcp_tools` for connected MCP tools.

That is a subtle but useful distinction.

It preserves the ability to treat MCP tools as part of the overall surface while still remembering that they are different in origin and lifecycle.

### 8.2 Lookup behavior

`get(name)` now checks `_tools` first and `_mcp_tools` second.

So invocation remains transparent to callers.

The agent loop still just asks the registry for a tool by name.

### 8.3 Registration behavior

MCP tools are added through `register_mcp_tool(...)` rather than `register(...)`.

That makes the lifecycle clearer:

- builtin/custom tool registration remains a core/runtime bootstrap concern,
- MCP registration becomes an external-capability synchronization concern.

### 8.4 Schema/export integration

`get_tools()` now merges normal tools and MCP tools before applying any `allowed_tools` filtering.

That is an important detail.

It means MCP tools are meant to participate in the same capability filtering model as local tools.

So once registered, they are conceptually first-class.

### 8.5 Why this matters

This design keeps the registry as the single capability gateway for the model.

No separate “MCP registry” is exposed to the agent loop.
The registry remains the one place where the tool surface is assembled.

---

## 9. Session integration: MCP becomes part of startup ownership

`core/agent/session.py` is where the earlier architectural work from `06` pays off.

### 9.1 New session-owned field

`Session` now owns:

- `self.mcp_manager: MCPManager`

That is the right place for it.

MCP connections are:

- configuration-dependent,
- lifecycle-bound,
- and not global.

So they belong to the session, not to the CLI and not to the tool registry itself.

### 9.2 Initialization path

A new async `Session.initialize()` method is introduced.

Its intended order is:

1. initialize MCP connections,
2. register discovered MCP tools into the registry,
3. run local custom tool discovery,
4. build `ContextManager` with the final tool list.

Architecturally, that order is good.

It is better than the earlier timing issue described in `docs/10-...`, because the context manager is now constructed *after* tool-surface expansion rather than before it.

So the intent here is that prompt construction sees:

- builtin tools,
- discovered local custom tools,
- and registered MCP tools.

### 9.3 Why this is a good continuation from `10`

`10` introduced runtime extension through local discovery roots.
`11` builds on that by layering MCP in before context initialization.

That means the project is moving toward a broader principle:

> build the complete per-session tool surface first, then hand that surface to prompt/context construction.

That is the correct architectural direction.

---

## 10. Agent lifecycle integration: startup and teardown now include MCP

`core/agent/agent.py` changes in two notable ways.

### 10.1 `__aenter__()` now initializes the session

`Agent.__aenter__()` now calls:

- `await self.session.initialize()`

This is a meaningful change because MCP cannot be treated as a passive field.
It requires startup work.

So `Agent` now explicitly acknowledges that entering a session means more than constructing Python objects.
It means preparing external tool connectivity.

### 10.2 `__aexit__()` now shuts MCP down

`Agent.__aexit__()` now also calls:

- `await self.session.mcp_manager.shutdown()`

This is important resource hygiene.

Without this, stdio processes or long-lived SSE connections could leak beyond the life of the interactive session.

So `11` strengthens the repo’s lifecycle symmetry:

- enter session -> initialize external tool surface,
- exit session -> tear external tool surface down.

---

## 11. How MCP tools participate in the interactive UI

There are no new `TUI` changes in this specific diff, but MCP tools still fit into the already-built interactive tool display pipeline.

### 11.1 Tool start / complete flow is unchanged

Once an MCP tool is in the registry, it flows through the same runtime path as any other tool:

1. model emits a tool call,
2. `Agent` yields `TOOL_CALL_START`,
3. registry invokes the tool,
4. `Agent` yields `TOOL_CALL_COMPLETE`,
5. `main.py` looks up the tool kind from the session registry,
6. `TUI` renders start and completion panels.

That is an important architectural success.

MCP did not require a parallel event system.

### 11.2 MCP-specific visual identity

`core/ui/tui.py` already defines:

- `"tool.mcp": "bright_cyan"`

So MCP tool panels can already have a distinct border style when `tool.kind.value == "mcp"` is passed through from `main.py`.

### 11.3 Result rendering behavior

Because `TUI.tool_call_complete(...)` does not yet have a dedicated `elif name == ...` path for MCP tools, MCP tool output currently goes through the **generic fallback renderer**:

- errors are shown as error text,
- output is shown in a monokai syntax block,
- truncation notes still work,
- start/complete panels still work.

So MCP is already integrated into the repo’s interactive observability model, just not yet with MCP-specific summaries or richer structured rendering.

### 11.4 CLI visibility

The `/tools` command in `main.py` uses `self.agent.session.tool_registry.get_tools()`, which now includes MCP tools.

That means once registration is working, MCP tools should appear in the same operator-facing tool inventory list as builtin and discovered local tools.

---

## 12. End-to-end intended lifecycle for an MCP tool in this repo

A complete intended runtime path looks like this:

1. user config provides one or more entries under `mcp_servers`,
2. `Agent.__aenter__()` awaits `Session.initialize()`,
3. `Session.initialize()` asks `MCPManager` to connect to configured servers,
4. each `MCPClient` creates either a stdio or SSE transport,
5. each connected client calls `list_tools()` and caches remote tool metadata,
6. `MCPManager.register_tools(...)` wraps each discovered remote tool in an `MCPTool`,
7. those `MCPTool` instances are inserted into `ToolRegistry` under names like `server__tool`,
8. `ContextManager` is built using the now-expanded tool list,
9. the model can choose a namespaced MCP tool in the same way it chooses builtin tools,
10. registry invokes the `MCPTool`,
11. `MCPTool` delegates to `MCPClient.call_tool(...)`,
12. the returned content is normalized into a `ToolResult`,
13. the agent emits normal tool completion events,
14. `TUI` renders the result through the existing interactive tool pipeline,
15. session exit disconnects every MCP client.

This is the intended architectural picture, and it is a strong one.

---

## 13. Example of how this feels from the agent’s perspective

Suppose a configured MCP server named `docs` exposes two remote tools:

- `search`
- `fetch_page`

After registration, the local runtime tool surface would include:

- `docs__search`
- `docs__fetch_page`

From the model’s perspective, those are just tool names with schemas.

From the runtime’s perspective, those calls are routed like this:

- `docs__search` -> `MCPTool.execute(...)` -> `MCPClient.call_tool("search", arguments)`
- `docs__fetch_page` -> `MCPTool.execute(...)` -> `MCPClient.call_tool("fetch_page", arguments)`

From the UI’s perspective, they appear like any other tool run, but with the MCP tool kind and generic output rendering.

This is a good example of the adapter pattern working end-to-end.

---

## 14. Important nuances and current limitations

This section is especially important for this iteration.
The overall design is solid, but the current implementation still has a few meaningful gaps.

### 14.1 `Session.initialize()` currently calls the MCP bootstrap coroutine without `await`

In `core/agent/session.py`, the code currently does:

- `self.mcp_manager.initialize()`

inside an async function, but without awaiting it.

That means the intended initialization path is not actually executed.

Practical effect in the current code:

- `_initialized` remains `False`,
- `_clients` stays empty,
- `register_tools(...)` registers zero MCP tools,
- and Python emits a runtime warning that the coroutine was never awaited.

This is not just a style issue. It blocks the intended MCP startup flow.

### 14.2 Dict-backed schema export is not yet fully wired for MCP tools

`MCPTool.schema` correctly returns a dictionary-based schema shape.
However, the shared `Tool.to_openai_schema()` path in `core/tools/base.py` currently constructs a result for dict-backed schemas but never returns it before falling through to `ValueError`.

So if MCP tools do reach schema export, the current behavior is effectively:

- `ValueError: Invalid schema type for tool ...`

That means MCP tool registration and MCP tool schema exposure are conceptually aligned, but one shared serializer path still needs completion.

### 14.3 MCP output is flattened to text

`MCPClient.call_tool(...)` currently extracts `.text` when available and otherwise stringifies content items.

That is a sensible early implementation, but it means richer structured MCP responses are currently collapsed into plain text.

So the repo gets compatibility first, but not full fidelity yet.

### 14.4 Connection failures are mostly suppressed into status state

`MCPManager.initialize()` uses `asyncio.gather(..., return_exceptions=True)`.

That keeps startup resilient, which is good, but it also means failed connections are not loudly surfaced during initialization.
Instead, their effect is mostly visible indirectly through:

- missing registered tools,
- client status values,
- or future inspection through `get_all_servers()`.

This is a reasonable early tradeoff, but it reduces immediate operator visibility.

### 14.5 MCP tools are conservatively treated as mutating

`MCPTool.is_mutating(...)` returns `True` unconditionally.

That is a conservative and defensible default because the runtime cannot assume the semantics of a remote tool.
But it also means MCP tools are treated as potentially effectful even when the remote tool is effectively read-only.

### 14.6 UI support is integrated but generic

The good news:

- MCP tools already travel through the standard tool event pipeline,
- and they already have a dedicated color/style category through `ToolKind.MCP`.

The current limitation:

- there is no MCP-specific completion renderer in `TUI` yet,
- so MCP results do not get summary extraction the way `shell`, `grep`, `glob`, `web_search`, or `web_fetch` do.

That is fine for now, but it is a likely next refinement.

---

## 15. Why this change matters in the series

This change is significant because it broadens the project’s tool model in a deeper way than previous builtin additions.

Earlier expansions mostly answered the question:

> what more can the local process do itself?

This iteration adds a new question:

> what other tool systems can the local process connect to and expose?

That is a much more general capability.

It means the project is moving from:

- a single-process agent with builtin and local plugin tools,

toward:

- a session-managed orchestration runtime that can project external protocol-based capabilities into one unified tool surface.

That is a major architectural milestone, even if the current implementation still has a couple of startup/serialization gaps.

---

## 16. Delta summary table (`10` -> current uncommitted state)

| Area | `10` baseline | Current uncommitted delta (`11`) |
|---|---|---|
| Extension model | Local custom tool discovery from `.ai-agent/tools` | Adds external MCP server-backed tool discovery and registration |
| Tool source location | Local Python modules | Local Python modules + remote MCP servers |
| New runtime package | None for MCP | Adds `core/tools/mcp/` package |
| Session lifecycle | Session owns client/context/registry/discovery | Session additionally owns `MCPManager` |
| Startup behavior | Build registry + discover local tools | Intended to connect MCP servers, register remote tools, then build context |
| Registry structure | One main dynamic tool pool | Adds a dedicated `_mcp_tools` pool merged into the visible tool surface |
| Tool naming | Builtin/custom names | Namespaced remote tool names like `server__tool` |
| External dependency | No MCP client library | Adds `fastmcp` |
| UI integration | Existing generic tool display + specialized builtin renderers | MCP tools reuse the same tool event/UI path with MCP kind styling |
| Main implementation gap | Prompt timing nuance for discovery | Unawaited MCP initialization and incomplete dict-schema export path |

---

## 17. Natural continuation points for a future `12`

Natural next steps after this iteration would be:

- fixing the `await` gap in `Session.initialize()` so MCP bootstrap actually runs,
- completing dict-backed tool schema export so `MCPTool` can be advertised to the model cleanly,
- adding CLI visibility for MCP server status via `MCPManager.get_all_servers()`,
- adding MCP-specific `TUI` rendering for remote-tool summaries and server attribution,
- preserving structured MCP content rather than flattening everything to plain text,
- and introducing stronger approval/policy rules for remote tools.

That would continue the transition from:

- **protocol-level MCP integration scaffolding**

into:

- **fully operational and observable remote tool orchestration**.

---

## 18. Key takeaways

1. The main delta since `docs/10-...` is the addition of MCP-backed external tool integration and a session-scoped manager for their lifecycle.
2. `MCPClient`, `MCPManager`, and `MCPTool` form a clean three-layer adapter stack from remote server -> discovered metadata -> local tool abstraction.
3. `ToolRegistry` now supports a dedicated MCP tool bucket while still presenting one unified tool surface to the rest of the agent runtime.
4. `Session` and `Agent` now treat MCP connectivity as part of startup/teardown ownership, which is the correct continuation of the session architecture introduced earlier in the series.
5. MCP tools already fit into the existing interactive tool display pipeline, even though they currently use generic result rendering rather than MCP-specific summaries.
6. The implementation is architecturally strong, but two current gaps matter: MCP initialization is not yet awaited in `Session.initialize()`, and dict-backed schema export is not yet fully returned by the shared schema serializer.
7. This iteration is the point where the repo moves from “extensible with local tools” toward “extensible with external protocol-backed tool ecosystems.”

