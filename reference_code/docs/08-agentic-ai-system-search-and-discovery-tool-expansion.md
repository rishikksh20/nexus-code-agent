# 08. Agentic AI System Search and Discovery Tool Expansion: Grep/Glob Workspace Navigation and Tool Result Presentation Refinement

This document is a continuation of:

- `docs/01-agentic-ai-system-basics.md`
- `docs/02-agentic-ai-system-runtime-and-error-paths.md`
- `docs/03-agentic-ai-system-context-management-and-prompt-construction.md`
- `docs/04-agentic-ai-system-tool-calling-and-execution-runtime.md`
- `docs/05-agentic-ai-system-configuration-and-environmental-context-management.md`
- `docs/06-agentic-ai-system-session-runtime-ownership-and-state-boundaries.md`
- `docs/07-agentic-ai-system-builtin-tool-expansion-and-interactive-tool-observability.md`

`01` established the client and internal event basics.
`02` introduced the runtime shell (`CLI -> Agent -> TUI`) and agent-level event routing.
`03` added managed context, prompt construction, and token-aware utilities.
`04` introduced the first tool runtime and the initial `read_file` capability.
`05` added configuration loading and deployment-aware runtime bootstrap.
`06` moved client/context/tools under a dedicated `Session` boundary.
`07` expanded the builtin tool surface with write/edit/list/shell capabilities and made tool execution much more visible in the interactive UI.
`08` explains the next incremental step in the current uncommitted changes: after giving the agent ways to read, write, edit, and execute commands, the system now adds better **search and discovery primitives** (`grep`, `glob`) so the agent can find the right files and lines before acting, while also tightening one subtle tool-result presentation path in the terminal UI.

In this stage, the code adds:

- a new `grep` builtin tool for regex-based content search,
- a new `glob` builtin tool for filename/path pattern discovery,
- builtin tool registration updates so these tools become part of the default capability surface,
- and a small but meaningful `TUI` refinement so successful tool results do not incorrectly render error text in the fallback path.

---

## 1. High-level change in this iteration

The project shifts from a **workspace-capable coding agent that can act on known files** to a **workspace-capable coding agent that can also discover files and symbols more intelligently before acting**.

Previous effective flow (from `07`):

`main.py (CLI)` -> `Agent(config)` -> `Session(config)` -> `ToolRegistry(read/write/edit/list/shell)` -> model can inspect, mutate, and execute -> `AgentEvent` -> `TUI`

Current flow (this `08` step):

`main.py (CLI)` -> `Agent(config)` -> `Session(config)` -> `ToolRegistry(read/write/edit/list/shell/grep/glob)` -> model can first discover candidate files and matching lines -> then read/edit/execute with better target selection -> `AgentEvent` -> `TUI` with slightly cleaner fallback tool-result rendering

That is the main conceptual shift.

`07` made the agent more operational. `08` makes it more exploratory.

That distinction matters because a coding agent often needs two separate abilities:

1. **perform actions** on the workspace,
2. **find where those actions should happen**.

Without search/discovery tools, the model must either:

- guess file names,
- rely on user-provided paths,
- or repeatedly use broader tools like `list_dir` and `read_file` to narrow down manually.

With `grep` and `glob`, the system takes a meaningful step toward a more natural coding workflow:

- discover files by path pattern,
- search inside files by content pattern,
- then move into reading, editing, writing, or executing commands with better context.

---

## 2. Change scope since the previous commit

The current uncommitted diff is against commit `2018d1f` (`:art: Expand builtin toolset with write, edit, list, and shell capabilities; enhance UI for tool result rendering`), which corresponds to the state documented in `07`.

### New packages/modules introduced

- `core/tools/builtin/grep.py`
- `core/tools/builtin/glob.py`

### Existing files updated

- `core/tools/builtin/__init__.py`
- `core/ui/tui.py`

### Center of gravity in this change

The center of gravity is the addition of **search/discovery tools**.

If `07` was about *giving the agent more ways to act*, then `08` is about *giving the agent better ways to locate what to act on*.

The UI change in this diff is smaller, but still important because it keeps tool-result rendering semantically cleaner.

---

## 3. Architectural delta: from action-oriented tooling to discovery-oriented tooling

### 3.1 Prior state (`07` baseline)

At the end of `07`, the builtin tool surface already covered several important workspace operations:

- `read_file`
- `write_file`
- `edit`
- `list_dir`
- `shell`

That was a strong operational baseline.

But discovery still had a gap.

The runtime could:

- list directories,
- read known files,
- edit known files,
- write known files,
- and run commands.

What it could not yet do directly was:

- search for files by wildcard/path pattern,
- or search file contents by regex across a directory tree.

That meant the agent had action power, but only limited builtin search power.

### 3.2 Current state (`08`)

The current changes add two high-value read tools:

- `glob`
- `grep`

This widens the capability surface in a very specific and useful way.

The agent can now approach a task in a more realistic programming sequence:

1. discover likely files by path or extension,
2. search for candidate symbols or text patterns,
3. read the most relevant file regions,
4. perform edits or writes,
5. optionally verify through shell commands.

That turns the tool bundle from mostly:

- **inspect / change / verify**

into:

- **discover / inspect / change / verify**.

That is a meaningful architectural improvement even though the diff is smaller than `07`.

---

## 4. Builtin registry expansion (`core/tools/builtin/__init__.py`)

The builtin registration layer now exposes two new tools:

- `GrepTool`
- `GlobTool`

### 4.1 Why this file matters

`core/tools/builtin/__init__.py` is the small but important switchboard that defines the runtime's default local tool surface.

By adding imports, `__all__` entries, and `get_all_builtin_tools()` registrations for `GrepTool` and `GlobTool`, the code makes those tools part of the default session-owned registry without requiring any special-case agent logic.

That is exactly how the earlier tool architecture was intended to scale.

The agent loop does not need to know about each tool by name. It only needs a registry that can:

- advertise schemas,
- map names to tool instances,
- and invoke them generically.

`08` therefore extends the system in the same architectural style established in `04` and expanded in `07`: new capabilities are introduced by implementing tools and registering them, not by rewriting the agent loop.

### 4.2 What this implies conceptually

This registration update reinforces an important design principle in the codebase:

- **capability growth should mostly happen at the tool layer, not the orchestration layer**.

That is a healthy direction for maintainability.

---

## 5. New builtin tool: `glob` (`core/tools/builtin/glob.py`)

`GlobTool` gives the agent a filename/path discovery primitive.

### 5.1 Why `glob` is a useful addition

A coding agent frequently needs to answer questions like:

- which files match `**/*.py`?
- where are the tests?
- which config files exist under a project subtree?
- what files in this repo match a naming convention?

`list_dir` can help with local directory inspection, but it is not ideal for flexible recursive pattern matching.

`glob` fills that gap.

### 5.2 Input contract

`GlobParams` defines:

- `pattern`
- `path`

This is intentionally small and appropriate.

The design keeps the tool focused on one job:

- search within a directory root using a glob expression.

### 5.3 Execution flow

`GlobTool.execute(...)` currently performs this sequence:

1. parse parameters into `GlobParams`,
2. resolve the search root relative to the invocation cwd,
3. ensure the path exists and is a directory,
4. call `search_path.glob(params.pattern)`,
5. filter results down to files only,
6. convert paths to cwd-relative display form when possible,
7. limit displayed results to 1000,
8. return both formatted output and structured metadata.

This is a useful and appropriately bounded first version.

### 5.4 Metadata returned

The tool returns metadata including:

- `path`
- `matches`

This continues the existing tool-result pattern used elsewhere in the project: the UI and later consumers get both text output and structured summary context.

### 5.5 Code nuance: unused helper-style residue

The file also contains a `_find_files(...)` helper that walks the directory tree, filters common ignored folders, skips hidden files, and avoids binary files.

In the current implementation, `execute(...)` does not call that helper. Instead, it uses `Path.glob(...)` directly.

That is noteworthy for documentation because it signals one of two things:

- the tool may have evolved from an earlier directory-walk design,
- or the helper is preparatory scaffolding for future filtering or behavior tightening.

Either way, the current active behavior is the direct `glob(...)` path, not the helper path.

### 5.6 Why this matters architecturally

`glob` is important because it lets the agent locate files by shape before content inspection begins.

That reduces the amount of blind reading the agent has to do and makes later `read_file` or `edit` calls more targeted.

---

## 6. New builtin tool: `grep` (`core/tools/builtin/grep.py`)

`GrepTool` adds content-based search across files.

### 6.1 Why `grep` matters

Once a coding agent knows roughly where to look, it often still needs to find:

- symbol names,
- log messages,
- exact strings,
- regex-shaped patterns,
- or repeated code fragments.

That is exactly the role of `grep`.

`read_file` is excellent when the target file is already known.

`grep` is excellent when the agent first needs to answer:

- *which files mention this thing, and on which lines?*

### 6.2 Input contract

`GrepParams` defines:

- `pattern`
- `path`
- `case_insensitive`

This is again a focused and sensible interface.

The tool is explicitly regex-based, which makes it more flexible than a plain substring search.

### 6.3 Execution flow

`GrepTool.execute(...)` performs a careful sequence:

1. parse arguments into `GrepParams`,
2. resolve the search root,
3. validate path existence,
4. compile the regex with optional `re.IGNORECASE`,
5. choose either:
   - a directory traversal via `_find_files(...)`, or
   - a direct single-file search,
6. read candidate files as UTF-8 when possible,
7. scan line by line,
8. emit grouped results by file path with line numbers,
9. return summary metadata even when there are no matches.

### 6.4 Output format

The output format groups matches like this:

- `=== relative/path.py ===`
- `line_number:matching line`

That is a practical shape because it gives both:

- enough context for the model to decide what file to inspect next,
- and enough structure for a human to read comfortably in the terminal.

### 6.5 Metadata returned

The tool returns metadata such as:

- `path`
- `matches`
- `files_searched`

That provides a useful summary of search scope and outcome.

### 6.6 Directory traversal policy

The internal `_find_files(...)` method skips common noisy or generated directories:

- `node_modules`
- `__pycache__`
- `.git`
- `.venv`
- `venv`

It also skips hidden files and avoids binary files.

This is an important operational detail.

It means `grep` is not just a raw brute-force walk. It already includes some pragmatic workspace hygiene so searches stay more relevant and manageable.

### 6.7 Match and file limits

The traversal limits the candidate file list to 500 files.

That is a notable code nuance because it establishes an implicit safety/performance boundary.

The tool is optimized for practical local search, not for arbitrarily large repository indexing.

### 6.8 Why `grep` matters architecturally

`grep` is the missing bridge between:

- broad workspace navigation (`list_dir`, `glob`),
- and precise file reading/editing (`read_file`, `edit`, `write_file`).

It improves the agent's ability to form a better target set before it consumes more tokens reading files or attempts risky edits.

---

## 7. `glob` and `grep` together: why both are needed

These two tools are related, but they solve different discovery problems.

### 7.1 `glob` answers path-shape questions

Examples:

- all Python files under `src/`
- all markdown docs under `docs/`
- all test files matching a naming pattern
- all config files with a given extension

### 7.2 `grep` answers content questions

Examples:

- where is `ToolResult` referenced?
- which files contain `begin_assistant`?
- where does a specific error message appear?
- what lines match a regex describing a function signature?

### 7.3 Why the combination matters

A capable coding agent often needs both layers of narrowing:

1. restrict the candidate file universe by path pattern,
2. then search the reduced set for content patterns,
3. then inspect exact files with `read_file`.

That is why this is more than “two new tools.”

It is the addition of a more natural search workflow to the overall agent runtime.

---

## 8. Tool-system progression from `07` to `08`

The progression now looks like this:

- `07` gave the agent stronger local action tools,
- `08` gives the agent stronger local discovery tools.

That is a very natural next step.

Once an agent can edit, write, and run commands, the next bottleneck is often not execution power. It is target selection.

So `08` improves the system in a way that can reduce:

- unnecessary file reads,
- guesswork about file names,
- wide directory browsing,
- and poorly targeted edits.

In that sense, `08` is about **precision before action**.

---

## 9. Terminal UI refinement (`core/ui/tui.py`)

The only UI code diff in this iteration is small, but it is still worth documenting precisely.

### 9.1 The change itself

In the generic fallback rendering branch of `TUI.tool_call_complete(...)`, the code changes from:

- `if error:`

to:

- `if error and not success:`

### 9.2 Why this matters

This is a subtle correctness improvement.

The tool-result model allows a result to carry both:

- output text,
- and an `error` field.

In practice, the UI should only render the `Error: ...` block when the tool actually failed.

Without this guard, a successful tool result that happened to include an `error` string-like payload could be displayed misleadingly in the terminal.

So even though the code change is tiny, it improves the semantic integrity of tool output presentation.

### 9.3 Why this belongs in `08`

`07` made tool rendering much richer.

`08` adds search tools and then lightly tightens the rendering logic so the fallback path better matches runtime truth.

That is a natural follow-on polish step once more tools are added.

---

## 10. Why the UI change is smaller than the capability change

It is important to be precise about the scope here.

This diff does **not** add a brand-new custom renderer for `grep` or `glob` comparable to the specialized renderers for:

- `read_file`
- `write_file`
- `edit`
- `shell`
- `list_dir`

Instead, `grep` and `glob` currently benefit from:

- generic tool-call start rendering,
- argument ordering support already present in `_ordered_args(...)`,
- and the generic fallback completion rendering path.

So the main runtime gain in `08` is not a UI overhaul. It is the capability addition of search tools, paired with a minor UI correctness cleanup.

That is the accurate interpretation of the diff.

---

## 11. End-to-end workflow impact after this commit

A coding-oriented request can now unfold more effectively like this:

1. the user asks a question that implies file or symbol discovery,
2. the model uses `glob` to find likely files by pattern,
3. the model uses `grep` to search those files or directories for matching text,
4. the model uses `read_file` to inspect the most relevant matches in detail,
5. the model uses `edit` or `write_file` to apply changes,
6. the model uses `shell` to verify behavior if needed,
7. `TUI` renders the tool invocations and outputs, while the fallback branch now avoids incorrectly surfacing error text on successful tool runs.

This sequence is much closer to how an actual developer works in a terminal.

That is the real value of `08`.

---

## 12. Important code-level nuances and implications

### 12.1 The search tools are still local and bounded

Both `grep` and `glob` are local workspace tools.

They are not indexing systems, database-backed search services, or external network searches.

That keeps them aligned with the project's local-first design.

### 12.2 `grep` has more defensive filtering than `glob`

`grep` actively uses a traversal helper that:

- skips known noisy directories,
- skips hidden files,
- avoids binary files,
- and caps the number of files searched.

`glob`, by contrast, currently uses `Path.glob(...)` directly and does not apply the same helper in its active execution path.

That means the two tools are related, but not yet symmetric in filtering policy.

This is worth calling out because it may become a future cleanup area.

### 12.3 `glob.py` contains imports/helpers not used in the current active path

`glob.py` imports `os`, `re`, and helper utilities tied to `_find_files(...)`, but the actual `execute(...)` path does not use all of them.

This is not catastrophic, but it does show the tool is still somewhat early and may have some leftover scaffolding.

### 12.4 Search output remains plain text with structured metadata

Neither new tool introduces a new artifact type like `FileDiff`.

That is appropriate.

Their value lies in returning readable search results plus summary metadata rather than richer binary/structural result objects.

### 12.5 Tool extensibility continues to scale cleanly

The fact that `08` mainly adds two files and a registry update is a good sign.

It shows the tool architecture is doing its job: capabilities can grow without forcing invasive changes into the agent loop.

### 12.6 The UI correction reflects an important principle

Even small presentation guards matter in agent systems.

If the terminal confuses success and failure states, user trust degrades quickly.

So the `if error and not success` refinement is a small patch with disproportionate observability value.

---

## 13. Delta summary table (`07` -> current uncommitted state)

| Area | `07` baseline | Current uncommitted delta (`08`) |
|---|---|---|
| Discovery tooling | `list_dir` only for broad structure browsing | Adds `glob` for path discovery and `grep` for content discovery |
| Default builtin registry | Read/write/edit/list/shell tool set | Registry now includes `GrepTool` and `GlobTool` |
| Workspace search strategy | Primarily read known files or browse directories | Agent can now search by filename pattern and regex content |
| Search output | No dedicated builtin content/path search tool output | Plain-text grouped search results plus metadata |
| `grep` scope control | N/A | Skips common noisy dirs, hidden files, binary files, and caps file count |
| `glob` result control | N/A | Limits displayed file matches to 1000 and returns match count |
| UI completion fallback | Could render `error` text whenever present | Now only renders error text when `success` is false |
| Big-picture workflow | Inspect/change/verify | Discover/inspect/change/verify |

---

## 14. Big-picture significance

`08` is a smaller change set than `07`, but it is still an important one.

`07` expanded what the agent could do once it knew where to operate.

`08` improves how the agent figures out where to operate in the first place.

That is a meaningful step because many agent failures do not come from lacking edit capability. They come from poor targeting:

- reading the wrong file,
- missing the relevant symbol,
- searching too broadly or too narrowly,
- or spending too many tokens on manual discovery.

By adding `glob` and `grep`, the project strengthens the upstream part of the agent workflow: discovery and narrowing.

That makes later actions more efficient and more likely to be correct.

So even though this diff is modest in size, it advances the runtime toward a more complete coding-agent loop.

---

## 15. Natural continuation points for a future `09`

Natural next-step topics after this iteration would be:

- adding dedicated `TUI` renderers for `grep` and `glob` outputs,
- unifying filtering policy between `glob` and `grep`,
- removing unused helper/import residue in `glob.py`,
- introducing result truncation/summary refinement for very large search outputs,
- enabling tool policy controls around search scope,
- and composing search tools more explicitly into agent prompt guidance or tool-selection heuristics.

That would continue the transition from:

- **expanded search/discovery capability**

into:

- **more precise and ergonomically rendered codebase navigation**.

---

## 16. Key takeaways

1. The main uncommitted change after `07` is the addition of two discovery-oriented builtin tools: `glob` for path matching and `grep` for regex-based content search.

2. These tools fill an important gap between directory browsing and direct file editing by letting the agent narrow targets before reading or mutating files.

3. `core/tools/builtin/__init__.py` is updated so both tools become part of the default builtin registry without any special-case orchestration logic.

4. `grep` is the more defensive of the two new tools: it skips noisy/generated directories, hidden files, binary files, and caps the search set size.

5. `glob` provides useful filename/path discovery, though its current active path is simpler and less filtered than `grep`.

6. The `TUI` change in this diff is small but meaningful: error text in the fallback rendering path is now shown only when a tool actually fails.

7. Architecturally, `08` shifts the system from mostly `inspect/change/verify` toward `discover/inspect/change/verify`.

8. This is an incremental but important step toward a more practical coding agent, because better target discovery usually leads to better downstream reads, edits, and command execution.

