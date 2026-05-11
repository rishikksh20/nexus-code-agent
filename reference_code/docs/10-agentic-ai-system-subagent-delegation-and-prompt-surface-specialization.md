# 10. Agentic AI System Custom Tool Discovery from Agent Directory

This document describes the current uncommitted delta since the previous commit, focused on one feature:

- discovery and registration of custom tools from agent directories.

This update replaces the earlier sub-agent-focused write-up with the behavior now present in the codebase.

---

## 1. High-level change in this iteration

The runtime now supports loading custom tool classes from Python files placed in:

- project scope: `.ai-agent/tools/*.py` under the active `cwd`
- user scope: `.ai-agent/tools/*.py` under the global config directory

At session startup, these discovered tools are instantiated with the active `Config` and registered into the existing `ToolRegistry` alongside default tools.

The result is a pluggable tool surface that can be extended without editing core builtin tool modules.

---

## 2. Change scope since last commit

### New module

- `core/tools/discovery.py`

### Updated module

- `core/agent/session.py`

No other tracked files are currently modified in this delta.

---

## 3. New module: `core/tools/discovery.py`

`ToolDiscoveryManager` is introduced to discover and register local custom tools.

### 3.1 Responsibilities

`ToolDiscoveryManager` owns four operations:

1. load a Python module from a file path (`_load_tool_modules`)
2. inspect the module and collect valid `Tool` subclasses (`_find_tool_classes`)
3. discover tools in one directory root (`discover_from_directory`)
4. run all configured discovery roots (`discover_all`)

### 3.2 Discovery roots

`discover_all` scans two roots:

1. `self.config.cwd`
2. `get_config_dir()`

For each root it resolves:

- `<root>/.ai-agent/tools`

and loads all `*.py` files in that directory, skipping names starting with `__`.

### 3.3 Class filtering rules

A class is considered discoverable only if all are true:

- it is a class object
- it subclasses `Tool`
- it is not the base `Tool` class itself
- it is defined in that discovered module (`obj.__module__ == module.__name__`)

This avoids accidentally registering imported helper classes from other modules.

### 3.4 Registration behavior

For each discovered class:

1. instantiate with `tool_class(self.config)`
2. register via `self.registry.register(tool)`

Because `ToolRegistry.register` overwrites existing names and logs a warning on collision, discovered tools can intentionally replace existing tool names if needed.

### 3.5 Error model

Discovery is fail-soft:

- invalid directories are ignored
- per-file load/inspection/instantiation errors are swallowed with `continue`

This preserves session startup continuity even if one custom tool file is broken.

---

## 4. Session integration: `core/agent/session.py`

`Session` now integrates discovery as a first-class startup step.

### 4.1 Construction updates

After creating the registry and context manager, the session now:

1. creates `ToolDiscoveryManager(self.config, self.tool_registry)`
2. calls `self.discovery_manager.discover_all()`

This means custom tools are registered before regular turn execution begins.

### 4.2 Effective startup flow now

`main.py -> Agent(config) -> Session(config)`

Inside `Session`:

1. build client
2. build default registry
3. build context manager (memory + current tool list)
4. discover and register custom tools from project/user `.ai-agent/tools`
5. start turn loop

---

## 5. Architectural significance

This change adds a plugin-style extension point while preserving the existing runtime model.

- No agent-loop rewrite is required.
- Discovery composes with the current registry abstraction.
- Tool loading is directory-convention based, making customization explicit and local.

The architecture shifts from "all tools are compiled into core" toward "core + convention-based runtime extensions".

---

## 6. Important nuances

### 6.1 Prompt tool list timing

In `Session`, `ContextManager` is currently created before `discover_all()` executes.

So the initial system prompt receives the tool list from the registry before discovered custom tools are added. Tool invocation can still work at runtime because tools are registered afterward, but prompt-level visibility of newly discovered tools may lag unless prompt/context are rebuilt.

### 6.2 Collision policy

If a discovered tool uses the same `name` as an existing tool, registry behavior is overwrite-with-warning.

This enables intentional overrides but can also hide mistakes if names collide unintentionally.

### 6.3 Limited diagnostics

`discover_from_directory` currently suppresses exceptions silently on per-file failure. This keeps startup resilient, but debugging bad plugin files can be harder without surfaced logs.

---

## 7. Delta summary table

| Area | Previous commit behavior | Current uncommitted behavior |
|---|---|---|
| Custom tool loading | Not loaded from agent directories | Loaded from `.ai-agent/tools` in project and user config roots |
| Discovery runtime component | None | Adds `ToolDiscoveryManager` |
| Session startup | Registry created and used as-is | Registry is additionally extended by discovery at startup |
| Failure handling | N/A for discovery | Fail-soft per file and per directory |
| Override semantics | Only explicit registration paths | Discovered tools can overwrite same-name registered tools |

---

## 8. Key takeaways

1. The main delta since the last commit is custom tool discovery from `.ai-agent/tools` directories.
2. The new discovery manager dynamically imports Python modules, finds valid `Tool` subclasses, and registers them in the existing registry.
3. Session startup now performs discovery automatically, so custom tools are available without core code edits.
4. The implementation is resilient (fail-soft) but currently low-visibility for discovery errors.
5. Prompt construction currently happens before discovery registration, which may limit immediate prompt awareness of newly discovered tools.
