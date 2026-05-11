# 02-2 — Plugins: Extending the Registry from External Packages

## Prerequisites

Complete [02-1-mcp-integration.md](02-1-mcp-integration.md) first.

MCP connects to external servers. Plugins are different: they are **Python packages** that extend your agent by registering tools, hooks, or skills directly into the runtime — installed via `pip`, not started as processes.

This chapter builds a `PluginLoader` that discovers and loads plugins at startup, following the same `pyproject.toml` entry-point pattern used by pytest, mypy, and Flask extensions.

---

## What you will build

```
agent/
    plugins.py         ← NEW: PluginContract, PluginLoader, load_plugins()
main.py                ← updated: call load_plugins() before building registry
example_plugin/        ← NEW: minimal example plugin package
    __init__.py
    pyproject.toml
```

---

## 1. The plugin contract

A plugin is any Python package that exposes one callable at a known entry point. The callable receives the live registry and hook executor and does whatever it wants with them:

```python
# The entire plugin API contract — one function signature:
def register(registry: ToolRegistry, hooks: HookExecutor) -> None:
    registry.register(MyCustomTool())
    hooks.register(MyCustomHook())
```

That is it. The plugin sees the same `BaseTool` and `Hook` interfaces you have been using throughout the series. There is no special plugin SDK to learn.

---

## 2. Create `agent/plugins.py`

```python
# agent/plugins.py

from __future__ import annotations

import importlib
import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.tools import ToolRegistry
from agent.hooks import HookExecutor


# ── Plugin metadata ───────────────────────────────────────────────────────────

ENTRY_POINT_GROUP = "agent.plugins"    # name in pyproject.toml [project.entry-points]


@dataclass
class PluginInfo:
    """Metadata about one loaded plugin."""
    name: str
    module: str
    source: str    # "entry_point" | "directory" | "builtin"
    loaded: bool = False
    error: str = ""


# ── Loader ────────────────────────────────────────────────────────────────────

class PluginLoader:
    """
    Discovers and loads plugins from two sources:
      1. Entry points — pip-installed packages that declare 'agent.plugins' entry point
      2. Local directory — .py files in a plugins/ directory beside main.py

    All plugins receive the live ToolRegistry and HookExecutor.
    A plugin that raises during register() is logged but does NOT crash the agent.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        hook_executor: HookExecutor,
        plugins_dir: Path | None = None,
        allow_list: list[str] | None = None,
    ) -> None:
        self.registry = registry
        self.hooks = hook_executor
        self.plugins_dir = plugins_dir or Path("plugins")
        self.allow_list = allow_list    # if set, only load plugins in this list
        self._loaded: list[PluginInfo] = []

    def load_all(self) -> list[PluginInfo]:
        """Load from entry points first, then from local directory."""
        self._load_entry_points()
        self._load_local_directory()
        return self._loaded

    def summary(self) -> str:
        if not self._loaded:
            return ""
        ok = [p for p in self._loaded if p.loaded]
        err = [p for p in self._loaded if not p.loaded]
        lines = [f"Plugins ({len(ok)} loaded, {len(err)} failed):"]
        for p in ok:
            lines.append(f"  ✓ {p.name}")
        for p in err:
            lines.append(f"  ✗ {p.name}: {p.error}")
        return "\n".join(lines)

    # ── Entry point discovery ─────────────────────────────────────────────────

    def _load_entry_points(self) -> None:
        try:
            eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
        except Exception:
            return

        for ep in eps:
            info = PluginInfo(name=ep.name, module=ep.value, source="entry_point")
            if self.allow_list is not None and ep.name not in self.allow_list:
                continue
            self._call_register(info, ep.load)

    # ── Local directory discovery ─────────────────────────────────────────────

    def _load_local_directory(self) -> None:
        if not self.plugins_dir.exists():
            return

        for path in sorted(self.plugins_dir.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            name = path.stem
            info = PluginInfo(name=name, module=str(path), source="directory")
            if self.allow_list is not None and name not in self.allow_list:
                continue

            def make_loader(p):
                def loader():
                    spec = importlib.util.spec_from_file_location(p.stem, p)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return getattr(mod, "register")
                return loader

            self._call_register(info, make_loader(path))

    # ── Safe registration ─────────────────────────────────────────────────────

    def _call_register(self, info: PluginInfo, loader_fn) -> None:
        try:
            register_fn = loader_fn()
            register_fn(self.registry, self.hooks)
            info.loaded = True
        except Exception as exc:
            info.error = str(exc)
        finally:
            self._loaded.append(info)


# ── Convenience function ──────────────────────────────────────────────────────

def load_plugins(
    registry: ToolRegistry,
    hook_executor: HookExecutor,
    plugins_dir: Path | None = None,
    allow_list: list[str] | None = None,
) -> list[PluginInfo]:
    """
    One-call plugin loading for main.py.

    Returns list of loaded plugins (for display/logging).
    Failed plugins are included in the list with loaded=False.
    """
    loader = PluginLoader(
        registry=registry,
        hook_executor=hook_executor,
        plugins_dir=plugins_dir,
        allow_list=allow_list,
    )
    return loader.load_all()
```

---

## 3. Write an example plugin

Create a local plugin in `plugins/wordcount.py`:

```python
# plugins/wordcount.py
# A simple plugin that adds a word count tool and a logging hook.

from agent.tools import BaseTool, ToolResult, ToolExecutionContext, ToolRegistry
from agent.hooks import HookExecutor, HookEvent, HookResult
from typing import Any


class WordCountTool(BaseTool):
    """Count words in a text string."""
    name = "word_count"
    description = "Count the number of words in a text string."
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to count words in."}
        },
        "required": ["text"],
    }
    is_mutating = False

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        text = arguments.get("text", "")
        count = len(text.split())
        return ToolResult(output=f"{count} words", metadata={"word_count": count})


class VerboseLogHook:
    """Log every tool call to stdout with extra detail."""
    event = HookEvent.POST_TOOL_USE

    async def run(self, payload: dict[str, Any]) -> HookResult:
        tool = payload.get("tool_name", "?")
        ok = "✓" if not payload.get("is_error") else "✗"
        print(f"  [plugin:log] {ok} {tool}")
        return HookResult.allow()


# The required entry point — called by PluginLoader
def register(registry: ToolRegistry, hooks: HookExecutor) -> None:
    registry.register(WordCountTool())
    hooks.register(VerboseLogHook())
```

---

## 4. Make it installable (optional but best practice)

```toml
# example_plugin/pyproject.toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "my-agent-plugin"
version = "0.1.0"
dependencies = []

# This is the key line — declares the plugin entry point
[project.entry-points."agent.plugins"]
wordcount = "my_agent_plugin:register"
```

```bash
pip install -e ./example_plugin/
```

The plugin is now discovered automatically via `importlib.metadata.entry_points()` without any code changes in your agent.

---

## 5. Update `main.py`

```python
# main.py  — updated build_agent()

from agent.plugins import load_plugins, PluginLoader
from pathlib import Path

def build_agent(project_notes: str = "", mode = ExecutionMode.DEFAULT) -> Agent:
    # ...existing registry and hook_executor setup...

    # Load plugins AFTER registry and hooks are built but BEFORE agent is constructed
    infos = load_plugins(
        registry=registry,
        hook_executor=executor,
        plugins_dir=Path("plugins"),
        # allow_list=["wordcount"]  # uncomment to whitelist specific plugins
    )

    for info in infos:
        if info.loaded:
            print(f"  ✓ plugin: {info.name}")
        else:
            print(f"  ✗ plugin: {info.name} — {info.error}")

    return Agent(...)
```

---

## 6. Security rules for plugins

Plugins have full access to the registry and hook executor. A malicious plugin can:
- Register tools that exfiltrate data
- Register hooks that silently block all safety checks
- Import and run arbitrary code at load time

**Minimum safety rules:**

1. **Directory plugins** — only load from a specific `plugins/` directory you control; never eval strings
2. **Entry-point plugins** — only load if the package is in an explicit `allow_list`
3. **No network at load time** — plugins should only register objects; network calls happen in `execute()`
4. **Plugins still go through guardrails** — `MCPToolAdapter` and plugin-registered tools both pass through `GuardrailChecker` and `PermissionChecker`
5. **Review before install** — treat `pip install my-agent-plugin` like `pip install` of any arbitrary code

---

## 7. Checklist before moving on

- [ ] `PluginLoader` discovers plugins from entry points AND local `plugins/` directory
- [ ] A failing plugin does not crash the agent — error is recorded in `PluginInfo.error`
- [ ] `load_plugins()` is called after registry and hooks exist, before agent is built
- [ ] Example `plugins/wordcount.py` adds a tool and a hook
- [ ] `allow_list` optionally restricts which plugins are loaded
- [ ] Plugins go through guardrails and permissions — no bypass
- [ ] `plugins/` directory files starting with `_` are skipped

---

Next: [03-session-manager.md](03-session-manager.md) — save and restore conversation state across sessions.

*After completing Chapter 03, see [03-1-context-compaction.md](03-1-context-compaction.md) for managing long conversations without hitting token limits.*

