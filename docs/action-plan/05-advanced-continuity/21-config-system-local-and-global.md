# Chapter 21: Config System — Local And Global Parameters

## Objective

Design a two-tier configuration system where a global user-level file sets personal defaults and a per-workspace local file overrides only what the project needs. On top of both, environment variables and CLI flags have final say.

This chapter defines every supported parameter, explains where each tier lives, shows the merge order, and provides a `ConfigLoader` that resolves the final merged config for the harness to use.

## Why Two Tiers Matter

A single config file always ends up being either too broad or too narrow:

- Too broad: workspace-specific settings (allowed tools, project name, local skill dirs) pollute a global file that is shared across dozens of projects.
- Too narrow: a per-workspace file makes you repeat model name, API settings, and personal preferences in every project you create.

The solution is the same one Git, npm, and most serious CLI tools use: a global defaults file in the home directory and a local override file in the project directory. The two layers are merged at startup.

## File Locations

```text
~/.agent/
  config.toml          ← global: your personal defaults across all projects

{workspace}/
  .agent/
    config.toml        ← local: project-specific overrides
    sessions/
    memory/
    knowledge.md
```

The local file is optional. If it does not exist, the harness uses global defaults plus built-in fallbacks. If the global file does not exist either, the harness boots with built-in defaults and warns once.

## Current Nexus Notes

The current Nexus runtime uses `.nexus/` and `~/.nexus/` rather than `.agent/`, but keeps the same two-tier model described here. It also now relies on this layout for additional continuity state:

- `.nexus/facts.json`
- `.nexus/audit-trail.jsonl`
- `~/.nexus/profile.md`
- `~/.nexus/workspaces.json`

Skills are loaded from built-ins, configured `skill_paths`, global `~/.nexus/skills/`, local `.nexus/skills/`, and `.agents/skills/` in priority order (last wins on name collision). The system prompt includes skill metadata only; activation is controlled by config or run-only `--skill`.

For live provider calls, Nexus uses `provider`, `model_name`, and `api_base_url` from the merged config. `mistral` is the default provider with `mistral-medium-latest` and `https://api.mistral.ai/v1` as built-in defaults. Provider auth is resolved from environment variables (`MISTRAL_API_KEY` → `NEXUS_API_KEY` → `OPENAI_API_KEY`); the `api_key` config field is also supported for cases where an env var is not preferred.

The config merge now uses **six** layers rather than five:

```
built-in defaults
  ↓
global ~/.nexus/config.toml
  ↓
local .nexus/config.toml
  ↓
.env file in workspace root  ← new; parsed at startup before env-var lookup
  ↓
AGENT_* environment variables
  ↓
CLI flags
```

The `.env` file is loaded from the workspace root at startup. Its values take priority over the system environment for the same keys, making it easy to store `MISTRAL_API_KEY` per-project without exporting it globally.

The current Nexus runtime validates provider configuration eagerly: supported values are `fake`, `mistral`, `openai`, and `openai-compatible`. `mistral` auto-sets `api_base_url` to `https://api.mistral.ai/v1`; other live providers require `api_base_url` to be set explicitly before startup succeeds.

Compaction limits (`compaction_soft_limit`, `compaction_hard_limit`) are no longer static defaults. At startup, Nexus looks up the active model in a built-in model-limits table and sets soft to 65% and hard to 85% of that model's context window. User-provided values in any config layer are respected and never overwritten by the auto-tuning logic.

A `/config reinit [local|global]` slash command is now available inside the REPL. It rewrites the target config file (`.nexus/config.toml` for `local`, `~/.nexus/config.toml` for `global`) to clean Nexus defaults — provider `mistral`, standard model and settings — and immediately reloads the merged config. Sessions, memory, knowledge, and audit trails are not affected. This replaces manual file editing when a config becomes corrupted or stale.

## Merge Order

Settings are resolved in this priority chain, from lowest to highest:

```
built-in defaults
  ↓ overridden by
global ~/.agent/config.toml
  ↓ overridden by
local {workspace}/.agent/config.toml
  ↓ overridden by
environment variables (AGENT_*)
  ↓ overridden by
CLI flags (--model, --mode, --no-stream, ...)
```

Higher layers can only override; they cannot delete or restrict keys that a lower layer set. Safety boundaries (hard-deny policy, sandbox rules) are enforced in code and are not configurable through any tier.

---

## Global Config: All Parameters

File: `~/.agent/config.toml`

```toml
# ─────────────────────────────────────────
# Provider and Model
# ─────────────────────────────────────────

# Which LLM provider adapter to use.
# Built-in values: "openai", "anthropic", "fake"
provider = "openai"

# Model identifier passed to the provider.
model_name = "gpt-4o-mini"

# Base URL for the provider API. Override for local models (e.g. Ollama).
# Default: "" (uses the provider SDK default)
api_base_url = ""

# Maximum output tokens per model call.
max_output_tokens = 4096

# Sampling temperature. 0.0 = deterministic, 1.0 = creative.
temperature = 0.2


# ─────────────────────────────────────────
# Context
# ─────────────────────────────────────────

# Soft token limit. Compaction starts when this is reached.
compaction_soft_limit = 10000

# Hard token limit. Turns above this are always truncated.
compaction_hard_limit = 14000

# Number of recent messages to keep intact during compaction.
compaction_keep_recent = 12


# ─────────────────────────────────────────
# Execution
# ─────────────────────────────────────────

# Default execution mode. Values: "plan", "default", "auto"
default_mode = "default"

# Allow read-only tools without confirmation even in default mode.
auto_confirm_read_only = true

# Maximum agent loop iterations before raising a safety error.
max_loop_iterations = 40


# ─────────────────────────────────────────
# UI / Output
# ─────────────────────────────────────────

# Stream output tokens to the terminal as they arrive.
stream_output = true

# Show tool call names and arguments inline in the REPL.
show_tool_calls = true

# Show a thinking indicator while the model is processing.
show_thinking_indicator = true

# Emit ANSI color codes in terminal output.
color_output = true


# ─────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────

# Number of recent sessions to keep before pruning.
max_sessions_retained = 50

# Save session state after every turn (vs. only at exit).
save_on_every_turn = true


# ─────────────────────────────────────────
# Skills
# ─────────────────────────────────────────

# Global skills directory. Skills here are available in all projects.
skills_dir = "~/.agent/skills"


# ─────────────────────────────────────────
# Plugins
# ─────────────────────────────────────────

# Global plugin directory.
plugins_dir = "~/.agent/plugins"


# ─────────────────────────────────────────
# Memory
# ─────────────────────────────────────────

# Global memory directory for user-scoped memory entries.
memory_dir = "~/.agent/memory"


# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────

# Log level. Values: "DEBUG", "INFO", "WARNING", "ERROR"
log_level = "INFO"

# Log format. Values: "json", "text"
log_format = "json"

# Directory for log files. Empty string means log to stderr only.
log_dir = "~/.agent/logs"


# ─────────────────────────────────────────
# Post-Session Hooks
# ─────────────────────────────────────────

# Update ~/.agent/profile.md with session learnings at session end.
update_profile_on_session_end = true
```

---

## Local Config: All Parameters

File: `{workspace}/.agent/config.toml`

The local file only needs to contain keys that differ from the global defaults. Any key not present here falls through to the global value.

```toml
# ─────────────────────────────────────────
# Project Identity
# ─────────────────────────────────────────

# Human-readable project name, used in context and knowledge files.
project_name = "my-project"

# Short description injected into the base context.
project_description = "A Python web service that handles order processing."


# ─────────────────────────────────────────
# Provider and Model (workspace overrides)
# ─────────────────────────────────────────

# Override the model for this workspace only.
# model_name = "gpt-4o"


# ─────────────────────────────────────────
# Execution (workspace overrides)
# ─────────────────────────────────────────

# Override the default mode for this project.
default_mode = "default"

# Hard-restrict which tools can run in this project.
# Empty list means all registered tools are allowed.
allowed_tools = ["read_file", "search_memory", "get_time", "write_note"]

# Tools that are always denied in this project regardless of mode.
denied_tools = ["run_command"]

# Sandbox shell commands in Docker. Requires Docker to be running.
sandbox_commands = false


# ─────────────────────────────────────────
# Context
# ─────────────────────────────────────────

# Cap context tokens lower for cheaper or faster models.
# compaction_soft_limit = 6000
# compaction_hard_limit = 8000


# ─────────────────────────────────────────
# Paths (workspace-scoped)
# ─────────────────────────────────────────

# Local session storage (defaults to {workspace}/.agent/sessions).
# session_dir = ".agent/sessions"

# Local memory directory (defaults to {workspace}/.agent/memory).
# memory_dir = ".agent/memory"

# Local knowledge file (defaults to {workspace}/.agent/knowledge.md).
# knowledge_file = ".agent/knowledge.md"


# ─────────────────────────────────────────
# Skills (workspace-scoped)
# ─────────────────────────────────────────

# Local skills directory. Skills here merge with global skills.
# Local skills take precedence when names collide.
skills_dir = ".agent/skills"


# ─────────────────────────────────────────
# Plugins (workspace-scoped)
# ─────────────────────────────────────────

# Local plugin directory.
plugins_dir = ".agent/plugins"


# ─────────────────────────────────────────
# Post-Session Hooks (workspace overrides)
# ─────────────────────────────────────────

# Update .agent/knowledge.md with session learnings at session end.
update_knowledge_on_session_end = true
```

---

## Environment Variables

Any config key can be overridden with an `AGENT_` prefixed environment variable. The naming rule is: `AGENT_` + uppercase key with dots replaced by underscores.

| Key | Environment variable |
|---|---|
| `model_name` | `AGENT_MODEL_NAME` |
| `provider` | `AGENT_PROVIDER` |
| `api_base_url` | `AGENT_API_BASE_URL` |
| `default_mode` | `AGENT_DEFAULT_MODE` |
| `log_level` | `AGENT_LOG_LEVEL` |
| `max_context_tokens` (alias) | `AGENT_MAX_TOKENS` |
| `stream_output` | `AGENT_STREAM_OUTPUT` |
| `max_loop_iterations` | `AGENT_MAX_LOOP_ITERATIONS` |

**API keys are never stored in config files.** Use environment variables only:

```bash
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
```

The harness reads these via the provider adapter, not through `AgentConfig`.

---

## The ConfigLoader

```python
# config/loader.py
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AgentConfig:
    # Provider / model
    provider: str = "openai"
    model_name: str = "gpt-4o-mini"
    api_base_url: str = ""
    max_output_tokens: int = 4096
    temperature: float = 0.2

    # Context
    compaction_soft_limit: int = 10_000
    compaction_hard_limit: int = 14_000
    compaction_keep_recent: int = 12

    # Execution
    default_mode: str = "default"
    auto_confirm_read_only: bool = True
    max_loop_iterations: int = 40

    # UI
    stream_output: bool = True
    show_tool_calls: bool = True
    show_thinking_indicator: bool = True
    color_output: bool = True

    # Sessions
    max_sessions_retained: int = 50
    save_on_every_turn: bool = True

    # Paths (resolved at load time)
    session_dir: Path = field(default_factory=lambda: Path(".agent/sessions"))
    memory_dir: Path = field(default_factory=lambda: Path(".agent/memory"))
    knowledge_file: Path = field(default_factory=lambda: Path(".agent/knowledge.md"))
    skills_dir: Path = field(default_factory=lambda: Path("~/.agent/skills"))
    plugins_dir: Path = field(default_factory=lambda: Path("~/.agent/plugins"))
    log_level: str = "INFO"
    log_format: str = "json"
    log_dir: str = "~/.agent/logs"

    # Tools
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    sandbox_commands: bool = False

    # Project identity
    project_name: str = ""
    project_description: str = ""

    # Post-session
    update_profile_on_session_end: bool = True
    update_knowledge_on_session_end: bool = True


def _load_toml_file(path: Path) -> dict:
    """Return parsed TOML dict or empty dict if file does not exist."""
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _apply_env_overrides(data: dict) -> dict:
    """Overlay environment variables onto a config dict."""
    mapping = {
        "AGENT_PROVIDER": "provider",
        "AGENT_MODEL_NAME": "model_name",
        "AGENT_API_BASE_URL": "api_base_url",
        "AGENT_DEFAULT_MODE": "default_mode",
        "AGENT_LOG_LEVEL": "log_level",
        "AGENT_STREAM_OUTPUT": "stream_output",
        "AGENT_MAX_LOOP_ITERATIONS": "max_loop_iterations",
        # Legacy short alias
        "AGENT_MAX_TOKENS": "compaction_hard_limit",
    }
    result = dict(data)
    for env_key, config_key in mapping.items():
        value = os.getenv(env_key)
        if value is not None:
            # Coerce boolean strings
            if value.lower() in {"true", "1", "yes"}:
                result[config_key] = True
            elif value.lower() in {"false", "0", "no"}:
                result[config_key] = False
            else:
                try:
                    result[config_key] = int(value)
                except ValueError:
                    result[config_key] = value
    return result


def load_config(
    workspace_root: Path | None = None,
    global_root: Path | None = None,
    cli_overrides: dict | None = None,
) -> AgentConfig:
    """
    Merge config from all tiers into a single AgentConfig.

    Priority (lowest → highest):
      built-in defaults → global TOML → local TOML → env vars → cli_overrides
    """
    global_root = global_root or Path.home() / ".agent"
    workspace_root = workspace_root or Path.cwd()

    global_data = _load_toml_file(global_root / "config.toml")
    local_data = _load_toml_file(workspace_root / ".agent" / "config.toml")

    # Merge: global is base, local overrides
    merged = {**global_data, **local_data}
    merged = _apply_env_overrides(merged)

    if cli_overrides:
        merged.update({k: v for k, v in cli_overrides.items() if v is not None})

    # Build typed config from merged dict, falling back to dataclass defaults
    defaults = AgentConfig()
    return AgentConfig(
        provider=merged.get("provider", defaults.provider),
        model_name=merged.get("model_name", defaults.model_name),
        api_base_url=merged.get("api_base_url", defaults.api_base_url),
        max_output_tokens=int(merged.get("max_output_tokens", defaults.max_output_tokens)),
        temperature=float(merged.get("temperature", defaults.temperature)),
        compaction_soft_limit=int(merged.get("compaction_soft_limit", defaults.compaction_soft_limit)),
        compaction_hard_limit=int(merged.get("compaction_hard_limit", defaults.compaction_hard_limit)),
        compaction_keep_recent=int(merged.get("compaction_keep_recent", defaults.compaction_keep_recent)),
        default_mode=merged.get("default_mode", defaults.default_mode),
        auto_confirm_read_only=bool(merged.get("auto_confirm_read_only", defaults.auto_confirm_read_only)),
        max_loop_iterations=int(merged.get("max_loop_iterations", defaults.max_loop_iterations)),
        stream_output=bool(merged.get("stream_output", defaults.stream_output)),
        show_tool_calls=bool(merged.get("show_tool_calls", defaults.show_tool_calls)),
        show_thinking_indicator=bool(merged.get("show_thinking_indicator", defaults.show_thinking_indicator)),
        color_output=bool(merged.get("color_output", defaults.color_output)),
        max_sessions_retained=int(merged.get("max_sessions_retained", defaults.max_sessions_retained)),
        save_on_every_turn=bool(merged.get("save_on_every_turn", defaults.save_on_every_turn)),
        session_dir=Path(merged.get("session_dir", defaults.session_dir)).expanduser(),
        memory_dir=Path(merged.get("memory_dir", defaults.memory_dir)).expanduser(),
        knowledge_file=Path(merged.get("knowledge_file", defaults.knowledge_file)).expanduser(),
        skills_dir=Path(merged.get("skills_dir", defaults.skills_dir)).expanduser(),
        plugins_dir=Path(merged.get("plugins_dir", defaults.plugins_dir)).expanduser(),
        log_level=merged.get("log_level", defaults.log_level),
        log_format=merged.get("log_format", defaults.log_format),
        log_dir=merged.get("log_dir", defaults.log_dir),
        allowed_tools=list(merged.get("allowed_tools", defaults.allowed_tools)),
        denied_tools=list(merged.get("denied_tools", defaults.denied_tools)),
        sandbox_commands=bool(merged.get("sandbox_commands", defaults.sandbox_commands)),
        project_name=merged.get("project_name", defaults.project_name),
        project_description=merged.get("project_description", defaults.project_description),
        update_profile_on_session_end=bool(
            merged.get("update_profile_on_session_end", defaults.update_profile_on_session_end)
        ),
        update_knowledge_on_session_end=bool(
            merged.get("update_knowledge_on_session_end", defaults.update_knowledge_on_session_end)
        ),
    )
```

---

## Initialising Config Directories

Add a helper that ensures all expected directories exist before the harness tries to use them:

```python
def ensure_config_dirs(config: AgentConfig, global_root: Path) -> None:
    dirs = [
        config.session_dir,
        config.memory_dir,
        config.knowledge_file.parent,
        config.skills_dir,
        config.plugins_dir,
        global_root / "skills",
        global_root / "plugins",
        global_root / "memory",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
```

Call this once at startup after `load_config()` returns. Do not scatter `mkdir` calls across subsystems.

---

## Config Parameter Reference Table

| Parameter | Tier | Default | Env override | Description |
|---|---|---|---|---|
| `provider` | global | `"openai"` | `AGENT_PROVIDER` | Provider adapter |
| `model_name` | global/local | `"gpt-4o-mini"` | `AGENT_MODEL_NAME` | Model identifier |
| `api_base_url` | global | `""` | `AGENT_API_BASE_URL` | Override API base (local models) |
| `max_output_tokens` | global | `4096` | — | Max tokens per model call |
| `temperature` | global | `0.2` | — | Sampling temperature |
| `compaction_soft_limit` | global/local | `10000` | `AGENT_MAX_TOKENS` | Token count that triggers compaction |
| `compaction_hard_limit` | global/local | `14000` | — | Hard cap before truncation |
| `compaction_keep_recent` | global/local | `12` | — | Recent messages preserved intact |
| `default_mode` | global/local | `"default"` | `AGENT_DEFAULT_MODE` | Execution mode at startup |
| `auto_confirm_read_only` | global | `true` | — | Skip confirmation for read-only tools |
| `max_loop_iterations` | global | `40` | `AGENT_MAX_LOOP_ITERATIONS` | Safety cap on agent loop |
| `stream_output` | global | `true` | `AGENT_STREAM_OUTPUT` | Stream tokens to terminal |
| `show_tool_calls` | global | `true` | — | Show tool name/args inline |
| `show_thinking_indicator` | global | `true` | — | Show spinner while model works |
| `color_output` | global | `true` | — | ANSI color in output |
| `max_sessions_retained` | global | `50` | — | Sessions kept before pruning |
| `save_on_every_turn` | global | `true` | — | Persist session after each turn |
| `session_dir` | local | `.agent/sessions` | — | Session file directory |
| `memory_dir` | local | `.agent/memory` | — | Memory file directory |
| `knowledge_file` | local | `.agent/knowledge.md` | — | Workspace knowledge file |
| `skills_dir` | global/local | `~/.agent/skills` | — | Skills root directory |
| `plugins_dir` | global/local | `~/.agent/plugins` | — | Plugin root directory |
| `log_level` | global | `"INFO"` | `AGENT_LOG_LEVEL` | Log verbosity |
| `log_format` | global | `"json"` | — | `"json"` or `"text"` |
| `log_dir` | global | `~/.agent/logs` | — | Log file directory |
| `allowed_tools` | local | `[]` (all) | — | Tool allowlist (empty = all) |
| `denied_tools` | local | `[]` | — | Tools always denied |
| `sandbox_commands` | local | `false` | — | Docker sandbox for shell tools |
| `project_name` | local | `""` | — | Human name for this workspace |
| `project_description` | local | `""` | — | Short project description for context |
| `update_profile_on_session_end` | global | `true` | — | Write `~/.agent/profile.md` at close |
| `update_knowledge_on_session_end` | local | `true` | — | Write `.agent/knowledge.md` at close |

---

## Generating A Default Config

Add an `init` command that writes starter files if they do not exist:

```python
DEFAULT_GLOBAL_CONFIG = """\
provider = "openai"
model_name = "gpt-4o-mini"
default_mode = "default"
stream_output = true
log_level = "INFO"
"""

DEFAULT_LOCAL_CONFIG = """\
# project-local overrides — only add keys that differ from your global config
project_name = "{name}"
default_mode = "default"
allowed_tools = []
denied_tools = []
"""


def init_config(workspace_root: Path, global_root: Path) -> None:
    global_cfg = global_root / "config.toml"
    local_cfg = workspace_root / ".agent" / "config.toml"

    if not global_cfg.exists():
        global_root.mkdir(parents=True, exist_ok=True)
        global_cfg.write_text(DEFAULT_GLOBAL_CONFIG, encoding="utf-8")
        print(f"Created global config: {global_cfg}")

    if not local_cfg.exists():
        local_cfg.parent.mkdir(parents=True, exist_ok=True)
        local_cfg.write_text(
            DEFAULT_LOCAL_CONFIG.format(name=workspace_root.name),
            encoding="utf-8",
        )
        print(f"Created local config: {local_cfg}")
```

This is called by `agent init` (see Chapter 23) and by the REPL `/config init` slash command (see Chapter 22).

---

## Action Plan

1. Create `config/loader.py` with `AgentConfig`, `load_config()`, and `ensure_config_dirs()`.
2. Create starter global and local config files using `init_config()`.
3. Call `load_config()` once in `app.py` before constructing any subsystem.
4. Pass the resolved `AgentConfig` to every subsystem that needs it; do not re-read files per subsystem.
5. Never store API keys in config files; read them from environment variables only.
6. Add a test that verifies local values override global values and env vars override both.

## Validation Checklist

- Running with no config files boots with built-in defaults.
- A local `model_name` overrides the global `model_name`.
- `AGENT_DEFAULT_MODE=plan` overrides TOML at any tier.
- `denied_tools` blocks a tool regardless of any other setting.
- `ensure_config_dirs()` creates all directories before any read or write.

## Definition Of Done

This chapter is complete when you can change one workspace's model without affecting any other workspace, and when the whole config resolution path is traceable in one function rather than scattered across subsystems.
