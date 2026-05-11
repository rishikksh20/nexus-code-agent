# 05. Agentic AI System Configuration and Environmental Context Management: Multi-Source Config Loading, Runtime Environment Setup, and Deployment-Ready Agent Bootstrap

This document is a continuation of:

- `docs/01-agentic-ai-system-basics.md`
- `docs/02-agentic-ai-system-runtime-and-error-paths.md`
- `docs/03-agentic-ai-system-context-management-and-prompt-construction.md`
- `docs/04-agentic-ai-system-tool-calling-and-execution-runtime.md`

`01` established the client and internal event basics.
`02` introduced runtime layering (`CLI -> Agent -> TUI`) and agent-level event routing.
`03` added managed context, prompt construction, and token-aware utilities.
`04` explained tool schemas, tool execution, and tool-aware runtime rendering.
`05` adds system-level configuration management, multi-source config loading, environment setup, and runtime bootstrap infrastructure.

In this stage, the code adds:

- a comprehensive Pydantic-based configuration schema,
- multi-source configuration loading (system-level, project-level, environment),
- structured environment variable management and loading,
- configuration validation with clear error reporting,
- lifecycle hook infrastructure for extensible runtime behavior,
- approval policies for agent decision autonomy,
- MCP (Model Context Protocol) server configuration,
- and runtime bootstrap orchestration.

---

## 1. High-level change in this iteration

The project shifts from a **hardcoded single-environment runtime** to a **multi-source, multi-environment-aware, deployment-ready agent system**.

Previous effective flow (from `04`):

`main.py (CLI)` -> `Agent` -> `ContextManager` -> `LLMClient` (+ tools) -> provider -> `AgentEvent` -> `TUI`

Current flow (this `05` step):

`.env (environment)` -> `config loader` -> `Config object` -> `main.py (CLI)` -> `Agent` -> `ContextManager` -> `LLMClient` (+ tools) -> provider -> `AgentEvent` -> `TUI`

That is a structural shift in initialization order. The runtime is no longer bootstrapped with only what was hardcoded at application start. It is now bootstrapped from multiple configuration sources: system defaults, project-specific overrides, environment variables, and loaded secret files.

---

## 2. Change scope since the previous commit

### New packages/modules introduced

- `core/config/__init__.py`
- `core/config/config.py`
- `core/config/loader.py`
- `core/utils/errors.py` (enhanced with `ConfigError`)

### Existing files updated

- `main.py`
- `core/agent/agent.py` (now takes `Config` parameter)
- `pyproject.toml` (new dependencies)

### New files in workspace

- `.env` (environment file for local development)

### Center of gravity in this commit

The center of gravity is the introduction of a **configuration management layer** that bridges between the external environment and the internal runtime.

If `04` was about *what actions the model can request*, then `05` is about *how the runtime environment is constructed, validated, and made flexible across different deployment contexts*.

---

## 3. Architectural delta: from bootstrapped constants to environment-driven configuration

### 3.1 Prior state (`04` baseline)

At the end of `04`, the system had:

- a hard-wired model name in the client,
- API keys read directly from environment variables in the client,
- working directory assumed to be `Path.cwd()`,
- max turns set to an implicit constant,
- no project-specific configuration capability,
- no approval/policy system for agent decisions.

The architecture was functional but assumed a single deployment context.

### 3.2 Current state (`05`)

The system now has:

- a `Config` object that owns all runtime parameters,
- a `ConfigLoader` that reads from multiple sources (system, project, environment),
- explicit validation of configuration before agent startup,
- environment variable loading via optional `.env` files,
- typed configuration with Pydantic validation and error messages,
- approval policies, hooks, tool constraints, and MCP server definitions,
- clear separation between configuration-load time and runtime time.

This means the same codebase can now operate correctly in:

- local development (`.env` + project config),
- CI/CD environments (system config + environment variables),
- containerized deployments (mounted config files + environment secrets),
- multi-user shared systems (user-level system config + project overrides).

---

## 4. Big-picture runtime model after `05`

A deployment now conceptually unfolds like this:

1. the shell or container loads `.env` file (if present),
2. the `load_config(cwd)` function searches for and merges:
   - system-level config from `~/.config/ai-agent/config.toml`,
   - project-level config from `.ai-agent/config.toml`,
   - optional `AGENT.md` developer instructions,
3. the loaded dictionaries are merged with project overriding system,
4. the merged dictionary is validated via Pydantic `Config` model,
5. the `Config` object is instantiated and validated for completeness,
6. the `CLI` receives the `Config` and uses it to initialize the `Agent`,
7. the `Agent` uses `Config` for model name, working directory, tool constraints, hooks, policy,
8. any configuration errors halt the program with clear messaging before the agent runs.

That is a meaningful pre-flight checklist for agent runtimes, because configuration failures are caught early and reported clearly rather than surfacing as runtime errors deep in a tool execution.

---

## 5. Configuration schema and validation (`core/config/config.py`)

`core/config/config.py` defines the complete runtime configuration contract using Pydantic.

This file is critical because it establishes the **system-wide configuration vocabulary** and ensures all parameters are typed and validated.

### 5.1 `ModelConfig`

```python
class ModelConfig(BaseModel):
    name: str = "mistral-medium-latest"
    temperature: float = Field(default=1, ge=0.0, le=2.0)
    context_window: int = 256_000
```

This groups all LLM-specific settings:

- `name`: the model identifier passed to the provider,
- `temperature`: sampling temperature (validated to be in `[0.0, 2.0]`),
- `context_window`: the provider's context limit in tokens.

Before `05`, these were scattered or hardcoded. Now they are:

- type-safe,
- validated (temperature bounds),
- documented inline,
- easy to override via config files.

### 5.2 `ShellEnvironmentPolicy`

```python
class ShellEnvironmentPolicy(BaseModel):
    ignore_default_excludes: bool = False
    exclude_patterns: list[str] = Field(
        default_factory=lambda: ["*KEY*", "*TOKEN*", "*SECRET*"]
    )
    set_vars: dict[str, str] = Field(default_factory=dict)
```

This defines how the runtime manages shell environment variables passed to tools:

- `ignore_default_excludes`: whether to ignore security-sensitive pattern matching,
- `exclude_patterns`: a list of glob patterns for variables to redact,
- `set_vars`: extra environment variables to inject into tool execution.

This is significant because agent tools need environmental context (PATH, HOME, etc.) but should not leak API keys or secrets. This policy makes that tension explicit and configurable.

### 5.3 `MCPServerConfig`

```python
class MCPServerConfig(BaseModel):
    enabled: bool = True
    startup_timeout_sec: float = 10
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: Path | None = None
    url: str | None = None
    
    @model_validator(mode="after")
    def validate_transport(self) -> MCPServerConfig:
        # Either command (stdio) or url (http/sse), not both
        ...
```

This is configuration for Model Context Protocol servers, which allow the agent to request tools from external processes.

Key design:

- supports two transport modes: stdio (command) and http/sse (url),
- validator ensures exactly one transport is specified,
- allows per-server environment and working directory,
- timeout prevents hanging on server startup.

This shows the architecture is already prepared for pluggable external tool systems beyond the builtin tools.

### 5.4 `ApprovalPolicy` and `HookTrigger`

```python
class ApprovalPolicy(str, Enum):
    ON_REQUEST = "on-request"
    ON_FAILURE = "on-failure"
    AUTO = "auto"
    AUTO_EDIT = "auto-edit"
    NEVER = "never"
    YOLO = "yolo"

class HookTrigger(str, Enum):
    BEFORE_AGENT = "before_agent"
    AFTER_AGENT = "after_agent"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    ON_ERROR = "on_error"
```

These enums define the agent's decision autonomy and extensibility points.

`ApprovalPolicy` controls whether the agent should ask for approval before acting:

- `ON_REQUEST`: ask for every tool call,
- `ON_FAILURE`: ask only if a tool fails,
- `AUTO`: run tools without asking,
- `AUTO_EDIT`: auto-approve writing tools only,
- `NEVER`: prevent tool execution,
- `YOLO`: whatever.

`HookTrigger` defines lifecycle points where users can attach custom scripts or commands to run before/after agent steps or on errors.

### 5.5 `HookConfig`

```python
class HookConfig(BaseModel):
    name: str
    trigger: HookTrigger
    command: str | None = None
    script: str | None = None
    timeout_sec: float = 30
    enabled: bool = True
    
    @model_validator(mode="after")
    def validate_hook(self) -> HookConfig:
        if not self.command and not self.script:
            raise ValueError("Hook must either have 'command' or 'script'")
        return self
```

This allows users to define custom actions attached to agent lifecycle events.

A hook can be:

- a shell command (e.g., `"python tests.py"`),
- a script file (e.g., `"./monitor.sh"`),
- with a timeout and enabled/disabled flag.

This is infrastructure for:

- monitoring/observability,
- automated testing on agent decisions,
- pause/resume workflows,
- approval flows via external systems.

### 5.6 The main `Config` class

```python
class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    cwd: Path = Field(default_factory=Path.cwd)
    shell_environment: ShellEnvironmentPolicy = Field(
        default_factory=ShellEnvironmentPolicy
    )
    hooks_enabled: bool = False
    hooks: list[HookConfig] = Field(default_factory=list)
    approval: ApprovalPolicy = ApprovalPolicy.ON_REQUEST
    max_turns: int = 100
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    allowed_tools: list[str] | None = None
    developer_instructions: str | None = None
    user_instructions: str | None = None
    debug: bool = False
```

This is the unified configuration object that the entire runtime uses.

Key aspects:

- every subsystem (model, shell, hooks, tools) has explicit config,
- defaults are sensible for development,
- structural validation happens via Pydantic,
- the object is immutable after construction (Pydantic dataclass pattern).

### 5.7 Properties and environment variable bridging

```python
@property
def api_key(self) -> str | None:
    return os.environ.get("API_KEY")

@property
def base_url(self) -> str | None:
    return os.environ.get("BASE_URL")

@property
def model_name(self) -> str:
    return self.model.name
```

These properties bridge:

- configuration state (stored in `self.model`),
- and environment state (read from `os.environ`).

This is important because secrets (API keys) should always be loaded from environment, never from config files. But non-secret config should be loadable from files for reproducibility.

### 5.8 Validation contract

```python
def validate(self) -> list[str]:
    errors: list[str] = []
    if not self.api_key:
        errors.append("No API key found. Set API_KEY environment variable")
    if not self.cwd.exists():
        errors.append(f"Working directory does not exist: {self.cwd}")
    return errors
```

This method allows the runtime to check preconditions before starting the agent.

It returns a list of errors rather than raising exceptions, which allows the CLI to collect and report all problems at once.

---

## 6. Multi-source configuration loading (`core/config/loader.py`)

Once the configuration schema is defined, the runtime needs a way to construct `Config` objects from files and environment.

`core/config/loader.py` handles that responsibility.

### 6.1 Platform-aware config directories

```python
def get_config_dir() -> Path:
    return Path(user_config_dir("ai-agent"))

def get_data_dir() -> Path:
    return Path(user_data_dir("ai-agent"))
```

This uses `platformdirs` to find the right config location for each OS:

- on Linux: `~/.config/ai-agent/`,
- on macOS: `~/Library/Application Support/ai-agent/`,
- on Windows: `%APPDATA%\ai-agent\`.

This is much better than hardcoding `~/.config` because it respects platform conventions.

### 6.2 TOML parsing

```python
def _parse_toml(path: Path):
    try:
        with open(path, "rb") as f:
            return tomli.load(f)
    except tomli.TOMLDecodeError as e:
        raise ConfigError("Invalid TOML in {path}: {e}", config_file=str(path)) from e
    except (OSError, IOError) as e:
        raise ConfigError(
            "Failed to read config file {path}: {e}", config_file=str(path)
        ) from e
```

Configuration is stored in TOML format because it is:

- human-readable and easy to edit,
- structured (better than flat env vars),
- supported by Python standard library (via `tomli` for Python <3.11),
- widely used in Rust/Python projects for configuration.

Error handling is explicit: TOML parse errors and file I/O errors are wrapped in `ConfigError` with context.

### 6.3 Project config discovery

```python
def _get_project_config(cwd: Path) -> Path | None:
    current = cwd.resolve()
    agent_dir = current / ".ai-agent"
    if agent_dir.is_dir():
        config_file = agent_dir / CONFIG_FILE_NAME
        if config_file.is_file():
            return config_file
    return None
```

The loader searches for a `.ai-agent/config.toml` file in the working directory.

This enables project-specific overrides:

- system config: shared defaults for all projects,
- project config: this project's specific settings.

Example use case:

- system config sets `max_turns = 100`,
- a particular project needs `max_turns = 50`,
- the project drops `.ai-agent/config.toml` with `max_turns = 50`.

### 6.4 Developer instructions from markdown

```python
def _get_agent_md_files(cwd: Path) -> Path | None:
    current = cwd.resolve()
    if current.is_dir():
        agent_md_file = current / AGENT_MD_FILE
        if agent_md_file.is_file():
            content = agent_md_file.read_text(encoding="utf-8")
            return content
    return None
```

The loader also searches for an `AGENT.md` file in the project root and loads its content as `developer_instructions`.

This allows projects to document their agent guidelines in markdown:

```markdown
# Agent Guidelines for MyProject

This agent is responsible for:
- Reading and analyzing source code
- Running tests
- Suggesting improvements

It should NOT:
- Delete files
- Access the database directly
```

That content is then available to the system prompt and influences agent behavior.

### 6.5 Configuration merging

```python
def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result
```

System config and project config are merged with **project overriding system**.

This uses recursive merging so that:

- system defines `[model]` section with `name = "openai"`,
- project can define just `[model]` with `temperature = 0.5`,
- the result has both settings merged.

### 6.6 The main loading orchestration

```python
def load_config(cwd: Path | None) -> Config:
    cwd = cwd or Path.cwd()
    
    system_path = get_system_config_path()
    config_dict: dict[str, Any] = {}
    
    if system_path.is_file():
        try:
            config_dict = _parse_toml(system_path)
        except ConfigError:
            logger.warning(f"Skipping invalid system config: {system_path}")
    
    project_path = _get_project_config(cwd)
    if project_path:
        try:
            project_config_dict = _parse_toml(project_path)
            config_dict = _merge_dicts(config_dict, project_config_dict)
        except ConfigError:
            logger.warning(f"Skipping invalid system config: {system_path}")
    
    if "cwd" not in config_dict:
        config_dict["cwd"] = cwd
    
    if "developer_instructions" not in config_dict:
        agent_md_content = _get_agent_md_files(cwd)
        if agent_md_content:
            config_dict["developer_instructions"] = agent_md_content
    
    try:
        config = Config(**config_dict)
    except Exception as e:
        raise ConfigError(f"Invalid configuration: {e}") from e
    
    return config
```

The full loading sequence:

1. start with empty dict,
2. try to load system config (warn if fails),
3. try to load project config (merge, warn if fails),
4. auto-inject `cwd` if not in config,
5. auto-inject `AGENT.md` content if not in config,
6. construct `Config` object (validate with Pydantic),
7. raise `ConfigError` if validation fails.

This design is graceful:

- system config is optional,
- project config is optional,
- missing files don't crash (only warn),
- invalid TOML in optional files is skipped,
- invalid schema (Pydantic validation) is fatal and reported clearly.

---

## 7. Environment variable loading (`main.py` changes)

Before `05`, the runtime had no explicit environment setup phase.

Now `main.py` includes:

```python
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

def _load_env_file(cwd: Path | None) -> None:
    if load_dotenv is None:
        return
    base_dir = cwd or Path.cwd()
    load_dotenv(dotenv_path=base_dir / ".env")
```

This:

- optionally imports `python-dotenv` (not required),
- loads `.env` file from the working directory if it exists,
- does nothing if dotenv is not installed (for containerized/preset-env setups).

Example `.env`:

```bash
API_KEY=sk-test-abc123
BASE_URL=https://api.example.com
```

This enables:

- local development (`.env` holds local API key),
- CI/CD (environment variables already set in CI system),
- containerized deployment (secrets mounted as env vars),
- secure secrets management (`.env` is in `.gitignore`).

---

## 8. Bootstrap orchestration in main (`main.py` restructure)

The CLI initialization has been restructured to build the config before creating the CLI:

```python
def main(prompt: str|None, cwd: Path|None) -> None:
    _load_env_file(cwd)  # 1. Load .env
    
    try:
        config = load_config(cwd=cwd)  # 2. Build Config
    except Exception as e:
        console.print(f"[error]Config error: {e}[\\error]")
        sys.exit(1)
    
    errors = config.validate()  # 3. Validate Config
    if errors:
        for error in errors:
            console.print(f"[error]Config File: {error}[\\error]")
        sys.exit(1)
    
    cli = CLI(config)  # 4. Create CLI with Config
    
    if prompt:
        result = asyncio.run(cli.run_single(prompt))
    else:
        asyncio.run(cli.run_interactive())
```

This is a clear pre-flight phase:

1. **environment loading** (secrets),
2. **config construction** (from files and environment),
3. **config validation** (pre-flight checks),
4. **CLI creation** (with validated config),
5. **agent execution** (guaranteed to have valid runtime state).

This pattern prevents the agent from starting in an invalid or incomplete state.

---

## 9. CLI and Agent initialization with Config

Prior to `05`, the `CLI` and `Agent` had implicit defaults.

Now both receive the `Config` object:

```python
class CLI:
    def __init__(self, config: Config) -> None:
        self.agent: Agent|None = None
        self.tui = TUI(config, console)
        self.config = config
```

and:

```python
class Agent:
    def __init__(self, config: Config):
        self.config = config
        self.client: LLMClient = LLMClient(config)
        self.context_manager = ContextManager(config)
        self.tool_registry = create_default_registry()
```

This means:

- `Agent` can read `config.model.name` for the model,
- `Agent` can read `config.max_turns` for the loop limit,
- `Agent` can read `config.cwd` for tool working directory,
- `Agent` can read `config.allowed_tools` to filter available tools,
- `Agent` can read `config.approval` to decide on approval flows,
- `Agent` can read `config.hooks` to trigger lifecycle actions.

The `Config` object becomes the single source of truth for runtime parameters.

---

## 10. Configuration as system-level substrate

`Config` is now the foundational layer that the agent system is built on.

```
[Environment / Secrets (.env)]
         ↓
[Configuration Files (system + project)]
         ↓
[Config Object (pydantic validated)]
         ↓
[Agent Runtime (uses config)]
```

Every component of the runtime can now be tuned without code changes:

- change `model.name` → uses different LLM,
- change `max_turns` → limits reasoning steps,
- change `cwd` → changes working directory for tools,
- change `allowed_tools` → restricts capabilities,
- change `approval` → changes decision autonomy,
- change `hooks` → adds monitoring/testing,
- add MCP servers → adds external tool capabilities.

This is a major architectural shift from **code-driven configuration** to **data-driven configuration**.

---

## 11. Error handling improvements (`core/utils/errors.py`)

The error handling layer has been enhanced to support configuration errors specifically:

```python
class ConfigError(AgentError):
    def __init__(
        self,
        message: str,
        config_key: str | None = None,
        config_file: str | None = None,
        **kwargs: Any,
    ) -> None:
        details = kwargs.pop("details", {}) or {}
        if config_key:
            details["config_key"] = config_key
        if config_file:
            details["config_file"] = config_file
        super().__init__(message, details=details, **kwargs)
```

`ConfigError` enriches error reporting with:

- `config_key`: which configuration setting failed,
- `config_file`: which file had the problem,
- `message`: what went wrong,
- `cause`: the underlying exception.

Example error output:

```
ConfigError: Invalid TOML in /home/user/.config/ai-agent/config.toml: ... (config_file=/home/user/.config/ai-agent/config.toml)
```

This makes debugging configuration issues much easier.

---

## 12. Dependency additions (`pyproject.toml`)

Three new dependencies have been added:

```toml
"platformdirs>=4.9.6",    # OS-aware config directory discovery
"pydantic>=2.13.3",        # Configuration schema validation (already there)
"tomli>=2.4.1",            # TOML parsing for config files
```

Note: `python-dotenv` is **not** listed as a required dependency. It is imported with a try/except in `main.py`, making it optional.

This design supports:

- production containers where secrets are already in environment,
- development where `.env` is convenient,
- systems where neither is available (CI with explicit env vars).

---

## 13. End-to-end agent initialization lifecycle after this commit

Deploying an agent now unfolds as:

1. **Environment Setup**:
   - Container or shell loads `.env` (if present),
   - secrets are set in environment variables.

2. **Configuration Discovery**:
   - `load_config(cwd)` searches system `~/.config/ai-agent/config.toml`,
   - searches project `.ai-agent/config.toml`,
   - merges with project overriding system.

3. **Configuration Enrichment**:
   - auto-injects `cwd` if not specified,
   - auto-injects `AGENT.md` as developer instructions.

4. **Configuration Construction**:
   - Pydantic validates merged dict against `Config` schema,
   - all types, ranges, and structural rules are checked.

5. **Pre-flight Validation**:
   - `config.validate()` checks critical runtime preconditions,
   - reports all errors at once (not one-at-a-time),
   - exits with `sys.exit(1)` if any error.

6. **CLI Creation**:
   - `CLI(config)` receives the validated config,
   - passes it to `TUI` for rendering context,
   - holds it for `Agent` creation.

7. **Agent Execution**:
   - `Agent(config)` uses config for model, tools, limits,
   - `ContextManager(config)` uses config if needed,
   - `LLMClient(config)` uses config for model/endpoint.

8. **Tool Execution Within Context**:
   - tools run with `config.cwd` as working directory,
   - shell environment is filtered per `config.shell_environment` policy,
   - hooks (if enabled) are triggered per configuration.

Compared to `04`, the major change is the **addition of an initialization phase** that ensures runtime state is valid before any agent work begins.

---

## 14. Conceptual progression from `04` to `05`

`04` introduced the tool runtime. `05` introduces the configuration infrastructure that makes the tool runtime deployable and tunable.

So the progression now looks like this:

1. **`01`**: client/event fundamentals
2. **`02`**: runtime/event routing and UI shell
3. **`03`**: context management and prompt construction
4. **`04`**: tool schemas, tool execution, and tool-aware runtime rendering
5. **`05`**: configuration management, environment setup, and deployment-ready bootstrap

That is a natural progression toward an agent system that is:

- **event-driven** (from `01` and `02`),
- **context-aware** (from `03`),
- **action-capable** (from `04`),
- **deployable** (from `05`).

---

## 15. Big-picture significance

This commit marks the transition from a **prototype** to a **deployable system**.

With `04`, the system had the core agent loop and tool execution. With `05`, it gains the infrastructure needed for:

- **multi-environment operation** (dev/staging/prod),
- **configuration management** (without code changes),
- **secret handling** (separate from config),
- **extensibility** (hooks, MCP servers),
- **policy control** (approval, tool restrictions),
- **observability** (debug flags, logging context),
- **operational safety** (pre-flight validation, error reporting).

That does not mean it is fully production-ready yet. But it now contains several core requirements that real deployed agents need:

- environment-aware startup,
- configuration validation before execution,
- pluggable tool systems,
- policy-driven decision making,
- clear error messages for operational problems.

In short, `05` is the point where the system moves from "working prototype" to "deployment-ready framework."

---

## 16. Important code-level nuances and implications

### 16.1 Configuration object is immutable after construction

Once a `Config` object is created via Pydantic, its fields cannot be changed at runtime. This is intentional:

- prevents accidental mutation,
- makes the configuration contract explicit,
- simplifies reasoning about runtime state.

If runtime policy changes are needed, they should be handled through:

- file changes (reload via new agent),
- environment variable changes (reload via new agent),
- explicit re-initialization,
- not mutation of the `Config` object itself.

### 16.2 Defaults are sensible for development but not production

Many configuration fields have defaults suitable for experimentation:

- `max_turns = 100` (generous for exploration),
- `approval = ApprovalPolicy.ON_REQUEST` (safe for testing),
- `debug = False` (can be overridden).

Production deployments should explicitly override these to be conservative.

### 16.3 Environment variables always override config files

The design ensures:

```python
@property
def api_key(self) -> str | None:
    return os.environ.get("API_KEY")
```

This means `API_KEY` is **always** read from environment, never from config files. This is intentional:

- secrets should never be stored in config file repos,
- environment is the canonical source for secrets,
- config file can be checked into version control.

### 16.4 Configuration loading is permissive but validation is strict

The loading phase is forgiving:

- missing system config file → fine, log warning,
- bad TOML in system config → fine, skip it,
- missing project config → fine, proceed.

But validation is strict:

- missing `API_KEY` → fatal,
- invalid working directory → fatal,
- Pydantic type errors → fatal.

This balance is good because:

- the system can start in many environments,
- but critical preconditions are always checked.

### 16.5 Project config can be checked into version control

The `.ai-agent/config.toml` file is **not** a secret. It should be committed to the project repo.

Only `.env` (or environment variables) should contain secrets.

Example structure:

```
project/
├── .ai-agent/
│   └── config.toml          # Commit this
├── .env                      # Don't commit this
└── AGENT.md                  # Commit this
```

---

## 17. Delta summary table (`04` -> current)

| Area | `04` baseline | Current (`05`) delta |
|---|---|---|
| Configuration source | Hardcoded defaults and env vars | Multi-source loading (system + project + env) |
| Configuration object | Implicit (scattered across code) | Explicit `Config` object (Pydantic validated) |
| Model settings | `LLMClient` hardcoded assumptions | `ModelConfig` schema with typed fields |
| Working directory | `Path.cwd()` implicit | `Config.cwd` explicit and validated |
| Environment management | Ad-hoc env var reading | `ShellEnvironmentPolicy` with redaction rules |
| Tool restrictions | None | `allowed_tools` list in config |
| Approval policy | Not configurable | `ApprovalPolicy` enum (ON_REQUEST, AUTO, etc.) |
| Lifecycle hooks | Not available | `HookConfig` with before/after/error triggers |
| MCP servers | Not available | `MCPServerConfig` for external tools |
| Pre-flight validation | None | `config.validate()` with explicit error list |
| Startup sequence | Direct Agent creation | Config → Validate → CLI → Agent creation |
| Secrets handling | Environment variables only | Explicit `.env` loading + distinguished from config |
| Dependencies | `click`, `openai`, `pydantic`, `tiktoken` | Added `platformdirs`, `tomli` |

---

## 18. Continuation pointer for next document

Natural next-step topics for `06` would be:

- integrating configuration into `ContextManager` (model name for tokenization, developer instructions in prompts),
- implementing approval flow orchestration (pausing before tool execution if `approval` policy requires),
- integrating hook execution into the agent lifecycle,
- adding more sophisticated environment variable management for tools,
- introducing MCP server client and integration,
- adding tool filtering via `allowed_tools`,
- implementing configuration reload/hot-update patterns,
- adding configuration schema export for documentation and tooling.

That would complete the transition from:

- **configuration management present**

to:

- **deeply integrated configuration-driven agent behavior**.

---

## 19. Key takeaways

1. **Configuration is now explicitly managed** via Pydantic, not scattered across code.

2. **Multi-source loading** enables code reuse across different deployment contexts (dev, CI, production).

3. **Environment separation** (`.env` for secrets, files for config) is now architectural best practice in this codebase.

4. **Pre-flight validation** prevents the agent from starting in an invalid state.

5. **Typed configuration schema** (via Pydantic) enables:
   - documentation,
   - validation,
   - IDE autocompletion,
   - schema export for tooling.

6. **The Config object** becomes the **single source of truth** for all runtime parameters, replacing scattered defaults and implicit assumptions.

7. **Graceful degradation** in loading (optional files, optional dependencies) supports multiple deployment scenarios.

8. This is the foundation for truly **deployable, tunable, policy-driven agent systems**.

