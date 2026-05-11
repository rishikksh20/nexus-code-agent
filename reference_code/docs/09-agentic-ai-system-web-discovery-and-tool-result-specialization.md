# 09. Agentic AI System Web Discovery and Tool Result Specialization: External Search/Fetch Capabilities and Richer Search-Oriented Runtime Rendering

This document is a continuation of:

- `docs/01-agentic-ai-system-basics.md`
- `docs/02-agentic-ai-system-runtime-and-error-paths.md`
- `docs/03-agentic-ai-system-context-management-and-prompt-construction.md`
- `docs/04-agentic-ai-system-tool-calling-and-execution-runtime.md`
- `docs/05-agentic-ai-system-configuration-and-environmental-context-management.md`
- `docs/06-agentic-ai-system-session-runtime-ownership-and-state-boundaries.md`
- `docs/07-agentic-ai-system-builtin-tool-expansion-and-interactive-tool-observability.md`
- `docs/08-agentic-ai-system-search-and-discovery-tool-expansion.md`

`01` established the client and internal event basics.
`02` introduced the runtime shell (`CLI -> Agent -> TUI`) and agent-level event routing.
`03` added managed context, prompt construction, and token-aware utilities.
`04` introduced the first tool runtime and the initial `read_file` capability.
`05` added configuration loading and deployment-aware runtime bootstrap.
`06` moved client/context/tools under a dedicated `Session` boundary.
`07` expanded the builtin tool surface and made tool executions much more visible in the terminal UI.
`08` added local workspace discovery primitives through `grep` and `glob`.
`09` explains the next incremental step in the current uncommitted changes: the agent now expands discovery beyond the local repository by adding web search and web fetch tools, and the terminal UI now renders search-oriented tool results with more tool-specific structure.

In this stage, the code adds:

- a `web_search` builtin tool for external web discovery,
- a `web_fetch` builtin tool for HTTP content retrieval,
- builtin registration updates so those tools become part of the default agent capability surface,
- dependency updates required for search and HTTP retrieval,
- and specialized `TUI` rendering for `grep`, `glob`, `web_search`, and `web_fetch` results.

---

## 1. High-level change in this iteration

The project shifts from a **workspace-discovery agent** to a **workspace-and-web discovery agent**.

Previous effective flow (from `08`):

`main.py (CLI)` -> `Agent(config)` -> `Session(config)` -> `ToolRegistry(...read/write/edit/list/shell/grep/glob)` -> model can discover files and matching lines inside the repo -> `AgentEvent` -> `TUI`

Current flow (this `09` step):

`main.py (CLI)` -> `Agent(config)` -> `Session(config)` -> `ToolRegistry(...grep/glob/web_search/web_fetch...)` -> model can discover local files, search the web, fetch remote page content, and then continue reasoning with both local and external context -> `AgentEvent` -> `TUI` with search-oriented result summaries

That is the main conceptual shift.

`08` improved how the agent finds things inside the repository.

`09` extends that same discovery idea outward:

- first find signals inside the repo,
- then search externally when local context is insufficient,
- then fetch the actual remote source content when a search hit looks relevant.

That gives the runtime a more realistic research loop rather than a purely local code-navigation loop.

---

## 2. Change scope since the previous commit

### New packages/modules introduced

- `core/tools/builtin/web_search.py`
- `core/tools/builtin/web_fetch.py`

### Existing files updated

- `core/tools/builtin/__init__.py`
- `core/ui/tui.py`
- `pyproject.toml`
- `uv.lock`

### Center of gravity in this change

The center of gravity is the addition of **external discovery and retrieval tools**.

If `08` was about *finding the right files and lines inside the workspace*, then `09` is about *letting the same agent research outside the workspace and presenting those results in a way that is easier to inspect interactively*.

---

## 3. Architectural delta: from local discovery to hybrid local/external discovery

### 3.1 Prior state (`08` baseline)

At the end of `08`, the agent had a stronger discovery surface than before:

- `glob` for path-based discovery,
- `grep` for regex-based content discovery,
- and the rest of the existing workspace tools for reading, editing, writing, listing, and shell execution.

That meant the runtime could do a much better job answering:

- where is the file?
- where is this symbol or text?
- what file should I read next?

But it was still fundamentally bounded by the local repository.

If the agent needed:

- external documentation,
- web results for an API/library,
- or the contents of a relevant page,

the builtin tool surface had no direct way to obtain it.

### 3.2 Current state (`09`)

The uncommitted changes add two network-oriented tools:

- `web_search`
- `web_fetch`

Together, these create a two-step external discovery pattern:

1. search broadly on the web,
2. fetch a chosen URL for actual content.

That mirrors the same separation that already exists in the local workspace:

- `glob`/`grep` discover likely targets,
- `read_file` retrieves detailed local content.

With `09`, the agent now has a corresponding external pattern:

- `web_search` discovers candidate remote sources,
- `web_fetch` retrieves a chosen source.

This is a strong conceptual fit with the architecture that the codebase has been building since `04`: each new capability is introduced as a focused tool, not as special-case logic in the agent loop.

---

## 4. Builtin tool surface expansion (`core/tools/builtin/__init__.py`)

The builtin registration layer now exposes two more tools:

- `WebSearchTool`
- `WebFetchTool`

### 4.1 Why this matters

The important architectural point is not just that imports were added. It is that the default tool surface continues to grow by extending the builtin registry rather than by changing `Agent` orchestration.

That means the same runtime model still applies:

- session builds the default registry,
- the agent advertises schemas from that registry,
- the model chooses tools by name,
- and execution remains generic.

So this change preserves the tool-system design discipline established earlier in the series.

### 4.2 Capability implication

By registering these tools in the builtin surface, web search and fetch become first-class runtime capabilities rather than optional side paths.

That matters because it changes what the model can responsibly attempt during a session:

- not only inspect the current repo,
- but also gather outside reference material when the task needs it.

---

## 5. New builtin tool: `web_search` (`core/tools/builtin/web_search.py`)

`web_search` adds external discovery.

### 5.1 Why this tool exists

Once a coding agent can search the local repository, the next natural need is external lookup:

- package usage examples,
- framework documentation,
- API references,
- recent public information,
- or broad background research.

`web_search` fills that gap.

### 5.2 Input contract

The tool uses a small search-specific schema:

- `query`
- `max_results`

That is a good fit for the rest of the tool system: the contract is narrow, typed, and easy for the model to satisfy.

### 5.3 Execution behavior

The tool currently delegates search to `DDGS().text(...)` and turns the provider output into a model-readable plain-text result that includes:

- a result header for the query,
- numbered results,
- titles,
- URLs,
- and snippets when available.

That means the tool is deliberately not returning raw provider objects. It returns a stable text transcript plus small structured metadata.

This is important because agent loops generally need search results in two forms:

- readable by the model for follow-up reasoning,
- and summarized for the UI so the human can quickly see what happened.

### 5.4 Metadata returned

The tool returns structured metadata with the number of results.

That keeps it consistent with the rest of the tool runtime, where a human-readable output block is paired with small machine-friendly summary facts.

### 5.5 Important behavioral nuance

The current implementation defines `max_results` in the schema, but the execution path does not yet actively use that value to limit the search provider response.

That means `max_results` currently behaves more like an intended contract than a fully enforced runtime bound.

Conceptually, this is still useful to mention because it shows the tool interface is ahead of the current execution detail. The capability is present, but one part of the control surface is not yet fully wired through.

---

## 6. New builtin tool: `web_fetch` (`core/tools/builtin/web_fetch.py`)

`web_fetch` adds direct remote content retrieval.

### 6.1 Why this tool matters

Search results alone are often insufficient for an agent.

A result list can tell the model where information might be, but not provide the actual body of the source. The next step is usually to fetch the page itself.

That is exactly what `web_fetch` does.

### 6.2 Input contract

The tool accepts:

- `url`
- `timeout`

This keeps the tool focused on one responsibility: fetch a page over HTTP(S) and return its content as text.

### 6.3 Execution behavior

The tool currently performs these conceptual steps:

1. validate that the URL uses `http` or `https`,
2. issue the request using `httpx.AsyncClient`,
3. follow redirects,
4. fail clearly on HTTP status errors,
5. return the response text on success,
6. truncate very large responses before returning them.

That is a good first-version design because it sets reasonable boundaries around a network-facing tool:

- transport is constrained,
- timeout exists,
- redirects are handled,
- large output is bounded,
- and error cases are converted into explicit tool failures rather than bubbling raw exceptions through the runtime.

### 6.4 Metadata returned

The result metadata includes:

- `status_code`
- `content_length`

This is useful because the tool output itself is the page body, while the metadata tells the UI and downstream reasoning something about the retrieval event.

That separation is consistent with the rest of the tool runtime design.

### 6.5 Why this is complementary to `web_search`

`web_search` and `web_fetch` are intentionally not the same tool.

That is good architecture.

One tool answers:

- *what remote sources might matter?*

The other answers:

- *what does this specific remote source actually say?*

Keeping those concerns separate makes the agent's decision process easier to understand and keeps each tool simple.

---

## 7. Dependency changes and why they matter

The dependency layer now adds `ddgs`, which brings in supporting packages such as `lxml` and `primp` through the lockfile.

### 7.1 `ddgs`

`ddgs` is the enabling dependency for the new search capability.

This is an architectural step because the tool system is no longer only calling local filesystem or shell primitives. It is now relying on an external search client library to broaden the agent's information-gathering surface.

### 7.2 Why this matters conceptually

The moment a dependency is added for search rather than local execution, the agent framework changes character slightly:

- from a repository operator,
- toward a lightweight research agent.

That does not make it a full web-browsing system, but it does mean the runtime now has an explicit external information path.

---

## 8. `TUI` now specializes search-oriented result rendering (`core/ui/tui.py`)

One of the most important parts of this uncommitted diff is not the new tools themselves, but the way the UI begins to present them.

The `TUI` tool completion rendering now has dedicated branches for:

- `grep`
- `glob`
- `web_search`
- `web_fetch`

### 8.1 Why this matters

Without specialized rendering, all tool results collapse into a generic fallback path.

That works technically, but it hides the semantics of the action.

For search-oriented tools, what the human usually wants to know first is not only the raw output, but also a short summary such as:

- how many matches were found,
- how many files were searched,
- what query was used,
- how many web results came back,
- what URL was fetched,
- what HTTP status came back,
- or how large the response was.

The new `TUI` branches make that information immediately visible.

### 8.2 `grep` rendering

For `grep`, the UI now surfaces summary information such as:

- total matches,
- files searched,

before showing the textual result block.

This makes the tool feel more like a search operation and less like an undifferentiated output dump.

### 8.3 `glob` rendering

For `glob`, the UI now highlights the match count before showing the file list.

That is a small change, but it improves scanability when the agent is discovering files.

### 8.4 `web_search` rendering

For `web_search`, the UI now shows:

- the query,
- the number of results,

before rendering the text block of results.

This is important because the query is part of the semantic meaning of the tool call. Showing the query in the panel makes it much easier to inspect whether the agent searched for the right thing.

### 8.5 `web_fetch` rendering

For `web_fetch`, the UI now shows:

- HTTP status code,
- content length,
- URL,

before rendering the fetched text.

That gives the human operator a much clearer understanding of what happened during remote retrieval.

### 8.6 Bigger implication

This change continues a pattern that started in `07`: the UI is becoming increasingly artifact-aware.

Different tool families now render differently because they produce different kinds of runtime artifacts.

That is a good sign for an agent system, because observability quality depends heavily on preserving those semantic differences rather than flattening everything into plain text.

---

## 9. Why this is a meaningful step even though the agent loop did not change

The `Agent` orchestration path did not need a major rewrite for this feature.

That is exactly why this change matters.

It demonstrates that the architecture is doing its job.

The runtime can gain meaningful new capabilities by:

- implementing new tools,
- registering them,
- and teaching the UI how to render their outputs more clearly,

without changing the core agent loop.

This is one of the clearest signs that the tool architecture is maturing.

The agent runtime is becoming extensible in the right dimension:

- capability growth at the tool layer,
- presentation refinement at the UI layer,
- minimal orchestration churn.

---

## 10. Conceptual progression from `08` to `09`

The progression now looks like this:

1. **`01`**: client/event fundamentals
2. **`02`**: runtime shell and lifecycle events
3. **`03`**: context ownership and prompt construction
4. **`04`**: tool schemas and local tool execution
5. **`05`**: configuration loading and runtime bootstrap
6. **`06`**: session-scoped runtime ownership
7. **`07`**: broader builtin actions and richer tool observability
8. **`08`**: local search and discovery primitives
9. **`09`**: external web discovery and specialized rendering for search/fetch results

That is a coherent progression.

After teaching the agent how to inspect the workspace and search the repository, the next logical step is to let it gather outside context as well.

---

## 11. Big-picture significance

This uncommitted change is small in file count, but important in what it signals about the system.

The runtime is no longer only a coding agent that operates on what is already present locally. It is starting to become a coding-and-research agent that can:

- search the repo,
- search the web,
- fetch remote sources,
- and show those actions clearly in the terminal.

That is a meaningful shift in practical usefulness.

Many real tasks require a mix of:

- local code inspection,
- external reference lookup,
- and follow-up edits or decisions.

`09` gives the runtime a first-class external information path for those workflows.

---

## 12. Important code-level nuances and implications

### 12.1 `web_search` exposes a bounded contract but does not fully enforce `max_results` yet

The schema advertises `max_results`, but the current execution path does not yet use that value to trim or request a bounded result count.

So the contract is partially ahead of the implementation detail.

### 12.2 `web_fetch` returns raw text rather than extracted semantic content

The current tool returns response body text directly. It does not yet attempt page-to-markdown conversion, readability extraction, or structured parsing.

That keeps the tool simple, but also means the agent may receive noisy page text depending on the source.

### 12.3 UI specialization now covers both old and new discovery tools

Even though `grep` and `glob` were introduced earlier, this uncommitted diff adds specific `TUI` rendering branches for them alongside the new web tools.

So part of the real delta here is not just capability growth, but better observability for the broader search/discovery family as a whole.

### 12.4 The capability surface expands without changing the session/agent contract

No new agent lifecycle machinery was needed. That shows the session-owned registry and generic tool execution model are already flexible enough to absorb more tools without architectural churn.

---

## 13. Delta summary table (`08` -> current uncommitted state)

| Area | `08` baseline | Current uncommitted delta |
|---|---|---|
| Discovery scope | Local workspace only | Local workspace plus external web |
| New builtin tools | `grep`, `glob` | `web_search`, `web_fetch` |
| Search style | Path and regex search in repo | Repo search plus external web search |
| Retrieval style | `read_file` for local content | `read_file` for local content plus `web_fetch` for remote content |
| Builtin registration | Local tools registered | Web tools added to default registry |
| Dependencies | Local/runtime tool dependencies only | Adds `ddgs`-based web search support |
| Tool result rendering | Some generic fallback for search-like outputs | Dedicated `TUI` branches for `grep`, `glob`, `web_search`, `web_fetch` |
| Operator visibility | Search results visible mainly as plain output | Search/fetch panels now show structured summaries |

---

## 14. Natural continuation points for a future `10`

Natural next-step topics after this change would be:

- wiring `max_results` fully into `web_search`,
- adding HTML-to-markdown or readability extraction for `web_fetch`,
- introducing domain allowlists or approval rules for network tools,
- caching remote fetch/search results per session,
- or distinguishing network-tool rendering even further in the UI.

That would complete the transition from:

- **external lookup exists**

to:

- **external lookup is policy-aware, bounded, and easier to consume automatically**.

---

## 15. Key takeaways

1. The main uncommitted architectural change is the addition of external discovery and retrieval through `web_search` and `web_fetch`.

2. The runtime now supports a more realistic research loop: local search, external search, remote fetch, then continued reasoning.

3. The builtin registration layer continues to be the main expansion point for capability growth, which keeps the agent loop stable.

4. The `TUI` now treats search-oriented tools as distinct result types, which improves operator visibility and trust.

5. The codebase is moving from a local coding agent toward a hybrid coding-and-research agent, while preserving the same session-owned tool architecture.