# 13-1 — Configuration System: TOML Files and Environment Variables

## Prerequisites

Complete [13-guardrails-and-safety.md](13-guardrails-and-safety.md) first.

Every `build_agent()` call in `main.py` has settings hardcoded as function arguments. Adding a new capability (a new MCP server, a changed permission policy, a different model) requires editing Python. This chapter replaces that with a proper configuration system using TOML files and environment variable overrides.

---

## What you will build

```
agent/
    config.py           ← NEW: AgentConfig dataclass, load_config()
agent.toml              ← NEW: project-level config (checked into git)
.env                    ← NEW: secrets / environment overrides (gitignored)
main.py                 ← updated: reads config before building agent
```

---

## 1. What belongs in config vs code

| Setting | Where |
|---|---|
| Model name, provider | `agent.toml` |
| Context window size | `agent.toml` |
| Permission policy | `agent.toml` |
| Default mode | `agent.toml` |
| Session/memory paths | `agent.toml` |
| API keys | `.env` or environment variables (never git) |
| Core agent loop logic | Python code (never config) |
| Guardrail hard-deny lists | Python code (safety-critical, must be reviewed) |

The rule: if changing the value changes *behavior* a user would configure, it belongs in config. If changing it changes *safety guarantees*, it belongs in code.

---

## 2. Create `agent.toml`

```toml
# agent.toml  —  project-level agent configuration
# Safe to commit to version control. Do NOT put secrets here.

[model]
provider = "openai"        # openai | anthropic | ollama | demo
name     = "gpt-4o"
context_window = 128000    # tokens (used for compaction budget)

[session]
root       = "sessions"    # directory for session JSON files
auto_save  = true

[memory]
root          = ".agent-memory"
max_entries   = 200        # prune if over this count
entry_ttl_days = 90        # remove entries older than this (0 = no TTL)

[skills]
root = "skills"            # directory to scan for SKILL.md files

[plugins]
root = "plugins"           # directory to scan for plugin .py files
allow_list = []            # if non-empty, only load named plugins

[permissions]
write_allowed_root = "."   # restrict writes to this directory
deny_tools = []            # e.g. ["bash"] to hard-deny shell access
allow_tools = [            # always-allowed without confirmation
    "get_time", "echo", "read_file", "glob",
    "search_memory", "save_memory", "skill",
    "ask_user_question", "check_my_mailbox",
]

[mode]
default = "default"        # default | plan | auto

[sandbox]
enabled = true             # use Docker sandboxing for bash tool if available
image   = "agent-sandbox:latest"
timeout = 30.0
memory  = "256m"

[compaction]
budget_fraction = 0.80     # compact when messages exceed 80% of context window
keep_first      = 2        # messages to always keep from conversation start
keep_last       = 20       # most-recent messages to always preserve
```

---

## 3. Create `agent/config.py`

```python
# agent/config.py

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Config dataclasses (typed) ────────────────────────────────────────────────

@dataclass
class ModelConfig:
    provider: str = "demo"
    name: str = "demo"
    context_window: int = 8192

@dataclass
class SessionConfig:
    root: str = "sessions"
    auto_save: bool = True

@dataclass
class MemoryConfig:
    root: str = ".agent-memory"
    max_entries: int = 200
    entry_ttl_days: int = 90

@dataclass
class SkillsConfig:
    root: str = "skills"

@dataclass
class PluginsConfig:
    root: str = "plugins"
    allow_list: list[str] = field(default_factory=list)

@dataclass
class PermissionsConfig:
    write_allowed_root: str = "."
    deny_tools: list[str] = field(default_factory=list)
    allow_tools: list[str] = field(default_factory=lambda: [
        "get_time", "echo", "read_file", "glob",
        "search_memory", "save_memory", "skill",
        "ask_user_question", "check_my_mailbox",
    ])

@dataclass
class ModeConfig:
    default: str = "default"

@dataclass
class SandboxConfig:
    enabled: bool = True
    image: str = "agent-sandbox:latest"
    timeout: float = 30.0
    memory: str = "256m"

@dataclass
class CompactionConfig:
    budget_fraction: float = 0.80
    keep_first: int = 2
    keep_last: int = 20


@dataclass
class AgentConfig:
    """
    Full agent configuration loaded from agent.toml and environment variables.

    Environment variable overrides follow the pattern:
        AGENT_MODEL_NAME=gpt-4o-mini
        AGENT_MODEL_PROVIDER=openai
        AGENT_MODE_DEFAULT=auto
    """
    model: ModelConfig = field(default_factory=ModelConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)

    # Secrets — never in TOML, always from environment
    @property
    def api_key(self) -> str:
        return os.environ.get("OPENAI_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")


# ── Loader ────────────────────────────────────────────────────────────────────

def load_config(
    config_path: str | Path | None = None,
    search_parents: bool = True,
) -> AgentConfig:
    """
    Load configuration from agent.toml, then apply environment variable overrides.

    Search order:
      1. Explicit path (if given)
      2. ./agent.toml
      3. Parent directories (if search_parents=True) — for nested project dirs
      4. Defaults (if no file found)

    Environment variables override any file value.
    Format: AGENT_{SECTION}_{KEY} = value (uppercased)
    """
    toml_data = _find_and_load_toml(config_path, search_parents)
    config = _build_config(toml_data)
    _apply_env_overrides(config)
    return config


def _find_and_load_toml(
    explicit: str | Path | None, search_parents: bool
) -> dict[str, Any]:
    """Find and parse agent.toml, returning an empty dict if not found."""
    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit))
    else:
        candidates.append(Path("agent.toml"))
        if search_parents:
            current = Path.cwd()
            for parent in current.parents:
                candidates.append(parent / "agent.toml")
                if parent == Path.home():
                    break

    for path in candidates:
        if path.exists():
            return _parse_toml(path)

    return {}   # no config file → use all defaults


def _parse_toml(path: Path) -> dict[str, Any]:
    """Parse TOML without requiring a third-party library (Python 3.11+)."""
    try:
        import tomllib                  # stdlib from Python 3.11
        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        try:
            import tomli as tomllib     # pip install tomli for Python 3.10
            with open(path, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            # Fallback: minimal key=value parser (no nested tables)
            print(f"Warning: tomllib not available. Install tomli for TOML support: pip install tomli")
            return {}


def _build_config(data: dict[str, Any]) -> AgentConfig:
    """Map TOML dict onto typed AgentConfig dataclasses."""
    def section(name: str) -> dict:
        return data.get(name, {})

    m = section("model")
    s = section("session")
    mem = section("memory")
    sk = section("skills")
    pl = section("plugins")
    p = section("permissions")
    mo = section("mode")
    sb = section("sandbox")
    co = section("compaction")

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
            root=mem.get("root", ".agent-memory"),
            max_entries=int(mem.get("max_entries", 200)),
            entry_ttl_days=int(mem.get("entry_ttl_days", 90)),
        ),
        skills=SkillsConfig(root=sk.get("root", "skills")),
        plugins=PluginsConfig(
            root=pl.get("root", "plugins"),
            allow_list=list(pl.get("allow_list", [])),
        ),
        permissions=PermissionsConfig(
            write_allowed_root=p.get("write_allowed_root", "."),
            deny_tools=list(p.get("deny_tools", [])),
            allow_tools=list(p.get("allow_tools", PermissionsConfig().allow_tools)),
        ),
        mode=ModeConfig(default=mo.get("default", "default")),
        sandbox=SandboxConfig(
            enabled=bool(sb.get("enabled", True)),
            image=sb.get("image", "agent-sandbox:latest"),
            timeout=float(sb.get("timeout", 30.0)),
            memory=sb.get("memory", "256m"),
        ),
        compaction=CompactionConfig(
            budget_fraction=float(co.get("budget_fraction", 0.80)),
            keep_first=int(co.get("keep_first", 2)),
            keep_last=int(co.get("keep_last", 20)),
        ),
    )


def _apply_env_overrides(config: AgentConfig) -> None:
    """
    Apply AGENT_{SECTION}_{KEY} environment variable overrides.

    Examples:
        AGENT_MODEL_NAME=gpt-4o-mini   → config.model.name = "gpt-4o-mini"
        AGENT_MODE_DEFAULT=plan        → config.mode.default = "plan"
        AGENT_COMPACTION_BUDGET_FRACTION=0.70 → config.compaction.budget_fraction = 0.70
    """
    mapping = {
        "AGENT_MODEL_PROVIDER":    (config.model, "provider", str),
        "AGENT_MODEL_NAME":        (config.model, "name", str),
        "AGENT_MODEL_CONTEXT_WINDOW": (config.model, "context_window", int),
        "AGENT_MODE_DEFAULT":      (config.mode, "default", str),
        "AGENT_SESSION_ROOT":      (config.session, "root", str),
        "AGENT_MEMORY_ROOT":       (config.memory, "root", str),
        "AGENT_PERMISSIONS_WRITE_ALLOWED_ROOT": (config.permissions, "write_allowed_root", str),
        "AGENT_SANDBOX_ENABLED":   (config.sandbox, "enabled", lambda v: v.lower() in ("1", "true", "yes")),
        "AGENT_COMPACTION_BUDGET_FRACTION": (config.compaction, "budget_fraction", float),
    }
    for env_key, (obj, attr, cast) in mapping.items():
        value = os.environ.get(env_key)
        if value is not None:
            try:
                setattr(obj, attr, cast(value))
            except (ValueError, TypeError) as e:
                print(f"Warning: invalid value for {env_key}={value!r}: {e}")
```

---

## 4. Update `main.py` to read config

```python
# main.py  — updated to read AgentConfig

from agent.config import load_config, AgentConfig

def build_agent(config: AgentConfig, mode_override: str | None = None) -> Agent:
    """Build agent from a typed config object."""
    from agent.modes import ExecutionMode
    from agent.permissions import PermissionPolicy, PermissionChecker
    from agent.memory import MemoryStore
    from agent.skills import load_skills_from_dir
    from agent.plugins import load_plugins
    from pathlib import Path

    mode = ExecutionMode(mode_override or config.mode.default)
    memory_store = MemoryStore(root=Path(config.memory.root))
    registry = default_registry(memory_store=memory_store)

    if config.skills.root:
        skill_registry = load_skills_from_dir(Path(config.skills.root))
        if skill_registry.names():
            registry.register(SkillTool(skill_registry))

    executor = HookExecutor()
    executor.register(LoggingHook())
    executor.register(AuditLogHook())
    executor.register(TurnSummaryHook())

    if config.plugins.root:
        load_plugins(
            registry=registry,
            hook_executor=executor,
            plugins_dir=Path(config.plugins.root),
            allow_list=config.plugins.allow_list or None,
        )

    policy = PermissionPolicy(
        write_allowed_root=config.permissions.write_allowed_root,
        deny_tools=set(config.permissions.deny_tools),
        allow_tools=set(config.permissions.allow_tools),
    )

    if config.sandbox.enabled:
        from agent.sandbox import DockerSandbox, SandboxedBashTool, docker_available, SandboxConfig as SC
        if docker_available():
            sb = DockerSandbox(SC(
                image=config.sandbox.image,
                timeout=config.sandbox.timeout,
                memory_limit=config.sandbox.memory,
            ))
            registry.register(SandboxedBashTool(sb))

    return Agent(
        model_client=_build_model_client(config),
        tool_registry=registry,
        cwd=__import__("os").getcwd(),
        model_name=config.model.name,
        hook_executor=executor,
        permission_checker=PermissionChecker(policy=policy),
        memory_store=memory_store,
        mode=mode,
        context_window=config.model.context_window,
        budget_fraction=config.compaction.budget_fraction,
    )


def _build_model_client(config: AgentConfig):
    from agent.client import DemoModelClient
    if config.model.provider == "demo":
        return DemoModelClient()
    elif config.model.provider == "openai":
        from agent.openai_client import OpenAIStreamingClient
        api_key = config.api_key
        if not api_key:
            print("Warning: OPENAI_API_KEY not set. Using demo client.")
            return DemoModelClient()
        return OpenAIStreamingClient(api_key=api_key, model=config.model.name)
    else:
        print(f"Warning: unknown provider '{config.model.provider}'. Using demo client.")
        return DemoModelClient()


async def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Agent harness")
    parser.add_argument("--config", help="Path to agent.toml", default=None)
    parser.add_argument("--mode", choices=["default", "plan", "auto"], default=None)
    parser.add_argument("--notes", metavar="FILE", help="Project notes file")
    parser.add_argument("--continue", dest="cont", action="store_true")
    parser.add_argument("--resume", metavar="ID")
    parser.add_argument("--export", metavar="ID", dest="export_id")
    args = parser.parse_args()

    config = load_config(config_path=args.config)
    agent = build_agent(config, mode_override=args.mode)
    store = SessionStore(root=Path(config.session.root))

    # ...rest of main() handles --continue, --resume, --export as before...
    await repl(agent, store)
```

---

## 5. Create `.env` for secrets

```bash
# .env  — NOT committed to git (add to .gitignore)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Override any config setting without editing agent.toml:
AGENT_MODE_DEFAULT=plan
AGENT_MODEL_NAME=gpt-4o-mini
```

```bash
# Load before running
source .env && python main.py

# Or use python-dotenv (pip install python-dotenv):
# Add to main.py:
from dotenv import load_dotenv; load_dotenv()
```

---

## 6. Run with different configs

```bash
# Default
python main.py

# Override model on the fly via env
AGENT_MODEL_NAME=gpt-4o-mini python main.py

# Use a project-specific config
python main.py --config ./my-project/agent.toml

# PLAN mode for a safe inspect-only session
python main.py --mode plan
```

---

## 7. Checklist before moving on

- [ ] `agent.toml` covers model, session, memory, skills, plugins, permissions, mode, sandbox, compaction
- [ ] `load_config()` searches for `agent.toml` starting in cwd and walking up parents
- [ ] `_apply_env_overrides()` applies `AGENT_{SECTION}_{KEY}` vars on top of file values
- [ ] API keys come from environment variables — never from `agent.toml`
- [ ] `build_agent()` accepts `AgentConfig` instead of individual parameters
- [ ] `tomllib` is used (Python 3.11+) with fallback to `tomli` for 3.10
- [ ] `.env` is in `.gitignore`
- [ ] Hard-coded guardrail deny lists remain in Python code — not in config

---

Next: [14-testing-the-harness.md](14-testing-the-harness.md) — write tests for every component you have built.

