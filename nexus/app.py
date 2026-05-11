from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from nexus import __version__
from nexus.ui import TerminalUI
import click

from nexus.cli.args import args_to_config_overrides
from nexus.cli.doctor import build_doctor_report, exit_code_for_report
from nexus.cli.headless import EXIT_NEEDS_CONFIRM, run_headless
from nexus.cli.init import init_workspace
from nexus.cli.input import resolve_prompt
from nexus.config import config_to_plain_dict, ensure_config_dirs, load_config
from nexus.config.loader import ConfigError
from nexus.config.model_limits import get_model_context_limit
from nexus.extensions.plugins import PluginLoader
from nexus.integrations.fake_model import FakeModelClient
from nexus.integrations.mcp import MCPServerConfig, MCPServerRuntime, MCPToolAdapter
from nexus.integrations.openai_compatible import OpenAICompatibleModelClient, resolve_provider_api_key
from nexus.memory.store import MemoryStore
from nexus.hooks import HookExecutor, setup_hooks
from nexus.runtime.agent import Agent
from nexus.runtime.delegation import DelegationRuntime
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl import run_repl
from nexus.runtime.repl_state import ReplState
from nexus.runtime.post_session import run_post_session_updates
from nexus.runtime.sessions import EphemeralSessionStore, SessionStore, new_snapshot
from nexus.runtime.slash_commands import build_router
from nexus.sandbox import register_agent_tool, register_sandbox_tool
from nexus.sandbox.tool import SandboxedCommandTool
from nexus.security.manager import ApprovalManager
from nexus.security.policy import ApprovalPolicy
from nexus.skills import BUILTIN_SKILLS_DIR, SkillRegistry, load_skill_registry
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, WriteNoteTool
from nexus.tools.filesystem import (
    BashTool,
    GlobTool,
    GrepTool,
    LsTool,
    ModifyFileTool,
    ReadFileTool,
    ReplaceTextTool,
    WriteFileTool,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeResources:
    mcp_servers: list[MCPServerRuntime] = field(default_factory=list)
    delegation: DelegationRuntime | None = None


def main(argv: list[str] | None = None) -> int:
    """Entry point — wraps the click CLI so callers get a plain integer exit code."""
    from nexus.cli.args import cli  # local import avoids any load-order issues

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


# ---------------------------------------------------------------------------
# Dispatch helpers — called by click commands in nexus.cli.args (lazy import).
# ---------------------------------------------------------------------------

def _dispatch_version() -> None:
    console = TerminalUI()
    console.print_version(__version__)


def _dispatch_runtime(params: dict) -> int:
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
    return asyncio.run(_run_runtime(params, config, console, workspace_root))


def _dispatch_doctor(output_format: str) -> int:
    workspace_root = Path.cwd()
    console = TerminalUI()
    try:
        config = load_config(workspace_root)
    except ConfigError as exc:
        console.print_config_error(exc)
        return 1
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    return asyncio.run(_run_doctor(config, console, output_format=output_format))


def _dispatch_init(force: bool) -> None:
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
    _print_provider_setup_reminder(console, config)


def _dispatch_config(scope: str) -> None:
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


async def _run_runtime(params: dict, config, console: TerminalUI, workspace_root: Path) -> int:
    # Auto-tune compaction limits when the user hasn't overridden them.
    _apply_model_context_limits(config)

    ensure_config_dirs(config)
    init_workspace(
        workspace_root,
        global_root=config.global_root,
        project_name=config.project_name,
        project_description=config.project_description,
    )

    hooks = setup_hooks(config)
    registry, resources = await _build_registry(config, hooks, load_plugins=not params["no_plugins"])
    _log_registered_tools(registry)
    agent = Agent(model_client=_build_model_client(config), tool_registry=registry, hooks=hooks)
    if params["no_session"]:
        session_store = EphemeralSessionStore()
    else:
        session_store = SessionStore(
            config.session_dir,
            max_sessions_retained=config.max_sessions_retained,
        )
    session, session_resumed = _resolve_session(
        params["session"], session_store, persist_sessions=not params["no_session"]
    )
    no_skills: bool = params["no_skills"]
    skill_registry = SkillRegistry() if no_skills else load_skill_registry(
        BUILTIN_SKILLS_DIR, config.skills_dir, config.local_root / "skills"
    )
    builtin_active = [] if no_skills else (
        ["nexus-agent"] if skill_registry.get("nexus-agent") is not None else []
    )
    active_skills = builtin_active + [
        name for name in params.get("skills", ()) if skill_registry.get(name) is not None
    ]
    state = ReplState(
        config=config,
        mode=ExecutionMode.PLAN if params["deny_mutating"] else ExecutionMode(config.default_mode),
        session=session,
        session_store=session_store,
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=console,
        hooks=hooks,
        approval_manager=ApprovalManager(
            policy=ApprovalPolicy(config.approval_policy)
        ),
        history=list(session.messages),
        skill_registry=skill_registry,
        active_skills=active_skills,
        mcp_servers=resources.mcp_servers,
        delegation=resources.delegation,
    )

    try:
        prompt = resolve_prompt(params)
        if prompt is not None:
            result = await run_headless(
                state,
                agent,
                prompt,
                auto_confirm=params["auto_confirm"],
                output_path=params["output"],
                output_format=params["output_format"],
                quiet=params["quiet"],
            )
            if result.exit_code == EXIT_NEEDS_CONFIRM:
                console.print_warning(f"Confirmation required: {result.error}")
            elif result.exit_code == 0:
                run_post_session_updates(config, state.session, active_skills=state.active_skills)
            return result.exit_code

        await run_repl(state, agent, build_router(), session_resumed=session_resumed)
        run_post_session_updates(config, state.session, active_skills=state.active_skills)
        return 0
    finally:
        await _close_runtime_resources(resources)


async def _build_registry(config, hooks: HookExecutor, *, load_plugins: bool = True) -> tuple[ToolRegistry, RuntimeResources]:
    registry = ToolRegistry()
    resources = RuntimeResources()
    model_client_factory = lambda: _build_model_client(config)
    if config.delegation_enabled:
        delegation = DelegationRuntime(
            worker_ids=[str(worker_id) for worker_id in config.delegation_workers],
            hooks=hooks,
            poll_interval=float(config.delegation_poll_interval_seconds),
            history_limit=int(config.delegation_message_history_limit),
            base_tool_registry=registry,
            model_client_factory=model_client_factory,
            workspace_root=config.workspace_root,
            temperature=float(config.temperature),
            max_output_tokens=int(config.max_output_tokens),
            auto_confirm_read_only=bool(config.auto_confirm_read_only),
        )
        await delegation.start()
        resources.delegation = delegation
    for tool in (
        GetTimeTool(),
        WriteNoteTool(max_bytes=int(config.write_note_max_bytes)),
        ReadFileTool(),
        WriteFileTool(),
        ModifyFileTool(),
        ReplaceTextTool(),
        GlobTool(),
        GrepTool(),
        LsTool(),
        BashTool(),
    ):
        if not _tool_enabled(config, tool.name):
            continue
        registry.register(tool, source="core", origin="builtin")

    if load_plugins:
        PluginLoader(config.plugins_dir).load_all(
            registry,
            hooks,
            can_register=lambda tool: _tool_enabled(config, tool.name),
        )

    for payload in config.mcp_servers:
        server = MCPServerConfig.from_dict(payload)
        runtime = MCPServerRuntime(server=server)
        try:
            specs = await runtime.refresh()
        except Exception as exc:
            logger.warning("Skipping MCP server %s: %s", server.name, exc)
            runtime.last_error = str(exc)
            resources.mcp_servers.append(runtime)
            continue
        resources.mcp_servers.append(runtime)
        client = runtime.client
        if client is None:
            logger.warning("Skipping MCP server %s because no client is available after refresh.", server.name)
            continue
        for display_name in specs:
            if not _tool_enabled(config, display_name):
                continue
            try:
                remote_name = display_name.removeprefix(server.prefix) if server.prefix else display_name
                registry.register(
                    MCPToolAdapter(
                        client,
                        next(spec for spec in await runtime._list_tools() if spec.name == remote_name),
                        display_name=display_name,
                    ),
                    source="mcp",
                    origin=server.name,
                )
            except ValueError as exc:
                logger.warning("Skipping MCP tool %s from %s: %s", display_name, server.name, exc)
        runtime.registered_tools = tuple(
            record.name for record in registry.records() if record.source == "mcp" and record.origin == server.name
        )

    if config.sandbox_commands and _tool_enabled(config, SandboxedCommandTool.name):
        register_sandbox_tool(registry, config)
    register_agent_tool(registry, resources.delegation, config)
    return registry, resources


def _tool_enabled(config, tool_name: str) -> bool:
    if config.allowed_tools and tool_name not in config.allowed_tools:
        return False
    if tool_name in config.denied_tools:
        return False
    return True


def _print_provider_setup_reminder(console: TerminalUI, config) -> None:
    """After init or on startup, remind the user how to set an API key if none is present."""
    console.print_provider_setup_reminder(config)


def _provider_error_message(exc: Exception, config) -> str:
    """Return a user-friendly explanation for common provider request failures."""
    from os import environ
    msg = str(exc)
    provider = config.provider
    has_key = bool(
        config.api_key
        or environ.get("MISTRAL_API_KEY")
        or environ.get("NEXUS_API_KEY")
        or environ.get("OPENAI_API_KEY")
    )
    if not has_key:
        key_env = "MISTRAL_API_KEY" if provider == "mistral" else "NEXUS_API_KEY"
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
            "or via [bold]AGENT_API_BASE_URL[/bold] environment variable."
        )
    if "connection" in msg.lower() or "urlopen" in msg.lower() or "name or service" in msg.lower():
        return (
            f"Could not connect to provider [bold]{provider}[/bold] "
            f"at [bold]{config.api_base_url}[/bold]. "
            "Check your internet connection and verify the base URL."
        )
    return f"Provider error: {msg}"


def _apply_model_context_limits(config) -> None:
    """Auto-tune compaction thresholds to match the active model's context window.

    Only overrides values that are still at the built-in defaults (10 000 / 14 000)
    so explicit user settings in config.toml or env vars are preserved.
    """
    _SOFT_DEFAULT = 10_000
    _HARD_DEFAULT = 14_000
    if config.compaction_soft_limit != _SOFT_DEFAULT and config.compaction_hard_limit != _HARD_DEFAULT:
        return  # User has explicitly overridden both; respect their settings
    ctx_limit = get_model_context_limit(config.model_name)
    if config.compaction_soft_limit == _SOFT_DEFAULT:
        config.compaction_soft_limit = int(ctx_limit * 0.65)
    if config.compaction_hard_limit == _HARD_DEFAULT:
        config.compaction_hard_limit = int(ctx_limit * 0.85)


def _build_model_client(config):
    if config.provider == "fake":
        return FakeModelClient()
    if config.provider in {"mistral", "openai-compatible", "openai"}:
        # config.api_key is set if it was present in .nexus/config.toml, a .env
        # file, or an AGENT_API_KEY environment variable. Fall back to the
        # provider-specific env var lookup when none of those are present.
        explicit_key = config.api_key or None
        return OpenAICompatibleModelClient(
            api_base_url=config.api_base_url,
            api_key=resolve_provider_api_key(config.provider, explicit_key),
            provider_name=config.provider,
        )
    raise ValueError(f"Unsupported provider: {config.provider}")


def _log_registered_tools(registry: ToolRegistry) -> None:
    for record in registry.records():
        logger.info(
            "Registered tool %s from %s (%s)",
            record.name,
            record.source,
            record.origin or "default",
        )


async def _close_runtime_resources(resources: RuntimeResources) -> None:
    if resources.delegation is not None:
        await resources.delegation.shutdown()
    for server in resources.mcp_servers:
        await server.close()


async def _run_doctor(config, console: TerminalUI, *, output_format: str) -> int:
    ensure_config_dirs(config)
    hooks = setup_hooks(config)
    registry, resources = await _build_registry(config, hooks)
    try:
        report = build_doctor_report(config, registry, resources)
    finally:
        await _close_runtime_resources(resources)
    console.print_doctor_report(report, output_format=output_format)
    return exit_code_for_report(report)


def _resolve_session(session_id: str | None, store: SessionStore, *, persist_sessions: bool = True):
    """Return (snapshot, resumed: bool).

    When no explicit session ID is given and sessions are enabled, automatically
    resume the most recently saved session so the user picks up where they left off.
    """
    if not persist_sessions:
        return new_snapshot(), False
    if session_id is not None:
        try:
            return store.load(session_id), True
        except FileNotFoundError:
            return new_snapshot(session_id=session_id), False
    # Auto-resume: load the latest saved session if one exists.
    latest = store.load_latest()
    if latest is not None:
        return latest, True
    return new_snapshot(), False


if __name__ == "__main__":
    raise SystemExit(main())

