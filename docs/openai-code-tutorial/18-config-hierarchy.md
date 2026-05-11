# 18 — Config Hierarchy: Global, Local, and Environment Overrides

## Prerequisites

Complete [17-cli-flags-and-headless-mode.md](17-cli-flags-and-headless-mode.md) first.

Chapter 13-1 gave you a single `agent.toml` file read from the project directory. Chapter 17 added CLI flags that override individual settings. That is good enough for a single project, but breaks down the moment you work across multiple projects.

Consider what happens today if you want to:

- always use `gpt-4o-mini` locally to save costs without changing every project's `agent.toml`,
- set a personal default log level across all workspaces,
- or keep your API key loading pattern consistent everywhere.

Right now you have to either edit every project file or remember to pass flags every time. Both are wrong.

The fix is a **two-tier config hierarchy**: a personal global file that lives at `~/.agent/agent.toml` and a project-local file at `agent.toml`. The local file overrides the global. CLI flags override both. Environment variables sit between them.

This chapter adds that hierarchy to the loader while keeping the `AgentConfig` dataclass and the rest of `main.py` unchanged.

---

## What you will build

```text
agent/
    config.py           ← updated: load_global_config(), merge_configs(),
                                   GlobalConfig, init_global_config()

~/.agent/
    agent.toml          ← NEW: user-level defaults (never committed anywhere)

agent.toml              ← existing: project-level overrides (committed)

main.py                 ← minor update: pass global_config_path to load_config()
```

By the end of the chapter, your config loads as:

```
built-in defaults
    ↓ overlaid by
~/.agent/agent.toml  (global — personal preferences)
    ↓ overlaid by
./agent.toml         (local — project settings)
    ↓ overlaid by
AGENT_* env vars     (environment — deployment or secret overrides)
    ↓ overlaid by
CLI flags            (per-run — highest priority)
```

---

## 1. Why five tiers

Each tier solves a different problem. Understanding what belongs at each level matters more than the implementation.

### Tier 1 — Built-in defaults

These live in your Python dataclasses (`ModelConfig`, `SessionConfig`, etc.). They ensure the harness works out of the box with no config files at all. Never put secrets here.

### Tier 2 — Global config (`~/.agent/agent.toml`)

This is your personal developer profile for all projects. Good things to put here:

- your preferred model and provider (not billable defaults you share with a team),
- your default log level,
- your preferred output format,
- whether you want the REPL to stream tokens or batch them.

This file is **never committed**. It lives in your home directory. Each developer on a team has their own.

### Tier 3 — Local config (`agent.toml`)

This is the project configuration, committed to version control. Good things to put here:

- the canonical model the project should use,
- project-specific permission rules,
- skill and plugin directories,
- sandbox settings relevant to the codebase,
- CI safety defaults like `[headless] deny_mutating = true`.

This file is **always committed** and identical for everyone on the team.

### Tier 4 — Environment variables (`AGENT_*`)

Good for:

- secrets that should never touch the filesystem,
- deployment-specific overrides in CI or Docker,
- temporary overrides in scripts without editing any file.

These were added in Chapter 13-1. They still work the same way.

### Tier 5 — CLI flags

The highest-priority override. Useful for one-off runs that differ from all defaults. These were added in Chapter 17.

### The rule

> Personal preferences go global. Project behavior goes local. Secrets go env. One-off experiments go CLI.

---

## 2. Design the global config file

The global file uses the same TOML structure as `agent.toml`. Only the sections you include get applied; sections you omit fall through to the project config or built-in defaults.

```toml
# ~/.agent/agent.toml — personal developer defaults
# This file is NEVER committed. It is your private config across all projects.

[model]
provider = "openai"
name     = "gpt-4o-mini"          # personal cost-saving override

[mode]
default = "default"

[logging]
level  = "INFO"
format = "json"

[headless]
output_format = "text"
quiet         = false

[ui]
stream_tokens   = true             # show tokens as they arrive in the REPL
show_tool_names = true             # print tool name before result
show_cost       = true             # show estimated cost after each turn
```

Note the `[ui]` section: this is a global-only section. It controls personal display preferences that have no place in a committed project config.

---

## 3. Add `UIConfig` to `agent/config.py`

```python
# agent/config.py  — add UIConfig

@dataclass
class UIConfig:
    """
    Personal display preferences. Global config only; not meaningful in project agent.toml.
    """
    stream_tokens: bool = True       # stream tokens as they arrive
    show_tool_names: bool = True     # print tool name before each result
    show_cost: bool = False          # show estimated cost after each turn
    show_session_id: bool = False    # show active session ID in prompt
    prompt_prefix: str = "you"       # REPL prompt label (e.g. "you> ")
    response_prefix: str = "agent"   # label before assistant responses
```

Add it to `AgentConfig`:

```python
@dataclass
class AgentConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    headless: HeadlessConfig = field(default_factory=HeadlessConfig)
    cli: CLIConfig = field(default_factory=CLIConfig)
    ui: UIConfig = field(default_factory=UIConfig)                   # NEW

    @property
    def api_key(self) -> str:
        return os.environ.get("OPENAI_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
```

---

## 4. Update `agent/config.py` with a two-tier loader

Replace the single `load_config()` with a version that accepts both paths and merges them in order.

```python
# agent/config.py  — replace load_config()

from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    try:
        import tomli as tomllib      # pip install tomli
    except ModuleNotFoundError:
        tomllib = None               # type: ignore[assignment]


def _read_toml(path: Path) -> dict:
    """Read a TOML file and return its contents as a dict, or {} if missing."""
    if tomllib is None:
        raise RuntimeError(
            "tomllib not available. Install tomli: pip install tomli"
        )
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _merge(base: dict, override: dict) -> dict:
    """
    Deep-merge two TOML dicts.

    For each key in override:
    - If both values are dicts, recurse.
    - Otherwise the override value wins.
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(
    config_path: str | Path | None = None,
    global_config_path: str | Path | None = None,
    search_parents: bool = True,
) -> "AgentConfig":
    """
    Load config from the two-tier hierarchy and apply env overrides.

    Merge order (later entries win):
        1. Built-in Python defaults (dataclass field defaults)
        2. Global config:  global_config_path or ~/.agent/agent.toml
        3. Local config:   config_path or first agent.toml found walking up from cwd
        4. AGENT_* environment variables
        5. (CLI flags applied separately in _apply_cli_overrides in main.py)
    """
    # ── Resolve file paths ────────────────────────────────────────────────────
    global_path = Path(global_config_path) if global_config_path else (
        Path.home() / ".agent" / "agent.toml"
    )

    if config_path:
        local_path: Path | None = Path(config_path)
    elif search_parents:
        local_path = _find_local_config(Path.cwd())
    else:
        local_path = Path("agent.toml") if Path("agent.toml").exists() else None

    # ── Read both files ───────────────────────────────────────────────────────
    global_data = _read_toml(global_path)
    local_data  = _read_toml(local_path) if local_path else {}

    # ── Deep-merge: global then local ─────────────────────────────────────────
    merged = _merge(global_data, local_data)

    # ── Build AgentConfig from merged dict ────────────────────────────────────
    config = _build_config(merged)

    # ── Apply env-var overrides on top ────────────────────────────────────────
    _apply_env_overrides(config)

    return config


def _find_local_config(start: Path) -> Path | None:
    """Walk up from start directory until agent.toml is found or filesystem root."""
    for directory in [start, *start.parents]:
        candidate = directory / "agent.toml"
        if candidate.exists():
            return candidate
    return None


def _build_config(data: dict) -> "AgentConfig":
    """Build AgentConfig from a merged TOML dict."""
    m  = data.get("model", {})
    s  = data.get("session", {})
    me = data.get("memory", {})
    sk = data.get("skills", {})
    pl = data.get("plugins", {})
    pe = data.get("permissions", {})
    mo = data.get("mode", {})
    sa = data.get("sandbox", {})
    co = data.get("compaction", {})
    lo = data.get("logging", {})
    te = data.get("telemetry", {})
    he = data.get("headless", {})
    cl = data.get("cli", {})
    ui = data.get("ui", {})

    return AgentConfig(
        model=ModelConfig(
            provider=m.get("provider", "demo"),
            name=m.get("name", "demo"),
            context_window=int(m.get("context_window", 8192)),
        ),
        session=SessionConfig(
            root=s.get("root", "sessions"),
            auto_save=bool(s.get("auto_save", True)),
        ),
        memory=MemoryConfig(
            root=me.get("root", ".agent-memory"),
            max_entries=int(me.get("max_entries", 200)),
            entry_ttl_days=int(me.get("entry_ttl_days", 90)),
        ),
        skills=SkillsConfig(root=sk.get("root", "skills")),
        plugins=PluginsConfig(
            root=pl.get("root", "plugins"),
            allow_list=list(pl.get("allow_list", [])),
        ),
        permissions=PermissionsConfig(
            write_allowed_root=pe.get("write_allowed_root", "."),
            deny_tools=list(pe.get("deny_tools", [])),
            allow_tools=list(pe.get("allow_tools", [
                "get_time", "echo", "read_file", "glob",
                "search_memory", "save_memory", "skill",
                "ask_user_question", "check_my_mailbox",
            ])),
        ),
        mode=ModeConfig(default=mo.get("default", "default")),
        sandbox=SandboxConfig(
            enabled=bool(sa.get("enabled", True)),
            image=sa.get("image", "agent-sandbox:latest"),
            timeout=float(sa.get("timeout", 30.0)),
            memory=sa.get("memory", "256m"),
        ),
        compaction=CompactionConfig(
            budget_fraction=float(co.get("budget_fraction", 0.80)),
            keep_first=int(co.get("keep_first", 2)),
            keep_last=int(co.get("keep_last", 20)),
        ),
        logging=LoggingConfig(
            level=lo.get("level", "INFO"),
            format=lo.get("format", "json"),
            path=lo.get("path", ".agent/logs/runtime.jsonl"),
        ),
        telemetry=TelemetryConfig(
            enabled=bool(te.get("enabled", True)),
            write_jsonl=bool(te.get("write_jsonl", True)),
        ),
        headless=HeadlessConfig(
            auto_confirm=bool(he.get("auto_confirm", False)),
            deny_mutating=bool(he.get("deny_mutating", False)),
            output_format=he.get("output_format", "text"),
            max_turns=int(he.get("max_turns", 0)) or None,
            quiet=bool(he.get("quiet", False)),
            no_session=bool(he.get("no_session", False)),
            no_plugins=bool(he.get("no_plugins", False)),
            no_skills=bool(he.get("no_skills", False)),
        ),
        cli=CLIConfig(
            entry_point=cl.get("entry_point", "agent"),
            version=cl.get("version", "0.1.0"),
        ),
        ui=UIConfig(
            stream_tokens=bool(ui.get("stream_tokens", True)),
            show_tool_names=bool(ui.get("show_tool_names", True)),
            show_cost=bool(ui.get("show_cost", False)),
            show_session_id=bool(ui.get("show_session_id", False)),
            prompt_prefix=ui.get("prompt_prefix", "you"),
            response_prefix=ui.get("response_prefix", "agent"),
        ),
    )
```

The key function is `_merge()`. It deep-merges two TOML dicts so that a local `[model]` section that only sets `name` does not erase the global `provider`. Only the keys that exist in the override file change.

---

## 5. Add `init_global_config()` for first-run setup

```python
# agent/config.py — add init_global_config()

def init_global_config(global_dir: Path | None = None) -> Path:
    """
    Create the global config directory and a starter ~/.agent/agent.toml.

    Called by `agent init --global` or on first run when the file is missing.
    Safe to call multiple times: does not overwrite an existing file.
    """
    directory = global_dir or (Path.home() / ".agent")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sessions").mkdir(exist_ok=True)
    (directory / "memory").mkdir(exist_ok=True)

    config_file = directory / "agent.toml"
    if config_file.exists():
        return config_file   # do not overwrite

    config_file.write_text(
        """\
# ~/.agent/agent.toml — personal developer defaults
# This file is NEVER committed to any project repository.
# Settings here apply to every project unless overridden by a local agent.toml.

[model]
# provider = "openai"
# name     = "gpt-4o-mini"

[mode]
default = "default"    # default | plan | auto

[logging]
level = "INFO"

[ui]
stream_tokens   = true
show_tool_names = true
show_cost       = false

[headless]
output_format = "text"
quiet         = false
""",
        encoding="utf-8",
    )
    return config_file
```

Update `_handle_init()` in `main.py` to call this:

```python
# main.py — update _handle_init()

def _handle_init(global_flag: bool = False) -> None:
    from agent.config import init_global_config

    # Always ensure the global directory exists on init
    global_file = init_global_config()
    print(f"Global config: {global_file}")

    if global_flag:
        # Only set up global config, stop here
        return

    # Local project setup
    config_path = Path("agent.toml")
    if config_path.exists():
        print(f"agent.toml already exists. Remove it to re-initialize.")
        raise SystemExit(EXIT_BAD_ARGS)

    config_path.write_text(
        """\
# agent.toml — project configuration
# Commit this file. Do NOT put secrets here.

[model]
provider       = "openai"
name           = "gpt-4o"
context_window = 128000

[mode]
default = "default"

[session]
root      = ".agent/sessions"
auto_save = true

[headless]
deny_mutating = false
output_format = "text"
""",
        encoding="utf-8",
    )
    print(f"Created agent.toml")

    agent_dir = Path(".agent")
    for sub in ["sessions", "memory", "logs"]:
        (agent_dir / sub).mkdir(parents=True, exist_ok=True)
    print(f"Created .agent/ directory structure")
```

Add `--global` to the `init` subcommand in `build_parser()`:

```python
# main.py — update init subparser
init_cmd = sub.add_parser("init", help="Create default config files")
init_cmd.add_argument("--global", dest="global_only", action="store_true",
                      help="Only create ~/.agent/agent.toml, skip local project setup")
```

---

## 6. Update `config show` to display all tiers

Now that there are two config files plus env vars, `agent config show` should be able to show each tier separately so you can debug what is actually active.

```python
# main.py — update _handle_config_show()

def _handle_config_show(config: AgentConfig, scope: str, global_path: Path, local_path: Path | None) -> None:
    import dataclasses

    if scope == "global":
        from agent.config import _read_toml
        data = _read_toml(global_path)
        print(f"=== Global config ({global_path}) ===")
        _print_toml_dict(data)
        return

    if scope == "local":
        from agent.config import _read_toml
        if local_path is None:
            print("No local agent.toml found in current directory or parents.")
            return
        data = _read_toml(local_path)
        print(f"=== Local config ({local_path}) ===")
        _print_toml_dict(data)
        return

    # Default: show merged (effective) config
    print(f"=== Merged config (global → local → env) ===")
    for f in dataclasses.fields(config):
        val = getattr(config, f.name)
        if dataclasses.is_dataclass(val):
            for sf in dataclasses.fields(val):
                print(f"  [{f.name}] {sf.name} = {getattr(val, sf.name)!r}")
        else:
            print(f"  {f.name} = {val!r}")


def _print_toml_dict(data: dict, indent: int = 0) -> None:
    prefix = "  " * indent
    for k, v in data.items():
        if isinstance(v, dict):
            print(f"{prefix}[{k}]")
            _print_toml_dict(v, indent + 1)
        else:
            print(f"{prefix}  {k} = {v!r}")
```

---

## 7. Update `main()` to pass both config paths

```python
# main.py — update load_config() call in main()

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcommand == "init":
        _handle_init(global_flag=getattr(args, "global_only", False))
        return

    global_config_path = getattr(args, "global_config", None) or (
        Path.home() / ".agent" / "agent.toml"
    )
    local_config_path = getattr(args, "config_path", None)

    config = load_config(
        config_path=local_config_path,
        global_config_path=global_config_path,
    )
    _apply_cli_overrides(config, args)

    if args.subcommand == "version":
        _handle_version(config)
        return

    if args.subcommand == "config":
        scope = (args.config_args[0] if args.config_args else "merged")
        local_path = _find_local_config(Path.cwd()) if not local_config_path else Path(local_config_path)
        _handle_config_show(config, scope, Path(global_config_path), local_path)
        return

    # ... rest of main() unchanged
```

Also add a `--global-config` flag to `build_parser()`:

```python
parser.add_argument("--global-config", metavar="PATH", type=Path,
                    dest="global_config",
                    help="Path to global agent.toml (default: ~/.agent/agent.toml)")
```

---

## 8. Environment variable reference

Environment variables sit between local config and CLI flags. They are useful for secrets, CI environments, and temporary overrides.

| Variable | Config field | Type | Example |
|---|---|---|---|
| `OPENAI_API_KEY` | `config.api_key` | secret | `sk-...` |
| `ANTHROPIC_API_KEY` | `config.api_key` | secret | `sk-ant-...` |
| `AGENT_MODEL_PROVIDER` | `config.model.provider` | str | `openai` |
| `AGENT_MODEL_NAME` | `config.model.name` | str | `gpt-4o` |
| `AGENT_MODEL_CONTEXT_WINDOW` | `config.model.context_window` | int | `128000` |
| `AGENT_MODE_DEFAULT` | `config.mode.default` | str | `plan` |
| `AGENT_SESSION_ROOT` | `config.session.root` | str | `.agent/sessions` |
| `AGENT_MEMORY_ROOT` | `config.memory.root` | str | `.agent-memory` |
| `AGENT_PERMISSIONS_WRITE_ALLOWED_ROOT` | `config.permissions.write_allowed_root` | str | `.` |
| `AGENT_SANDBOX_ENABLED` | `config.sandbox.enabled` | bool | `true` |
| `AGENT_COMPACTION_BUDGET_FRACTION` | `config.compaction.budget_fraction` | float | `0.70` |
| `AGENT_LOGGING_LEVEL` | `config.logging.level` | str | `DEBUG` |
| `AGENT_HEADLESS_AUTO_CONFIRM` | `config.headless.auto_confirm` | bool | `true` |
| `AGENT_HEADLESS_DENY_MUTATING` | `config.headless.deny_mutating` | bool | `true` |

The existing `_apply_env_overrides()` function from Chapter 13-1 handles these. Extend it to cover the new fields:

```python
# agent/config.py — extend _apply_env_overrides()

def _apply_env_overrides(config: AgentConfig) -> None:
    mapping = {
        "AGENT_MODEL_PROVIDER":    (config.model, "provider", str),
        "AGENT_MODEL_NAME":        (config.model, "name", str),
        "AGENT_MODEL_CONTEXT_WINDOW": (config.model, "context_window", int),
        "AGENT_MODE_DEFAULT":      (config.mode, "default", str),
        "AGENT_SESSION_ROOT":      (config.session, "root", str),
        "AGENT_MEMORY_ROOT":       (config.memory, "root", str),
        "AGENT_PERMISSIONS_WRITE_ALLOWED_ROOT": (config.permissions, "write_allowed_root", str),
        "AGENT_SANDBOX_ENABLED":   (config.sandbox, "enabled", _parse_bool),
        "AGENT_COMPACTION_BUDGET_FRACTION": (config.compaction, "budget_fraction", float),
        "AGENT_LOGGING_LEVEL":     (config.logging, "level", str),
        "AGENT_HEADLESS_AUTO_CONFIRM":  (config.headless, "auto_confirm", _parse_bool),
        "AGENT_HEADLESS_DENY_MUTATING": (config.headless, "deny_mutating", _parse_bool),
    }
    for env_key, (obj, attr, cast) in mapping.items():
        value = os.environ.get(env_key)
        if value is not None:
            try:
                setattr(obj, attr, cast(value))
            except (ValueError, TypeError) as e:
                print(f"Warning: invalid value for {env_key}={value!r}: {e}")


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")
```

---

## 9. Override precedence in practice

A common question is: what wins?

```bash
# Global config has: model.name = "gpt-4o-mini"
# Local agent.toml has: model.name = "gpt-4o"
# Run with:
AGENT_MODEL_NAME=claude-3-haiku python main.py --model gpt-4o-turbo

# Result: gpt-4o-turbo
# Precedence: CLI flag wins over env var wins over local file wins over global file
```

The `agent config show` subcommand lets you inspect this at any time:

```bash
agent config show global    # what your global file says
agent config show local     # what this project's agent.toml says
agent config show           # what is actually active after merging everything
```

---

## 10. Testing the hierarchy

```python
# tests/test_config.py

from pathlib import Path
import pytest
from agent.config import load_config


def test_local_overrides_global(tmp_path):
    """Local agent.toml should override global ~/.agent/agent.toml."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / "agent.toml").write_text(
        "[model]\nname = 'gpt-4o-mini'\nprovider = 'openai'\n"
    )

    local_dir = tmp_path / "project"
    local_dir.mkdir()
    (local_dir / "agent.toml").write_text(
        "[model]\nname = 'gpt-4o'\n"   # override name only; provider not set
    )

    config = load_config(
        config_path=local_dir / "agent.toml",
        global_config_path=global_dir / "agent.toml",
    )

    assert config.model.name == "gpt-4o"           # local wins
    assert config.model.provider == "openai"        # global fills in; not overridden


def test_env_var_overrides_both_files(tmp_path, monkeypatch):
    """AGENT_MODEL_NAME env var should win over both config files."""
    (tmp_path / "agent.toml").write_text("[model]\nname = 'gpt-4o'\n")
    monkeypatch.setenv("AGENT_MODEL_NAME", "claude-3-haiku")

    config = load_config(config_path=tmp_path / "agent.toml")

    assert config.model.name == "claude-3-haiku"


def test_missing_files_use_defaults(tmp_path):
    """Both files missing should produce built-in dataclass defaults."""
    config = load_config(
        config_path=tmp_path / "does_not_exist.toml",
        global_config_path=tmp_path / "also_missing.toml",
        search_parents=False,
    )

    assert config.model.provider == "demo"
    assert config.mode.default == "default"


def test_deep_merge_does_not_erase_sibling_keys(tmp_path):
    """
    A local [permissions] block that only sets one key should not
    erase the allow_tools list from the global file.
    """
    global_dir = tmp_path / "g"
    global_dir.mkdir()
    (global_dir / "agent.toml").write_text(
        "[permissions]\nwrite_allowed_root = '.'\nallow_tools = ['get_time', 'echo']\n"
    )

    local_dir = tmp_path / "l"
    local_dir.mkdir()
    (local_dir / "agent.toml").write_text(
        "[permissions]\ndeny_tools = ['bash']\n"   # only sets deny_tools
    )

    config = load_config(
        config_path=local_dir / "agent.toml",
        global_config_path=global_dir / "agent.toml",
    )

    assert "bash" in config.permissions.deny_tools        # from local
    assert "get_time" in config.permissions.allow_tools   # preserved from global
```

---

## 11. Checklist before moving on

- [ ] `~/.agent/agent.toml` is created by `agent init` if it does not exist
- [ ] `_merge()` deep-merges dicts; individual keys in local do not erase sibling keys in global
- [ ] Merge order is: built-in defaults → global → local → env vars → CLI flags
- [ ] `agent config show` prints the merged active config
- [ ] `agent config show global` prints only the global file
- [ ] `agent config show local` prints only the local file
- [ ] Missing files at either tier are silently skipped (use built-in defaults)
- [ ] `AGENT_*` env vars apply after both files
- [ ] `init_global_config()` does not overwrite an existing file
- [ ] `UIConfig` is global-only; project `agent.toml` can include it but it has no team-shared meaning
- [ ] Tests cover: local-wins, env-wins, missing-files, deep-merge sibling preservation

---

## 12. Exercises

**Exercise A — Config diff**

Add `agent config diff` that prints only the keys where global and local disagree. Useful when debugging why a project behaves differently from your personal defaults.

**Exercise B — Config set command**

Add `agent config set <section>.<key> <value>` that writes a key-value pair to the local `agent.toml` without overwriting other keys. Use `_read_toml()`, update the dict, and write back using `tomli_w` or a minimal string formatter.

**Exercise C — Config global set**

Same as B but writes to `~/.agent/agent.toml`. Useful for commands like:

```bash
agent config global set model.name gpt-4o-mini
```

**Exercise D — Profile switching**

Extend `GlobalConfig` to support named profiles:

```toml
[profiles.fast]
model.name = "gpt-4o-mini"

[profiles.smart]
model.name = "o1"
model.context_window = 200000
```

Add `--profile <name>` as a CLI flag that applies the named profile block on top of the merged base config.

---

Next: [19-slash-commands-and-repl-control.md](19-slash-commands-and-repl-control.md) — add `/`-prefixed REPL commands that let you inspect and change harness state without restarting the process.
