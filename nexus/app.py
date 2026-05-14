"""nexus/app.py — application entry point and runtime orchestrator.

Structure mirrors the reference code's CLI class pattern:

    app = NexusApp(config, console)   # reference: cli = CLI(config)
    await app.initialize(params)      # build registry, agent, resources
    await app.run_single(prompt)      # reference: cli.run_single(message)
    await app.run_interactive()       # reference: cli.run_interactive()
    await app.close()                 # teardown delegation + MCP

Dispatch helpers (_dispatch_runtime, _dispatch_doctor, …) are thin shims
called by the click commands defined in nexus/cli/args.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import click

from nexus import __version__
from nexus.cli.args import args_to_config_overrides
from nexus.cli.doctor import build_doctor_report, exit_code_for_report
from nexus.cli.headless import EXIT_NEEDS_CONFIRM, run_headless
from nexus.cli.init import init_workspace
from nexus.cli.input import resolve_prompt
from nexus.config import config_to_plain_dict, ensure_config_dirs, load_config
from nexus.config.loader import ConfigError
from nexus.config.model_limits import get_model_context_limit
from nexus.extensions.plugins import PluginLoader
from nexus.hooks import HookExecutor, setup_hooks
from nexus.integrations.anthropic import AnthropicModelClient, resolve_anthropic_api_key
from nexus.integrations.fake_model import FakeModelClient
from nexus.integrations.gemini import GeminiModelClient, resolve_gemini_api_key
from nexus.integrations.mcp import MCPServerConfig, MCPServerRuntime, MCPToolAdapter
from nexus.integrations.ollama import OllamaModelClient, resolve_ollama_base_url
from nexus.integrations.openai_compatible import (
    OpenAICompatibleModelClient,
    resolve_provider_api_key,
)
from nexus.memory.store import MemoryStore
from nexus.runtime.agent import Agent
from nexus.runtime.delegation import DelegationRuntime
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.post_session import run_post_session_updates
from nexus.runtime.repl import run_repl
from nexus.runtime.runtime_session import RuntimeSession, resolve_runtime_session
from nexus.runtime.slash_commands import build_router
from nexus.sandbox import register_sandbox_tool
from nexus.sandbox.tool import SandboxedCommandTool
from nexus.tools.base import ToolRegistry
from nexus.tools.registry import register_core_tools, tool_enabled
from nexus.tools.subagents import load_subagent_definitions, register_subagent_tools
from nexus.ui import TerminalUI

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RuntimeResources — groups long-lived async resources that need teardown
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RuntimeResources:
    """Holds async resources (delegation runtime, MCP server connections) that
    must be shut down cleanly when the session ends."""

    mcp_servers: list[MCPServerRuntime] = field(default_factory=list)
    delegation: DelegationRuntime | None = None


# ---------------------------------------------------------------------------
# NexusApp — main runtime orchestrator (analogous to CLI in reference code)
# ---------------------------------------------------------------------------

class NexusApp:
    """Orchestrates the Nexus agent runtime for a single user session.

    Lifecycle (mirrors ``CLI`` in the reference code)::

        app = NexusApp(config, console)
        await app.initialize(load_plugins=True)   # build registry + agent
        try:
            exit_code = await app.run_single(prompt, params)
            # — or —
            exit_code = await app.run_interactive(params)
        finally:
            await app.close()                     # teardown resources

    Parameters
    ----------
    config:
        Fully resolved :class:`AgentConfig` for the current session.
    console:
        Terminal UI instance used for all user-facing output.
    """

    def __init__(self, config, console: TerminalUI) -> None:
        self.config = config
        self.console = console
        # Set by initialize()
        self._registry: ToolRegistry | None = None
        self._resources: RuntimeResources | None = None
        self._agent: Agent | None = None
        self._hooks: HookExecutor | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def initialize(self, *, load_plugins: bool = True) -> None:
        """Build the tool registry, start delegation, connect MCP servers.

        Must be awaited before calling :meth:`run_single` or
        :meth:`run_interactive`.
        """
        self._apply_model_context_limits()
        self._hooks = setup_hooks(self.config)
        self._registry, self._resources = await self._build_registry(
            load_plugins=load_plugins
        )
        self._agent = Agent(
            model_client=self._build_model_client(),
            tool_registry=self._registry,
            hooks=self._hooks,
        )
        self._log_registered_tools()

    async def run_single(self, prompt: str, params: dict) -> int:
        """Run the agent once with *prompt* and return an exit code.

        Equivalent to ``CLI.run_single`` in the reference code.
        """
        runtime_session = self._build_runtime_session(params)
        result = await run_headless(
            runtime_session.state,
            self._agent,
            prompt,
            auto_confirm=params["auto_confirm"],
            output_path=params["output"],
            output_format=params["output_format"],
            quiet=params["quiet"],
        )
        if result.exit_code == EXIT_NEEDS_CONFIRM:
            self.console.print_warning(f"Confirmation required: {result.error}")
        elif result.exit_code == 0:
            run_post_session_updates(
                self.config,
                runtime_session.state.session,
                active_skills=runtime_session.state.active_skills,
            )
        return result.exit_code

    async def run_interactive(self, params: dict) -> int:
        """Start the interactive REPL and return an exit code when the user exits.

        Equivalent to ``CLI.run_interactive`` in the reference code.
        """
        runtime_session = self._build_runtime_session(params)
        await run_repl(
            runtime_session.state,
            self._agent,
            build_router(),
            session_resumed=runtime_session.session_resumed,
        )
        run_post_session_updates(
            self.config,
            runtime_session.state.session,
            active_skills=runtime_session.state.active_skills,
        )
        return 0

    async def close(self) -> None:
        """Shut down delegation runtime and MCP server connections."""
        if self._resources is None:
            return
        if self._resources.delegation is not None:
            await self._resources.delegation.shutdown()
        for server in self._resources.mcp_servers:
            await server.close()

    # ------------------------------------------------------------------
    # Private — session state
    # ------------------------------------------------------------------

    def _build_runtime_session(self, params: dict) -> RuntimeSession:
        """Construct the runtime session for one headless or interactive run."""
        return RuntimeSession.create(
            config=self.config,
            console=self.console,
            params=params,
            tool_registry=self._registry,
            hooks=self._hooks,
            resources=self._resources,
        )

    # ------------------------------------------------------------------
    # Private — registry + tool wiring
    # ------------------------------------------------------------------

    async def _build_registry(
        self, *, load_plugins: bool = True
    ) -> tuple[ToolRegistry, RuntimeResources]:
        """Build the tool registry and start async runtime resources.

        Order:
        1. Start delegation runtime (if enabled)
        2. Register all builtin tools
        3. Load plugins (if enabled)
        4. Connect MCP servers and register their tools
        5. Register sandbox / sub-agent tools
        """
        registry = ToolRegistry()
        resources = RuntimeResources()

        # 1. Delegation runtime
        if self.config.delegation_enabled:
            delegation = DelegationRuntime(
                worker_ids=[str(w) for w in self.config.delegation_workers],
                hooks=self._hooks,
                poll_interval=float(self.config.delegation_poll_interval_seconds),
                history_limit=int(self.config.delegation_message_history_limit),
                base_tool_registry=registry,
                model_client_factory=self._build_model_client,
                workspace_root=self.config.workspace_root,
                temperature=float(self.config.temperature),
                max_output_tokens=int(self.config.max_output_tokens),
                auto_confirm_read_only=bool(self.config.auto_confirm_read_only),
            )
            await delegation.start()
            resources.delegation = delegation

        # 2. Builtin tools
        register_core_tools(registry, self.config)

        # 3. Plugins
        if load_plugins:
            PluginLoader(self.config.plugins_dir).load_all(
                registry,
                self._hooks,
                can_register=lambda tool: self._tool_enabled(tool.name),
            )

        # 4. MCP servers
        for payload in self.config.mcp_servers:
            await self._connect_mcp_server(payload, registry, resources)

        # 5. Sandbox + sub-agent tools
        if self.config.sandbox_commands and self._tool_enabled(SandboxedCommandTool.name):
            register_sandbox_tool(registry, self.config)
        register_subagent_tools(
            registry,
            resources.delegation,
            self.config,
            definitions=load_subagent_definitions(self.config),
        )

        return registry, resources

    async def _connect_mcp_server(
        self,
        payload: dict,
        registry: ToolRegistry,
        resources: RuntimeResources,
    ) -> None:
        """Connect one MCP server and register its tools. Logs and skips on error."""
        server = MCPServerConfig.from_dict(payload)
        runtime = MCPServerRuntime(server=server)
        try:
            specs = await runtime.refresh()
        except Exception as exc:
            logger.warning("Skipping MCP server %s: %s", server.name, exc)
            runtime.last_error = str(exc)
            resources.mcp_servers.append(runtime)
            return

        resources.mcp_servers.append(runtime)
        client = runtime.client
        if client is None:
            logger.warning("Skipping MCP server %s: no client after refresh", server.name)
            return

        for display_name in specs:
            if not self._tool_enabled(display_name):
                continue
            try:
                remote_name = (
                    display_name.removeprefix(server.prefix)
                    if server.prefix
                    else display_name
                )
                registry.register(
                    MCPToolAdapter(
                        client,
                        next(
                            spec
                            for spec in await runtime._list_tools()
                            if spec.name == remote_name
                        ),
                        display_name=display_name,
                    ),
                    source="mcp",
                    origin=server.name,
                )
            except ValueError as exc:
                logger.warning(
                    "Skipping MCP tool %s from %s: %s", display_name, server.name, exc
                )

        runtime.registered_tools = tuple(
            r.name
            for r in registry.records()
            if r.source == "mcp" and r.origin == server.name
        )

    # ------------------------------------------------------------------
    # Private — config / model helpers
    # ------------------------------------------------------------------

    def _tool_enabled(self, tool_name: str) -> bool:
        """Return True if *tool_name* is permitted by the current config."""
        return tool_enabled(self.config, tool_name)

    def _build_model_client(self):
        """Construct the LLM client from the current config."""
        if self.config.provider == "fake":
            return FakeModelClient()
        if self.config.provider == "ollama":
            return OllamaModelClient(
                base_url=resolve_ollama_base_url(self.config.api_base_url or None),
                model_name=self.config.model_name,
            )
        if self.config.provider == "anthropic":
            explicit_key = self.config.api_key or None
            return AnthropicModelClient(api_key=resolve_anthropic_api_key(explicit_key))
        if self.config.provider == "gemini":
            explicit_key = self.config.api_key or None
            return GeminiModelClient(api_key=resolve_gemini_api_key(explicit_key))
        if self.config.provider in {"mistral", "openai-compatible", "openai"}:
            explicit_key = self.config.api_key or None
            return OpenAICompatibleModelClient(
                api_base_url=self.config.api_base_url,
                api_key=resolve_provider_api_key(self.config.provider, explicit_key),
                provider_name=self.config.provider,
            )
        raise ValueError(f"Unsupported provider: {self.config.provider}")

    def _apply_model_context_limits(self) -> None:
        """Auto-tune compaction thresholds to fit the active model's context window.

        Only overrides the built-in defaults (10 000 / 14 000); explicit user
        settings in ``config.toml`` or env vars are left untouched.
        """
        _SOFT_DEFAULT, _HARD_DEFAULT = 10_000, 14_000
        if (
            self.config.compaction_soft_limit != _SOFT_DEFAULT
            and self.config.compaction_hard_limit != _HARD_DEFAULT
        ):
            return  # user overrode both; respect their settings
        ctx_limit = get_model_context_limit(self.config.model_name)
        if self.config.compaction_soft_limit == _SOFT_DEFAULT:
            self.config.compaction_soft_limit = int(ctx_limit * 0.65)
        if self.config.compaction_hard_limit == _HARD_DEFAULT:
            self.config.compaction_hard_limit = int(ctx_limit * 0.85)

    def _log_registered_tools(self) -> None:
        for record in self._registry.records():
            logger.info(
                "Registered tool %s from %s (%s)",
                record.name,
                record.source,
                record.origin or "default",
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _resolve_session(*args, **kwargs) -> tuple:
    """Backward-compat shim for tests that imported the old helper."""
    return resolve_runtime_session(*args, **kwargs)


def provider_error_message(exc: Exception, config) -> str:
    """Return a user-friendly explanation for common LLM provider failures."""
    from os import environ

    msg = str(exc)
    provider = config.provider

    # Ollama-specific: no API key needed, but server must be running.
    if provider == "ollama":
        if "connection failed" in msg.lower() or "urlopen" in msg.lower() or "connection refused" in msg.lower():
            base_url = getattr(config, "api_base_url", "http://localhost:11434") or "http://localhost:11434"
            return (
                f"Could not connect to Ollama at [bold]{base_url}[/bold]. "
                "Make sure Ollama is running ([bold]ollama serve[/bold]) and the model is pulled "
                f"([bold]ollama pull {config.model_name}[/bold])."
            )
        if "404" in msg:
            return (
                f"Model [bold]{config.model_name}[/bold] not found in Ollama. "
                f"Run [bold]ollama pull {config.model_name}[/bold] to download it."
            )
        return f"Ollama error: {msg}"

    has_key = bool(
        config.api_key
        or environ.get("ANTHROPIC_API_KEY")
        or environ.get("GEMINI_API_KEY")
        or environ.get("GOOGLE_API_KEY")
        or environ.get("MISTRAL_API_KEY")
        or environ.get("NEXUS_API_KEY")
        or environ.get("OPENAI_API_KEY")
        or environ.get("API_KEY")
    )
    if not has_key:
        key_env = "API_KEY"
        return (
            f"No API key found for provider [bold]{provider}[/bold]. "
            f"Set [bold]{key_env}[/bold] in your [bold].env[/bold] file or environment, "
            "or run [bold]nexus init[/bold] for setup instructions."
        )
    if "401" in msg or "unauthorized" in msg.lower() or "authentication" in msg.lower():
        return (
            f"Authentication failed for provider [bold]{provider}[/bold] — "
            "check that your API key is valid and not expired."
        )
    if "403" in msg or "forbidden" in msg.lower():
        return (
            f"Access denied by provider [bold]{provider}[/bold] — "
            "check API key permissions and account status."
        )
    if "429" in msg or "rate limit" in msg.lower():
        return "Rate limit reached — wait a moment and try again."
    if "api_base_url" in msg.lower() or "require api_base_url" in msg.lower():
        return (
            f"No API base URL configured for provider [bold]{provider}[/bold]. "
            "Set [bold]api_base_url[/bold] in [bold].nexus/config.toml[/bold] "
            "or via [bold]AGENT_API_BASE_URL[/bold] env var."
        )
    if "connection" in msg.lower() or "urlopen" in msg.lower() or "name or service" in msg.lower():
        return (
            f"Could not connect to provider [bold]{provider}[/bold] "
            f"at [bold]{config.api_base_url}[/bold]. "
            "Check your internet connection and verify the base URL."
        )
    return f"Provider error: {msg}"


def _provider_error_message(exc: Exception, config) -> str:
    """Backward-compat shim for older REPL/headless call sites."""
    return provider_error_message(exc, config)


# ---------------------------------------------------------------------------
# Dispatch helpers — called by click commands in nexus/cli/args.py
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Entry point — wraps the click CLI and returns a plain integer exit code."""
    from nexus.cli.args import cli  # local import avoids load-order issues

    try:
        result = cli.main(args=argv, standalone_mode=False)
        return result if isinstance(result, int) else 0
    except click.exceptions.Exit as exc:
        return exc.code
    except click.exceptions.Abort:
        return 1
    except click.exceptions.ClickException as exc:
        exc.show()
        return exc.exit_code
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


def _dispatch_version() -> None:
    TerminalUI().print_version(__version__)


def _dispatch_runtime(params: dict) -> int:
    """Load config, create NexusApp, and dispatch to single or interactive mode."""
    workspace_root = Path.cwd()
    console = TerminalUI()
    try:
        config = load_config(
            workspace_root,
            cli_overrides=args_to_config_overrides(**params),
            local_config_path=params.get("config_file"),
            global_config_path=params.get("global_config"),
        )
    except ConfigError as exc:
        console.print_config_error(exc)
        return 1

    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    console = TerminalUI(color=config.color_output)
    return asyncio.run(_run_app(config, console, params))


async def _run_app(config, console: TerminalUI, params: dict) -> int:
    """Initialize NexusApp and run headless or interactive mode.

    This is the async heart of :func:`_dispatch_runtime` — analogous to
    ``asyncio.run(cli.run_single(…))`` / ``asyncio.run(cli.run_interactive())``
    in the reference code.
    """
    workspace_root = Path.cwd()
    ensure_config_dirs(config)
    init_workspace(
        workspace_root,
        global_root=config.global_root,
        project_name=config.project_name,
        project_description=config.project_description,
    )

    app = NexusApp(config, console)
    await app.initialize(load_plugins=not params["no_plugins"])
    try:
        prompt = resolve_prompt(params)
        if prompt is not None:
            return await app.run_single(prompt, params)
        return await app.run_interactive(params)
    finally:
        await app.close()


def _dispatch_doctor(output_format: str) -> int:
    """Run the health-check report and print results."""
    workspace_root = Path.cwd()
    console = TerminalUI()
    try:
        config = load_config(workspace_root)
    except ConfigError as exc:
        console.print_config_error(exc)
        return 1

    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    return asyncio.run(_run_doctor(config, console, output_format=output_format))


async def _run_doctor(config, console: TerminalUI, *, output_format: str) -> int:
    """Build NexusApp, collect health-check data, then tear down."""
    ensure_config_dirs(config)
    app = NexusApp(config, console)
    await app.initialize()
    try:
        report = build_doctor_report(config, app._registry, app._resources)
    finally:
        await app.close()
    console.print_doctor_report(report, output_format=output_format)
    return exit_code_for_report(report)


def _dispatch_init(force: bool) -> None:
    """Initialise or reinitialise the workspace config skeleton."""
    workspace_root = Path.cwd()
    console = TerminalUI()
    try:
        config = load_config(workspace_root)
    except ConfigError as exc:
        console.print_config_error(exc)
        return

    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    console = TerminalUI(color=config.color_output)
    ensure_config_dirs(config)
    created = init_workspace(
        workspace_root,
        global_root=config.global_root,
        project_name=config.project_name,
        project_description=config.project_description,
        force=force,
    )
    if created:
        console.print("Created:" if not force else "Reinitialized:")
        for path in created:
            console.print(f"- {path}")
    else:
        console.print("Nexus workspace already initialized.")
    console.print_provider_setup_reminder(config)


def _dispatch_config(scope: str) -> None:
    """Print the resolved config (global, local, or merged JSON)."""
    workspace_root = Path.cwd()
    console = TerminalUI()
    try:
        config = load_config(workspace_root)
    except ConfigError as exc:
        console.print_config_error(exc)
        return

    console = TerminalUI(color=config.color_output)
    if scope == "global":
        console.print(
            config.global_config_file.read_text(encoding="utf-8")
            if config.global_config_file.exists()
            else "# global config not initialized"
        )
    elif scope == "local":
        console.print(
            config.local_config_file.read_text(encoding="utf-8")
            if config.local_config_file.exists()
            else "# local config not initialized"
        )
    else:
        console.print(json.dumps(config_to_plain_dict(config), indent=2))


# ---------------------------------------------------------------------------
# Backward-compat shims for tests that import old module-level names
# ---------------------------------------------------------------------------

def _build_model_client(config):
    """Module-level shim — delegates to NexusApp._build_model_client."""
    return NexusApp(config, TerminalUI())._build_model_client()


def _apply_model_context_limits(config) -> None:
    """Module-level shim — delegates to NexusApp._apply_model_context_limits."""
    NexusApp(config, TerminalUI())._apply_model_context_limits()


if __name__ == "__main__":
    raise SystemExit(main())
