from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from rich.table import Table

from nexus.config import config_to_plain_dict, load_config
from nexus.config.model_limits import get_model_context_limit
from nexus.config.upgrade import inspect_config_upgrade, upgrade_config_file
from nexus.cli.init import _global_config_toml, _local_config_toml
from nexus.memory.store import MemoryEntry
from nexus.context import CarryOverState, TokenEstimator
from nexus.runtime.context_state import (
    get_context_payload,
    load_multi_agent_state,
    render_context_packet,
)
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.agent_scope import (
    clean_string_list,
    is_all_scope,
    mcp_tool_names_for_servers,
    normalize_subagent_name,
    subagent_profile,
    subagent_skill_names,
    subagent_tool_name,
    subagent_tool_names,
    supervisor_skill_names,
    supervisor_tool_names,
)
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import message_to_dict, new_snapshot
from nexus.tools.mcp import (
    MCPRefreshReport,
    MCPServerConfig,
    MCPServerRuntime,
    refresh_mcp_server_tools,
    register_discovered_mcp_tools,
)
from nexus.security.manager import ApprovalManager
from nexus.security.policy import ApprovalPolicy
from nexus.skills import get_skill_roots, load_skill_registry, resolve_active_skill_names, skill_template
from nexus.skills.parser import SkillParseError, validate_skill_metadata
from nexus.agents.loader import (
    demote_to_local,
    get_agent_roots,
    list_yaml_subagent_files,
    promote_to_global,
    scaffold_yaml,
)
from nexus.tools.subagents import (
    load_subagent_definitions,
    register_skill_subagent_tools,
    register_subagent_tools,
    register_yaml_subagent_tools,
)


CommandHandler = Callable[[ReplState, list[str]], Awaitable[None]]

# Only these parameters can be updated live via /provider set.
# This prevents arbitrary config key injection (unlike /config set which is unguarded).
PROVIDER_SETTABLE_PARAMS: frozenset[str] = frozenset({
    "provider",
    "model_name",
    "api_base_url",
    "temperature",
    "max_output_tokens",
    "max_loop_iterations",
    "stream_output",
    "show_tool_calls",
})


def _print_subcommand_help(
    state: ReplState,
    command: str,
    description: str,
    rows: tuple[tuple[str, str, str], ...],
) -> None:
    """Render a tidy help table for a slash command and its subcommands.

    *rows* is a sequence of (subcommand-syntax, description, example).
    """
    state.console.print(f"[bold]/{command}[/bold] — {description}\n")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Subcommand", no_wrap=True)
    table.add_column("What it does")
    table.add_column("Example", style="dim")
    for syntax, what, example in rows:
        table.add_row(syntax, what, example)
    state.console.print(table)


@dataclass(slots=True)
class SlashCommand:
    name: str
    description: str
    handler: CommandHandler


class SlashCommandRouter:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command

    async def dispatch(self, state: ReplState, raw_input: str) -> bool:
        if not raw_input.startswith("/"):
            return False
        try:
            parts = shlex.split(raw_input[1:])
        except ValueError as exc:
            state.console.print(f"Invalid command syntax: {exc}")
            return True
        if not parts:
            return True
        command = self._commands.get(parts[0].lower())
        if command is None:
            # Unknown slash command — forward the raw input to the agent as a
            # natural-language query so the user gets a helpful AI response.
            return False
        await command.handler(state, parts[1:])
        return True


def build_router() -> SlashCommandRouter:
    router = SlashCommandRouter()
    router.register(SlashCommand("help", "Show available slash commands.", handle_help))
    router.register(SlashCommand("mcp", "Inspect MCP server status and tools.", handle_mcp))
    router.register(SlashCommand("agent", "Inspect and scope supervisor tools, skills, and MCP.", handle_agent))
    router.register(SlashCommand("sub-agent", "Inspect and scope cognitive sub-agent resources.", handle_sub_agent))
    router.register(SlashCommand("subagent", "Inspect and scope cognitive sub-agent resources.", handle_sub_agent))
    router.register(SlashCommand("mode", "Show or switch execution mode.", handle_mode))
    router.register(SlashCommand("provider", "Show or update model provider and session parameters.", handle_provider))
    router.register(SlashCommand("skills", "Inspect and activate skills.", handle_skills))
    router.register(SlashCommand("config", "Show merged, local, or global config.", handle_config))
    router.register(SlashCommand("session", "Inspect or create sessions.", handle_session))
    router.register(SlashCommand("tools", "List registered tools.", handle_tools))
    router.register(SlashCommand("memory", "Inspect workspace memory entries.", handle_memory))
    router.register(SlashCommand("context", "Show system prompt or context usage stats.", handle_context))
    router.register(SlashCommand("history", "Show recent message history.", handle_history))
    router.register(SlashCommand("abort", "Abort the currently running agent turn.", handle_abort))
    router.register(SlashCommand("quit", "Save and exit the REPL.", handle_quit))
    router.register(SlashCommand("exit", "Save and exit the REPL.", handle_quit))
    return router


async def handle_help(state: ReplState, args: list[str]) -> None:
    del args
    table = Table(title="Slash Commands")
    table.add_column("Command")
    table.add_column("Description")
    for name, description in (
        ("/help", "Show command help."),
        ("/mcp [status|tools|available|activate|deactivate|refresh]", "Inspect and manage MCP server connections."),
        ("/agent [status|tools|skills|mcp|allow|disallow]", "Inspect and scope supervisor resources."),
        ("/sub-agent [list|show|tools|skills|mcp|allow|disallow]", "Inspect and scope sub-agent resources."),
        ("/mode [plan|default|auto]", "Show or switch execution mode."),
        ("/provider [list|set <param> <value>]", "Show active provider and update model/session parameters."),
        ("/skills [list|show|add|remove|reload]", "Inspect and activate session skills."),
        ("/config show [scope]", "Print config for merged, local, or global scope."),
        ("/config upgrade [local|global]", "Add new config keys/tool allowlist entries from latest Nexus defaults, then reload tools."),
        ("/session [new|list|resume|save|export]", "Show current session or manage saved sessions."),
        ("/tools [reload]", "List tools or reload the tool registry from config."),
        ("/memory list|search|save|show", "Inspect or update workspace memory."),
        ("/context [show|usage]", "Print the system prompt or show context/token usage stats."),
        ("/history [n]", "Show recent messages."),
        ("/abort", "Abort the currently running agent turn."),
        ("/quit", "Save and exit."),
    ):
        table.add_row(name, description)
    state.console.print(table)


async def handle_mcp(state: ReplState, args: list[str]) -> None:
    subcommand = args[0].lower() if args else "status"
    if subcommand == "help":
        _print_mcp_help(state)
        return

    if subcommand == "available":
        _print_mcp_available(state)
        return

    if subcommand in {"activate", "add"}:
        if len(args) < 2:
            state.console.print("Usage: /mcp activate <server>")
            return
        await _set_mcp_server_active(state, args[1], active=True)
        return

    if subcommand in {"deactivate", "remove"}:
        if len(args) < 2:
            state.console.print("Usage: /mcp deactivate <server>")
            return
        await _set_mcp_server_active(state, args[1], active=False)
        return

    if subcommand == "reload":
        reports = await _reload_mcp_servers(state)
        if not reports:
            state.console.print("No MCP servers configured. Add mcp_servers to .nexus/config.toml, then run /mcp reload.")
            return
        _refresh_system_prompt_after_mcp_change(state)
        _print_mcp_refresh_reports(state, reports, title="MCP Reload")
        _print_mcp_status(state)
        return

    if not state.mcp_servers:
        state.console.print("No MCP servers loaded. Add mcp_servers to .nexus/config.toml, then run /mcp reload.")
        state.console.print("Usage: /mcp [status|tools|available|activate|deactivate|refresh|reload|help]")
        return

    if subcommand == "status":
        _print_mcp_status(state)
        return
    if subcommand == "tools":
        _print_mcp_tools(state)
        return
    if subcommand == "refresh":
        target_name = args[1] if len(args) > 1 else None
        reports = await _refresh_mcp_servers(state, target_name)
        if not reports:
            state.console.print(f"MCP server not found: {target_name}")
            return
        _refresh_system_prompt_after_mcp_change(state)
        _print_mcp_refresh_reports(state, reports)
        _print_mcp_status(state)
        return
    state.console.print("Usage: /mcp [status|tools|available|activate|deactivate|refresh [server]|reload|help]")


async def handle_agent(state: ReplState, args: list[str]) -> None:
    subcommand = args[0].lower() if args else "status"
    if subcommand == "help":
        _print_subcommand_help(
            state, "agent", "Inspect and scope supervisor tools, skills, and MCP servers.",
            (
                ("status", "Show supervisor mode and effective resource counts.", "/agent status"),
                ("tools", "List registered tools and whether the supervisor can call them.", "/agent tools"),
                ("skills", "List globally active skills and supervisor scoped state.", "/agent skills"),
                ("mcp", "List active MCP servers and supervisor scoped state.", "/agent mcp"),
                ("allow tool <tool>", "Add a registered tool to the supervisor allowlist.", "/agent allow tool read_file"),
                ("disallow tool <tool>", "Remove a registered tool from the supervisor allowlist.", "/agent disallow tool read_file"),
                ("allow skill <skill>", "Add an active skill to the supervisor allowlist.", "/agent allow skill review"),
                ("disallow skill <skill>", "Remove a skill from the supervisor allowlist.", "/agent disallow skill review"),
                ("allow mcp <server>", "Add an active MCP server to the supervisor allowlist.", "/agent allow mcp filesystem"),
                ("disallow mcp <server>", "Remove an MCP server from the supervisor allowlist.", "/agent disallow mcp filesystem"),
            ),
        )
        return
    if subcommand == "status":
        _print_agent_status(state)
        return
    if subcommand == "tools":
        _print_agent_tools(state)
        return
    if subcommand == "skills":
        _print_agent_skills(state)
        return
    if subcommand == "mcp":
        _print_agent_mcp(state)
        return
    if subcommand in {"allow", "disallow"}:
        if len(args) < 3:
            state.console.print("Usage: /agent allow|disallow tool|skill|mcp <name>")
            return
        if _set_agent_resource_allowed(state, args[1].lower(), args[2], allowed=(subcommand == "allow")):
            state.refresh_system_prompt()
            _print_agent_status(state)
        return
    state.console.print("Usage: /agent [status|tools|skills|mcp|allow|disallow|help]")


async def handle_sub_agent(state: ReplState, args: list[str]) -> None:
    subcommand = args[0].lower() if args else "list"
    if subcommand == "help":
        _print_subcommand_help(
            state, "sub-agent", "Inspect and scope cognitive sub-agent resources.",
            (
                ("list", "List registered cognitive sub-agent tools.", "/sub-agent list"),
                ("show <name>", "Show one sub-agent description and effective resources.", "/sub-agent show execution"),
                ("tools <name>", "List effective tools for one sub-agent.", "/sub-agent tools execution"),
                ("skills <name>", "List allowed skill metadata for one sub-agent.", "/sub-agent skills execution"),
                ("mcp <name>", "List active MCP servers and sub-agent scoped state.", "/sub-agent mcp execution"),
                ("allow <name> tool|skill|mcp <id>", "Add a resource to a sub-agent allowlist.", "/sub-agent allow execution tool read_file"),
                ("disallow <name> tool|skill|mcp <id>", "Remove a resource from a sub-agent allowlist.", "/sub-agent disallow execution mcp filesystem"),
                ("agents list", "List YAML sub-agents in local and global agent directories.", "/sub-agent agents list"),
                ("agents new <name> [local|global]", "Scaffold a new YAML sub-agent file.", "/sub-agent agents new explore"),
                ("agents promote <name>", "Move a local YAML sub-agent to the global directory.", "/sub-agent agents promote explore"),
                ("agents demote <name>", "Move a global YAML sub-agent to the local directory.", "/sub-agent agents demote explore"),
                ("agents reload", "Re-scan agent directories and register new YAML sub-agents.", "/sub-agent agents reload"),
            ),
        )
        return
    if subcommand == "list":
        _print_subagent_list(state)
        return
    if subcommand == "agents":
        await _handle_sub_agent_agents(state, args[1:])
        return
    if subcommand in {"show", "tools", "skills", "mcp"}:
        if len(args) < 2:
            state.console.print(f"Usage: /sub-agent {subcommand} <name>")
            return
        record = _subagent_record(state, args[1])
        if record is None:
            state.console.print(f"Sub-agent not found: {args[1]}")
            return
        if subcommand == "show":
            _print_subagent_show(state, record)
        elif subcommand == "tools":
            _print_subagent_tools(state, record)
        elif subcommand == "skills":
            _print_subagent_skills(state, record)
        else:
            _print_subagent_mcp(state, record)
        return
    if subcommand in {"allow", "disallow"}:
        if len(args) < 4:
            state.console.print("Usage: /sub-agent allow|disallow <name> tool|skill|mcp <id>")
            return
        record = _subagent_record(state, args[1])
        if record is None:
            state.console.print(f"Sub-agent not found: {args[1]}")
            return
        if _set_subagent_resource_allowed(state, record, args[2].lower(), args[3], allowed=(subcommand == "allow")):
            state.refresh_system_prompt()
            _print_subagent_show(state, record)
        return
    state.console.print("Usage: /sub-agent [list|show|tools|skills|mcp|allow|disallow|agents|help]")


async def _handle_sub_agent_agents(state: ReplState, args: list[str]) -> None:
    """Handle the ``/sub-agent agents <subcommand>`` family."""
    subcommand = args[0].lower() if args else "list"

    if subcommand == "list":
        _print_yaml_agent_list(state)
        return

    if subcommand == "new":
        if len(args) < 2:
            state.console.print("Usage: /sub-agent agents new <name> [local|global]")
            return
        name = args[1].lower()
        scope = args[2].lower() if len(args) > 2 else "local"
        if scope not in {"local", "global"}:
            state.console.print("Scope must be 'local' or 'global'.")
            return
        _scaffold_yaml_agent(state, name, scope)
        return

    if subcommand == "promote":
        if len(args) < 2:
            state.console.print("Usage: /sub-agent agents promote <name>")
            return
        _move_yaml_agent(state, args[1].lower(), direction="promote")
        return

    if subcommand == "demote":
        if len(args) < 2:
            state.console.print("Usage: /sub-agent agents demote <name>")
            return
        _move_yaml_agent(state, args[1].lower(), direction="demote")
        return

    if subcommand == "reload":
        _reload_yaml_agents(state)
        return

    state.console.print(
        "Usage: /sub-agent agents [list|new <name>|promote <name>|demote <name>|reload]"
    )


def _print_yaml_agent_list(state: ReplState) -> None:
    records = list_yaml_subagent_files(state.config)
    if not records:
        local, global_ = get_agent_roots(state.config)
        state.console.print(
            f"No YAML sub-agent files found.\n"
            f"  Local:  {local}\n"
            f"  Global: {global_}\n"
            "Create one with: /sub-agent agents new <name>"
        )
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name", no_wrap=True)
    table.add_column("Scope")
    table.add_column("Description")
    table.add_column("Tools")
    table.add_column("Skills")
    table.add_column("MCPs")
    table.add_column("Turns")
    for rec in records:
        if not rec.get("valid"):
            table.add_row(
                rec["name"],
                rec["scope"],
                f"[red]INVALID: {rec.get('error', '?')}[/red]",
                "",
                "",
                "",
                "",
            )
        else:
            tools = ", ".join(rec.get("allowed_tools") or []) or "(all active)"
            skills = ", ".join(rec.get("allowed_skills") or []) or "(all active)"
            mcps = ", ".join(rec.get("allowed_mcps") or []) or "(all active)"
            table.add_row(
                rec["name"],
                rec["scope"],
                rec["description"],
                tools,
                skills,
                mcps,
                str(rec["max_turns"]),
            )
    state.console.print(table)


def _scaffold_yaml_agent(state: ReplState, name: str, scope: str) -> None:
    import re
    _NAME_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
    if not _NAME_RE.fullmatch(name):
        state.console.print(
            f"Invalid name '{name}'. Use lowercase letters, digits, hyphens, or underscores."
        )
        return

    local, global_ = get_agent_roots(state.config)
    target_dir = local if scope == "local" else global_
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{name}.yml"
    if dest.exists():
        state.console.print(f"File already exists: {dest}")
        return
    dest.write_text(scaffold_yaml(name), encoding="utf-8")
    state.console.print(f"Created {scope} sub-agent file: {dest}")
    state.console.print("Edit the file, then run: /sub-agent agents reload")


def _move_yaml_agent(state: ReplState, name: str, *, direction: str) -> None:
    try:
        if direction == "promote":
            dest = promote_to_global(name, state.config)
            state.console.print(f"Promoted '{name}' to global: {dest}")
        else:
            dest = demote_to_local(name, state.config)
            state.console.print(f"Demoted '{name}' to local: {dest}")
    except FileNotFoundError as exc:
        state.console.print(f"[red]{exc}[/red]")
        return
    except FileExistsError as exc:
        state.console.print(f"[red]{exc}[/red]")
        return
    # Reload so the moved definition is re-registered under its new scope.
    _reload_yaml_agents(state)


def _reload_yaml_agents(state: ReplState) -> None:
    """Re-scan agent directories and register newly discovered YAML sub-agents."""
    removed = state.tool_registry.unregister_source(source="agent-yaml")
    count = register_yaml_subagent_tools(
        state.tool_registry,
        state.config,
        replace_existing=True,
    )
    state.refresh_system_prompt()
    if count:
        action = "Reloaded" if removed else "Registered"
        state.console.print(f"{action} {count} YAML sub-agent tool(s). Run /tools to verify.")
    else:
        state.console.print("No YAML sub-agents found.")
    _print_yaml_agent_list(state)


async def handle_mode(state: ReplState, args: list[str]) -> None:
    if args and args[0].lower() == "help":
        _print_subcommand_help(
            state, "mode", "Show or switch the agent execution mode.",
            (
                ("(no args)", "Show the current execution mode.",                                     "/mode"),
                ("plan",      "Deny all mutating tools — read-only mode.",                            "/mode plan"),
                ("default",   "Require confirmation before any mutating tool runs.",                  "/mode default"),
                ("auto",      "Approve all tools automatically — no confirmation prompts.",           "/mode auto"),
                ("help",      "Show this help.",                                                      "/mode help"),
            ),
        )
        return
    if not args:
        state.console.print(f"Current mode: {state.mode.value}")
        return
    state.mode = ExecutionMode(args[0].lower())
    state.console.print(f"Mode set to: {state.mode.value}")


async def handle_provider(state: ReplState, args: list[str]) -> None:
    """Show or update model provider and session parameters.

    /provider               — show current provider status
    /provider list          — list all available providers
    /provider set <k> <v>   — update a provider-related parameter
    """
    if args and args[0].lower() == "help":
        _print_subcommand_help(
            state, "provider", "Show or update model provider and session parameters.",
            (
                ("(no args) / status",      "Show the current provider, model, temperature, etc.",             "/provider"),
                ("list",                    "List all available providers with active flag.",                  "/provider list"),
                ("set <param> <value>",     "Update a provider parameter and hot-reload config.",              "/provider set model_name mistral-large-latest"),
                ("",                        "Settable params: provider, model_name, api_base_url,",             ""),
                ("",                        "temperature, max_output_tokens, max_loop_iterations,",             ""),
                ("",                        "stream_output, show_tool_calls.",                                  ""),
                ("help",                    "Show this help.",                                                 "/provider help"),
            ),
        )
        return
    if not args or args[0].lower() in {"status", "show"}:
        _print_provider_status(state)
        return
    if args[0].lower() == "list":
        _print_provider_list(state)
        return
    if args[0].lower() == "set" and len(args) > 2:
        key = args[1].lower()
        value = " ".join(args[2:])
        if key not in PROVIDER_SETTABLE_PARAMS:
            state.console.print(
                f"Unknown or restricted parameter: {key!r}. "
                f"Settable parameters: {', '.join(sorted(PROVIDER_SETTABLE_PARAMS))}"
            )
            return
        _update_toml_value(state.config.local_config_file, key, value)
        state.config = load_config(
            state.config.workspace_root,
            global_root=state.config.global_root,
            local_config_path=state.config.local_config_file,
            global_config_path=state.config.global_config_file,
            cli_overrides={key: _coerce_toml_value(value)},
        )
        state.reload_model_client()
        state.refresh_system_prompt()
        state.console.print(f"Updated {key} = {getattr(state.config, key)!r}")
        return
    if args[0].lower() == "set" and len(args) <= 2:
        state.console.print("Usage: /provider set <param> <value>")
        return
    state.console.print("Usage: /provider [status|list|set <param> <value>]")


def _print_provider_status(state: ReplState) -> None:
    table = Table(title="Model Provider")
    table.add_column("Parameter")
    table.add_column("Value")
    param_order = (
        "provider",
        "model_name",
        "api_base_url",
        "temperature",
        "max_output_tokens",
        "max_loop_iterations",
        "stream_output",
        "show_tool_calls",
    )
    for param in param_order:
        table.add_row(param, str(getattr(state.config, param)))
    state.console.print(table)


def _print_provider_list(state: ReplState) -> None:
    table = Table(title="Available Providers")
    table.add_column("Provider")
    table.add_column("Description")
    table.add_column("Active")
    rows: tuple[tuple[str, str], ...] = (
        ("anthropic", "Anthropic Messages API (requires ANTHROPIC_API_KEY)."),
        ("fake", "Deterministic local fake client. No API key required."),
        ("gemini", "Google Gemini API (requires GEMINI_API_KEY or GOOGLE_API_KEY)."),
        ("mistral", "Mistral AI API endpoint (requires MISTRAL_API_KEY)."),
        ("openai", "OpenAI API endpoint (requires api_base_url and API key)."),
        ("openai-compatible", "Any OpenAI-compatible API endpoint (Ollama, vLLM, etc.)."),
        ("ollama", "Local Ollama server (no API key required)."),
    )
    for provider, description in rows:
        table.add_row(provider, description, "yes" if state.config.provider == provider else "no")
    state.console.print(table)


async def handle_config(state: ReplState, args: list[str]) -> None:
    if args and args[0].lower() == "help":
        _print_subcommand_help(
            state, "config", "Show, edit, or reload Nexus configuration.",
            (
                ("(no args) / show merged",  "Print the fully-merged config as JSON.",                         "/config"),
                ("show local",              "Print .nexus/config.toml (local workspace config).",              "/config show local"),
                ("show global",             "Print ~/.nexus/config.toml (global user config).",                "/config show global"),
                ("set <key> <value>",       "Write a key to local config and reload immediately.",             "/config set show_tool_calls false"),
                ("",                        "Profile shortcut: /config set agent_mode advanced",                ""),
                ("",                        "Example hidden-path override: /config set allow_hidden_paths true", ""),
                ("reset <key>",             "Remove a key from local config and reload.",                      "/config reset temperature"),
                ("reset-defaults [scope]",  "Rewrite local (default) or global config to clean Nexus defaults.", "/config reset-defaults"),
                ("reload",                  "Reload config plus workspace .env/environment values.",            "/config reload"),
                ("upgrade [local|global]",  "Add new config keys and default tool allowlist entries, then reload config and tools. Does not touch memory or sessions.", "/config upgrade"),
                ("reinit [local|global]",   "Rewrite local (default) or global config to clean Nexus defaults.  Clears provider/model overrides. Does not touch sessions or memory.","/config reinit"),
                ("help",                    "Show this help.",                                                 "/config help"),
            ),
        )
        return
    if args and args[0].lower() == "upgrade":
        await _handle_config_upgrade(state, scope=args[1].lower() if len(args) > 1 else "local")
        return
    if not args or args[0].lower() == "show":
        scope = args[1].lower() if len(args) > 1 else "merged"
    elif args[0].lower() == "reload":
        _reload_config(state)
        state.console.print("Config reloaded.")
        return
    elif args[0].lower() in {"reinit", "reset-defaults"} or (args[0].lower() == "reset" and len(args) > 1 and args[1].lower() in {"defaults", "default-config", "all"}):
        offset = 2 if args[0].lower() == "reset" else 1
        scope = args[offset].lower() if len(args) > offset else "local"
        if scope == "global":
            path = state.config.global_config_file
            new_content = _global_config_toml()
            label = "global"
        elif scope == "local":
            path = state.config.local_config_file
            new_content = _local_config_toml(
                workspace_root=state.config.workspace_root,
                project_name=state.config.project_name or state.config.workspace_root.name,
                project_description=state.config.project_description,
            )
            label = "local"
        else:
            state.console.print(f"[red]Unknown scope '{scope}'. Use 'local' or 'global'.[/red]")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content, encoding="utf-8")
        _reload_config(state)
        state.console.print(f"[green]Reinitialized {label} config at {path}[/green]")
        state.console.print("Use [bold]/config reload[/bold] or restart to apply all changes.")
        return
    elif args[0].lower() == "set" and len(args) > 2:
        _update_toml_value(state.config.local_config_file, args[1], " ".join(args[2:]))
        _reload_config(state)
        state.console.print(f"Updated local config: {args[1]}")
        return
    elif args[0].lower() == "reset" and len(args) > 1:
        _remove_toml_key(state.config.local_config_file, args[1])
        _reload_config(state)
        state.console.print(f"Removed local config override: {args[1]}")
        return
    else:
        scope = args[0].lower()
    if scope == "global":
        path = state.config.global_config_file
        content = path.read_text(encoding="utf-8") if path.exists() else "# global config not initialized\n"
        state.console.print(content)
        return
    if scope == "local":
        path = state.config.local_config_file
        content = path.read_text(encoding="utf-8") if path.exists() else "# local config not initialized\n"
        state.console.print(content)
        return
    merged = json.dumps(config_to_plain_dict(state.config), indent=2)
    state.console.print(merged)


async def handle_session(state: ReplState, args: list[str]) -> None:
    if args and args[0].lower() == "help":
        _print_subcommand_help(
            state, "session", "Inspect or manage REPL sessions.",
            (
                ("(no args)",         "Show current session ID and message count.",                   "/session"),
                ("new",              "Save the current session and start a fresh one.",             "/session new"),
                ("list",             "List all saved sessions with ID, timestamp, and summary.",   "/session list"),
                ("resume <id>",      "Load a previous session's messages into context.",            "/session resume abc123"),
                ("save",             "Persist the current session to disk.",                       "/session save"),
                ("export <path>",    "Export session messages as JSON to a file.",                  "/session export /tmp/s.json"),
                ("help",             "Show this help.",                                             "/session help"),
            ),
        )
        return
    if args and args[0].lower() == "new":
        state.session_store.save(state.session)
        state.session = new_snapshot()
        state.history = []
        _reset_session_runtime_state(state)
        state.console.print(f"Started new session: {state.session.session_id}")
        return
    if args and args[0].lower() == "list":
        sessions = state.session_store.list_sessions()
        if not sessions:
            state.console.print("No saved sessions.")
            return
        table = Table(title="Sessions")
        table.add_column("Session")
        table.add_column("Updated")
        table.add_column("Summary")
        for session in sessions:
            table.add_row(session.session_id, session.updated_at, session.summary or "-")
        state.console.print(table)
        return
    if args and args[0].lower() == "resume" and len(args) > 1:
        state.session = state.session_store.load(args[1])
        state.history = list(state.session.messages)
        _reset_session_runtime_state(state)
        state.console.print(f"Resumed session: {state.session.session_id}")
        return
    if args and args[0].lower() == "save":
        state.session.messages = list(state.history)
        state.session_store.save(state.session)
        state.console.print(f"Saved session: {state.session.session_id}")
        return
    if args and args[0].lower() == "export" and len(args) > 1:
        export_path = Path(args[1])
        body = json.dumps([message_to_dict(message) for message in state.history], indent=2)
        export_path.write_text(body, encoding="utf-8")
        state.console.print(f"Exported session to {export_path}")
        return
    state.console.print(
        f"Session {state.session.session_id} with {len(state.history)} messages."
    )


def _reset_session_runtime_state(state: ReplState) -> None:
    state.carry_over = CarryOverState()
    state.current_system_prompt = ""
    state.current_turn_id = ""
    state.current_trace_id = ""
    state.approval_manager = ApprovalManager(policy=ApprovalPolicy(state.config.approval_policy))


async def handle_skills(state: ReplState, args: list[str]) -> None:
    subcommand = args[0].lower() if args else "list"
    if subcommand == "help":
        _print_subcommand_help(
            state, "skills", "Inspect and manage Agent Skills. Skills named subagent-* register specialist cognitive tools in advanced mode.",
            (
                ("(no args) / list", "List discovered skills with source, active state, description, and path.", "/skills"),
                ("available", "Alias for list.", "/skills available"),
                ("show <name>", "Print the skill's SKILL.md content.", "/skills show nexus-agent"),
                ("activate <name> / add", "Persist skill activation to workspace config.", "/skills activate nexus-agent"),
                ("deactivate <name> / remove", "Persist skill deactivation to workspace config.", "/skills deactivate nexus-agent"),
                ("create-local <name>", "Create .nexus/skills/<name>/SKILL.md from a valid template.", "/skills create-local code-review"),
                ("remove-local <name>", "Remove only a workspace-local skill directory.", "/skills remove-local code-review"),
                ("reload", "Rescan skills, refresh sub-agent tools and prompt.", "/skills reload"),
                ("help", "Show this help.", "/skills help"),
            ),
        )
        return
    if subcommand in {"list", "available", ""}:
        skills = state.skill_registry.all()
        if not skills:
            state.console.print("No skills loaded.")
            return
        table = Table(title="Skills")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Source")
        table.add_column("Active")
        table.add_column("Description")
        table.add_column("Path")
        for skill in skills:
            table.add_row(
                skill.name,
                "subagent" if _is_subagent_skill_name(skill.name) else "skill",
                skill.source,
                "yes" if skill.name in state.active_skills else "no",
                skill.description,
                str(skill.skill_path or "-"),
            )
        state.console.print(table)
        return
    if subcommand == "show" and len(args) > 1:
        skill = state.skill_registry.get(args[1])
        if skill is None:
            state.console.print(f"Skill not found: {args[1]}")
            return
        if skill.skill_path and skill.skill_path.exists():
            state.console.print(skill.skill_path.read_text(encoding="utf-8"))
        else:
            state.console.print(skill.content)
        return
    if subcommand in {"activate", "add"} and len(args) > 1:
        _set_skill_active(state, args[1], active=True)
        return
    if subcommand in {"deactivate", "remove"} and len(args) > 1:
        _set_skill_active(state, args[1], active=False)
        return
    if subcommand == "create-local" and len(args) > 1:
        _create_local_skill(state, args[1])
        return
    if subcommand == "remove-local" and len(args) > 1:
        _remove_local_skill(state, args[1])
        return
    if subcommand == "reload":
        _reload_skill_state(state)
        state.console.print("Reloaded skills.")
        return
    state.console.print("Usage: /skills [list|available|show <name>|activate <name>|deactivate <name>|create-local <name>|remove-local <name>|reload]")


def _set_skill_active(state: ReplState, skill_name: str, *, active: bool) -> None:
    skill = state.skill_registry.get(skill_name)
    if skill is None:
        state.console.print(f"Skill not found: {skill_name}")
        return
    payload = tomllib.loads(state.config.local_config_file.read_text(encoding="utf-8")) if state.config.local_config_file.exists() else {}
    enabled = _string_list(payload.get("enabled_skills", []))
    disabled = _string_list(payload.get("disabled_skills", []))
    if active:
        if skill.name not in enabled:
            enabled.append(skill.name)
        disabled = [name for name in disabled if name != skill.name]
        action = "Activated"
    else:
        enabled = [name for name in enabled if name != skill.name]
        if skill.name not in disabled:
            disabled.append(skill.name)
        state.run_skills = [name for name in state.run_skills if name != skill.name]
        action = "Deactivated"
    payload["enabled_skills"] = enabled
    payload["disabled_skills"] = disabled
    _write_toml(state.config.local_config_file, payload)
    _reload_config(state)
    _reload_skill_state(state)
    state.console.print(f"{action} skill: {skill.name}")


def _create_local_skill(state: ReplState, skill_name: str) -> None:
    try:
        validate_skill_metadata(
            {
                "name": skill_name,
                "description": f"Describe what {skill_name} helps with and when Nexus should use it.",
            },
            directory_name=skill_name,
        )
    except SkillParseError as exc:
        state.console.print(f"Invalid skill name: {exc}")
        return
    skill_dir = state.config.local_root / "skills" / skill_name
    skill_file = skill_dir / "SKILL.md"
    if skill_file.exists():
        state.console.print(f"Local skill already exists: {skill_name}")
        return
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts").mkdir()
    (skill_dir / "references").mkdir()
    (skill_dir / "assets").mkdir()
    skill_file.write_text(skill_template(skill_name), encoding="utf-8")
    _reload_skill_state(state)
    state.console.print(f"Created local skill: {skill_file}")


def _remove_local_skill(state: ReplState, skill_name: str) -> None:
    skill = state.skill_registry.get(skill_name)
    if skill is None:
        state.console.print(f"Skill not found: {skill_name}")
        return
    local_skills_root = (state.config.local_root / "skills").resolve()
    skill_dir = skill.skill_path.parent.resolve() if skill.skill_path else None
    if skill_dir is None or skill.source != "local" or skill_dir.parent != local_skills_root:
        state.console.print(f"Refusing to remove non-local skill: {skill_name}")
        return
    shutil.rmtree(skill_dir)
    if skill_name in state.active_skills:
        _set_skill_active(state, skill_name, active=False)
    else:
        _reload_skill_state(state)
    state.console.print(f"Removed local skill: {skill_name}")


def _reload_skill_state(state: ReplState) -> None:
    state.skill_registry = load_skill_registry(*get_skill_roots(state.config), config=state.config)
    state.active_skills = resolve_active_skill_names(
        state.skill_registry,
        state.config,
        extra=tuple(state.run_skills),
    )
    preserved_records = [record for record in state.tool_registry.records() if record.source != "agent-skill"]
    state.tool_registry.clear()
    for record in preserved_records:
        state.tool_registry.register(record.tool, source=record.source, origin=record.origin)
    register_skill_subagent_tools(
        state.tool_registry,
        state.config,
        state.skill_registry,
    )
    state.refresh_system_prompt()


def _is_subagent_skill_name(skill_name: str) -> bool:
    return skill_name.startswith("subagent-") or skill_name.startswith("subagent_")


async def handle_tools(state: ReplState, args: list[str]) -> None:
    subcommand = args[0].lower() if args else ""
    if subcommand == "help":
        _print_subcommand_help(
            state, "tools", "List or reload registered tools.",
            (
                ("(no args)", "Print a table of all tools: name, source, mutating flag, and description.", "/tools"),
                ("reload",   "Reload config and re-register builtin tools without restarting.",            "/tools reload"),
                ("help",     "Show this help.",                                                            "/tools help"),
            ),
        )
        return
    if subcommand == "reload":
        _reload_config(state)
        count = _reload_tools(state)
        state.console.print(f"[green]Tools reloaded.[/green] {count} tool(s) registered.")
        return
    table = Table(title="Registered Tools")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Source")
    table.add_column("Origin")
    table.add_column("Mutating")
    table.add_column("Description")
    for record in state.tool_registry.records():
        tool = record.tool
        kind = getattr(tool, "kind", "tool")
        table.add_row(
            record.name,
            getattr(kind, "value", str(kind)),
            record.source,
            record.origin or "-",
            "yes" if getattr(tool, "is_mutating", False) else "no",
            str(getattr(tool, "description", "")),
        )
    state.console.print(table)


async def handle_memory(state: ReplState, args: list[str]) -> None:
    if args and args[0].lower() == "help":
        _print_subcommand_help(
            state, "memory", "Inspect or update workspace memory entries.",
            (
                ("(no args) / list",       "List all memory entry keys.",                          "/memory list"),
                ("search <query>",         "Search memory entries by content.",                   "/memory search architecture"),
                ("save <key> <content>",   "Save a new memory entry under a key.",                "/memory save goals Improve test coverage"),
                ("show <key>",             "Print the content of a memory entry.",                "/memory show goals"),
                ("help",                   "Show this help.",                                     "/memory help"),
            ),
        )
        return
    if not args or args[0] == "list":
        keys = state.memory_store.list_keys()
        state.console.print("\n".join(keys) if keys else "No memory entries.")
        return
    subcommand = args[0].lower()
    if subcommand == "search" and len(args) > 1:
        matches = state.memory_store.search(" ".join(args[1:]))
        state.console.print("\n\n".join(entry.content for entry in matches) if matches else "No matches.")
        return
    if subcommand == "save" and len(args) > 2:
        key = args[1]
        content = " ".join(args[2:])
        state.memory_store.save(MemoryEntry(key=key, content=content, keywords=(key,)))
        state.console.print(f"Saved memory entry: {key}")
        return
    if subcommand == "show" and len(args) > 1:
        entry = state.memory_store.load(args[1])
        state.console.print(entry.content if entry else "Memory entry not found.")
        return
    state.console.print("Usage: /memory list|search <query>|save <key> <content>|show <key>")


async def handle_context(state: ReplState, args: list[str]) -> None:
    subcommand = args[0].lower() if args else "show"

    if subcommand == "help":
        _print_subcommand_help(
            state, "context", "Show system prompt or context/token usage stats.",
            (
                ("(no args) / show", "Print the current assembled system prompt.",                              "/context"),
                ("usage",            "Show token usage table: model, context window, history tokens,",          "/context usage"),
                ("",                 "compaction thresholds, and % of context consumed.",                       ""),
                ("usage <agent>",     "Show context usage for a supervisor or sub-agent.",                       "/context usage supervisor"),
                ("agents",           "List known supervisor and sub-agent context snapshots.",                   "/context agents"),
                ("agent <id>",        "Show one recorded agent context snapshot.",                              "/context agent supervisor"),
                ("task <id>",         "Show one typed multi-agent task context.",                               "/context task execute"),
                ("summary",           "Show typed multi-agent session summary.",                                "/context summary"),
                ("help",             "Show this help.",                                                         "/context help"),
            ),
        )
        return

    if subcommand == "usage":
        if len(args) > 1:
            _print_agent_context_usage(state, args[1])
            return
        _print_supervisor_context_usage(state)
        return

    if subcommand == "agents":
        _print_context_agents(state)
        return

    if subcommand == "agent" and len(args) > 1:
        _print_agent_context(state, args[1])
        return

    if subcommand == "task" and len(args) > 1:
        _print_context_task(state, args[1])
        return

    if subcommand == "summary":
        _print_context_summary(state)
        return

    # Default: show system prompt
    state.console.print(state.current_system_prompt or "Context not built yet.")


def _print_supervisor_context_usage(state: ReplState) -> None:
    estimator = TokenEstimator()
    history_tokens = sum(estimator.estimate(m.content) for m in state.history)
    system_tokens = estimator.estimate(state.current_system_prompt or "")
    definition_tokens = _supervisor_definition_token_breakdown(state, estimator)
    schema_tokens = (
        definition_tokens["tool_schemas"]
        + definition_tokens["subagent_schemas"]
        + definition_tokens["mcp_schemas"]
    )
    total_estimated = history_tokens + system_tokens + schema_tokens
    ctx_limit = get_model_context_limit(state.config.model_name)
    soft = state.config.compaction_soft_limit
    hard = state.config.compaction_hard_limit
    pct = round(total_estimated / ctx_limit * 100, 1) if ctx_limit else 0.0

    table = Table(title="Context Usage: supervisor")
    table.add_column("Field")
    table.add_column("Value", justify="right")
    table.add_row("Provider", state.config.provider)
    table.add_row("Model", state.config.model_name)
    table.add_row("Context window", f"{ctx_limit:,} tokens")
    table.add_row("System prompt (est.)", f"{system_tokens:,} tokens")
    table.add_row("History (est.)", f"{history_tokens:,} tokens")
    table.add_row("Tool schemas (est.)", f"{definition_tokens['tool_schemas']:,} tokens")
    table.add_row("Sub-agent schemas (est.)", f"{definition_tokens['subagent_schemas']:,} tokens")
    table.add_row("MCP schemas (est.)", f"{definition_tokens['mcp_schemas']:,} tokens")
    table.add_row("Active skills prompt (est.)", f"{definition_tokens['skills_prompt']:,} tokens")
    table.add_row("MCP prompt guidance (est.)", f"{definition_tokens['mcp_prompt']:,} tokens")
    table.add_row("Total used incl. schemas (est.)", f"{total_estimated:,} tokens  ({pct}%)")
    table.add_row("Compaction soft limit", f"{soft:,} tokens  ({round(soft/ctx_limit*100,1)}%)")
    table.add_row("Compaction hard limit", f"{hard:,} tokens  ({round(hard/ctx_limit*100,1)}%)")
    state.console.print(table)


def _supervisor_definition_token_breakdown(state: ReplState, estimator: TokenEstimator) -> dict[str, int]:
    from nexus.tools.base import tool_to_schema

    available = supervisor_tool_names(state.config, state.tool_registry)
    buckets = {
        "tool_schemas": 0,
        "subagent_schemas": 0,
        "mcp_schemas": 0,
        "skills_prompt": 0,
        "mcp_prompt": 0,
    }
    for record in state.tool_registry.records():
        if record.name not in available:
            continue
        schema_text = json.dumps(tool_to_schema(record.tool), sort_keys=True)
        if record.source == "mcp":
            buckets["mcp_schemas"] += estimator.estimate(schema_text)
        elif record.name.startswith("subagent_"):
            buckets["subagent_schemas"] += estimator.estimate(schema_text)
        else:
            buckets["tool_schemas"] += estimator.estimate(schema_text)

    if state.skill_registry.all():
        active = set(supervisor_skill_names(state.config, state.active_skills))
        buckets["skills_prompt"] = estimator.estimate(state.skill_registry.summary(active=active))

    prompt = state.current_system_prompt or ""
    marker = "## MCP Tool Contract"
    if marker in prompt:
        mcp_section = prompt.split(marker, 1)[1].split("\n\n## ", 1)[0]
        buckets["mcp_prompt"] = estimator.estimate(marker + mcp_section)
    return buckets


async def handle_history(state: ReplState, args: list[str]) -> None:
    if args and args[0].lower() == "help":
        _print_subcommand_help(
            state, "history", "Show recent conversation messages.",
            (
                ("(no args)",  "Print all messages in the current session.",     "/history"),
                ("<n>",        "Print the last n messages only.",                 "/history 5"),
                ("help",       "Show this help.",                                 "/history help"),
            ),
        )
        return
    count = int(args[0]) if args else len(state.history)
    recent = state.history[-count:]
    if not recent:
        state.console.print("No history.")
        return
    for message in recent:
        state.console.print(f"[{message.role}] {message.content}")


async def handle_quit(state: ReplState, args: list[str]) -> None:
    del args
    state.should_exit = True
    state.console.print("Saving session and exiting.")


async def handle_abort(state: ReplState, args: list[str]) -> None:
    del args
    if state.request_abort_current_turn():
        state.console.print("[yellow]Abort requested for the current turn.[/yellow]")
        return
    state.console.print("No running turn to abort.")


def _context_agent_records(state: ReplState) -> dict[str, dict]:
    records: dict[str, dict] = {}
    payload = get_context_payload(state.session.metadata)
    agents = payload.get("agents", {})
    if isinstance(agents, dict):
        for agent_id, record in agents.items():
            if isinstance(record, dict):
                records[str(agent_id)] = record
    return records


def _print_context_agents(state: ReplState) -> None:
    records = _context_agent_records(state)
    if not records:
        state.console.print("No agent context snapshots recorded yet.")
        return
    table = Table(title="Agent Contexts")
    table.add_column("Agent")
    table.add_column("Role")
    table.add_column("Scope")
    table.add_column("Tokens")
    table.add_column("Messages")
    table.add_column("Tools")
    for agent_id, record in sorted(records.items()):
        tools = record.get("allowed_tools", [])
        table.add_row(
            agent_id,
            str(record.get("role", "-")),
            str(record.get("scope", "-")),
            str(record.get("token_estimate", "-")),
            str(record.get("message_count", "-")),
            ", ".join(str(tool) for tool in tools) if isinstance(tools, list) and tools else "-",
        )
    state.console.print(table)


def _print_agent_context(state: ReplState, agent_id: str) -> None:
    records = _context_agent_records(state)
    record = records.get(agent_id)
    if record is None:
        state.console.print(f"Agent context not found: {agent_id}")
        return
    state.console.print(json.dumps(record, indent=2))


def _print_agent_context_usage(state: ReplState, agent_id: str) -> None:
    records = _context_agent_records(state)
    record = records.get(agent_id)
    if record is None:
        state.console.print(f"Agent context not found: {agent_id}")
        return
    ctx_limit = get_model_context_limit(state.config.model_name)
    tokens = int(record.get("token_estimate", 0) or 0)
    pct = round(tokens / ctx_limit * 100, 1) if ctx_limit else 0.0
    table = Table(title=f"Context Usage: {agent_id}")
    table.add_column("Field")
    table.add_column("Value", justify="right")
    table.add_row("Role", str(record.get("role", "-")))
    table.add_row("Scope", str(record.get("scope", "-")))
    table.add_row("Token estimate", f"{tokens:,} tokens")
    table.add_row("Context window", f"{ctx_limit:,} tokens")
    table.add_row("Used", f"{pct}%")
    table.add_row("Messages", str(record.get("message_count", "-")))
    table.add_row("Tool calls", str(record.get("tool_call_count", "-")))
    state.console.print(table)


def _print_context_task(state: ReplState, task_id: str) -> None:
    typed = load_multi_agent_state(state.session.metadata)
    task = typed.tasks.get(task_id)
    if task is None:
        state.console.print(f"Task context not found: {task_id}")
        return
    state.console.print(json.dumps(task.to_dict(), indent=2))


def _print_context_summary(state: ReplState) -> None:
    typed = load_multi_agent_state(state.session.metadata)
    summary = {
        "objective": typed.objective,
        "tasks": len(typed.tasks),
        "agents": len(typed.agents),
        "packets": len(typed.packets),
        "artifacts": len(typed.artifacts),
        "events": len(typed.events),
        "latest_packets": [render_context_packet(packet) for packet in typed.packets[-3:]],
    }
    if typed.latest_summary is not None:
        summary["rolling_summary"] = typed.latest_summary.to_dict()
    state.console.print(json.dumps(summary, indent=2))


def _print_agent_status(state: ReplState) -> None:
    tools = supervisor_tool_names(state.config, state.tool_registry)
    skills = supervisor_skill_names(state.config, state.active_skills)
    allowed_tools = clean_string_list(getattr(state.config, "agent_allowed_tools", []))
    allowed_skills = clean_string_list(getattr(state.config, "agent_allowed_skills", []))
    allowed_mcp_servers = clean_string_list(getattr(state.config, "agent_allowed_mcp_servers", []))
    table = Table(title="Supervisor Agent")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Mode", str(state.config.agent_mode))
    table.add_row("Effective tools", str(len(tools)))
    table.add_row("Effective skills", str(len(skills)))
    table.add_row("Configured allowed tools", ", ".join(allowed_tools) or "default")
    table.add_row("Configured allowed skills", ", ".join(allowed_skills) or "default")
    table.add_row("Configured allowed MCP servers", ", ".join(allowed_mcp_servers) or "default")
    state.console.print(table)


def _print_agent_tools(state: ReplState) -> None:
    available = supervisor_tool_names(state.config, state.tool_registry)
    configured_scope = getattr(state.config, "agent_allowed_tools", [])
    configured_mcp_scope = getattr(state.config, "agent_allowed_mcp_servers", [])
    configured = set(clean_string_list(configured_scope))
    configured_mcp = set(clean_string_list(configured_mcp_scope))
    configured_mcp_tools = mcp_tool_names_for_servers(state.tool_registry, configured_mcp)
    table = Table(title="Supervisor Tools")
    table.add_column("Tool")
    table.add_column("Source")
    table.add_column("State")
    table.add_column("Description")
    for record in state.tool_registry.records():
        if (record.source != "mcp" and is_all_scope(configured_scope)) or (record.source == "mcp" and is_all_scope(configured_mcp_scope)):
            scope = "allowed"
        elif record.name in configured or record.name in configured_mcp_tools:
            scope = "allowed"
        elif record.name in available:
            scope = "default"
        else:
            scope = "unavailable"
        table.add_row(record.name, record.source, scope, record.tool.description)
    state.console.print(table)


def _print_agent_skills(state: ReplState) -> None:
    effective = set(supervisor_skill_names(state.config, state.active_skills))
    configured_scope = getattr(state.config, "agent_allowed_skills", [])
    configured = set(clean_string_list(configured_scope))
    table = Table(title="Supervisor Skills")
    table.add_column("Skill")
    table.add_column("Global Active")
    table.add_column("State")
    table.add_column("Description")
    for skill in state.skill_registry.all():
        globally_active = skill.name in state.active_skills
        if is_all_scope(configured_scope) and globally_active:
            scope = "allowed"
        elif skill.name in configured:
            scope = "allowed"
        elif skill.name in effective:
            scope = "default"
        else:
            scope = "unavailable"
        table.add_row(skill.name, "yes" if globally_active else "no", scope, skill.description)
    state.console.print(table)


def _print_agent_mcp(state: ReplState) -> None:
    configured_scope = getattr(state.config, "agent_allowed_mcp_servers", [])
    configured = set(clean_string_list(configured_scope))
    table = Table(title="Supervisor MCP Servers")
    table.add_column("Server")
    table.add_column("Global Active")
    table.add_column("State")
    table.add_column("Tools")
    for server in state.mcp_servers:
        if is_all_scope(configured_scope):
            scope = "allowed"
        elif server.server.name in configured:
            scope = "allowed"
        elif not configured:
            scope = "default"
        else:
            scope = "unavailable"
        table.add_row(server.server.name, "yes", scope, ", ".join(server.registered_tools) or "-")
    state.console.print(table)


def _set_agent_resource_allowed(state: ReplState, kind: str, name: str, *, allowed: bool) -> bool:
    normalized = _normalize_resource_name(state, kind, name, require_active=allowed)
    if normalized is None:
        return False
    field_name = {
        "tool": "allowed_tools",
        "skill": "allowed_skills",
        "mcp": "allowed_mcp_servers",
    }.get(kind)
    if field_name is None:
        state.console.print("Resource kind must be one of: tool, skill, mcp")
        return False
    payload = tomllib.loads(state.config.local_config_file.read_text(encoding="utf-8")) if state.config.local_config_file.exists() else {}
    agent_scope = _payload_agent_scope(payload)
    _set_allowed_list_value(
        agent_scope,
        field_name,
        normalized,
        allowed=allowed,
        current_values=_current_supervisor_resource_names(state, kind),
    )
    _write_toml(state.config.local_config_file, payload)
    _reload_config(state)
    action = "Allowed" if allowed else "Disallowed"
    state.console.print(f"{action} {kind} for supervisor: {normalized}")
    return True


def _print_subagent_list(state: ReplState) -> None:
    records = _subagent_records(state)
    if not records:
        state.console.print("No sub-agent tools registered. Set agent_mode = \"advanced\" or add [[sub-agents]] entries, then reload tools.")
        return
    table = Table(title="Sub-Agents")
    table.add_column("Name")
    table.add_column("Tool")
    table.add_column("Source")
    table.add_column("Description")
    for record in records:
        definition = getattr(record.tool, "_definition", None)
        name = getattr(definition, "name", record.name.removeprefix("subagent_"))
        table.add_row(name, record.name, record.source, record.tool.description)
    state.console.print(table)


def _print_subagent_show(state: ReplState, record) -> None:
    definition = getattr(record.tool, "_definition", None)
    name = getattr(definition, "name", record.name.removeprefix("subagent_"))
    table = Table(title=f"Sub-Agent: {name}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Tool", record.name)
    table.add_row("Source", record.source)
    table.add_row("Description", record.tool.description)
    table.add_row("Effective tools", ", ".join(_effective_subagent_tools(state, record)) or "-")
    table.add_row("Allowed skills", ", ".join(_effective_subagent_skills(state, record)) or "-")
    table.add_row("Active MCP tools", ", ".join(_effective_subagent_mcp_tools(state, record)) or "-")
    state.console.print(table)


def _print_subagent_tools(state: ReplState, record) -> None:
    effective = set(_effective_subagent_tools(state, record))
    definition = getattr(record.tool, "_definition", None)
    profile = subagent_profile(state.config, getattr(definition, "name", record.name))
    configured_scope = profile.get("allowed_tools", [])
    configured_mcp_scope = profile.get("allowed_mcps", []) or profile.get("allowed_mcp_servers", [])
    configured = set(clean_string_list(configured_scope))
    configured_mcp = set(clean_string_list(configured_mcp_scope))
    configured_mcp_tools = mcp_tool_names_for_servers(state.tool_registry, configured_mcp)
    table = Table(title=f"Sub-Agent Tools: {record.name}")
    table.add_column("Tool")
    table.add_column("Source")
    table.add_column("State")
    table.add_column("Description")
    for tool_record in state.tool_registry.records():
        if tool_record.name.startswith("subagent_") or tool_record.name == "delegate_task":
            continue
        if (tool_record.source != "mcp" and is_all_scope(configured_scope)) or (tool_record.source == "mcp" and is_all_scope(configured_mcp_scope)):
            scope = "allowed"
        elif tool_record.name in configured or tool_record.name in configured_mcp_tools:
            scope = "allowed"
        elif tool_record.name in effective:
            scope = "available"
        else:
            scope = "unavailable"
        table.add_row(tool_record.name, tool_record.source, scope, tool_record.tool.description)
    state.console.print(table)


def _print_subagent_skills(state: ReplState, record) -> None:
    effective = set(_effective_subagent_skills(state, record))
    definition = getattr(record.tool, "_definition", None)
    profile = subagent_profile(state.config, getattr(definition, "name", record.name))
    configured_scope = profile.get("allowed_skills", [])
    configured = set(clean_string_list(configured_scope))
    table = Table(title=f"Sub-Agent Skills: {record.name}")
    table.add_column("Skill")
    table.add_column("Global Active")
    table.add_column("State")
    table.add_column("Description")
    for skill in state.skill_registry.all():
        if is_all_scope(configured_scope) and skill.name in state.active_skills:
            scope = "allowed"
        elif skill.name in configured:
            scope = "allowed"
        elif skill.name in effective:
            scope = "available"
        else:
            scope = "unavailable"
        table.add_row(skill.name, "yes" if skill.name in state.active_skills else "no", scope, skill.description)
    state.console.print(table)


def _print_subagent_mcp(state: ReplState, record) -> None:
    effective_tools = set(_effective_subagent_tools(state, record))
    definition = getattr(record.tool, "_definition", None)
    profile = subagent_profile(state.config, getattr(definition, "name", record.name))
    configured_scope = profile.get("allowed_mcps", []) or profile.get("allowed_mcp_servers", [])
    configured = set(clean_string_list(configured_scope))
    table = Table(title=f"Sub-Agent MCP: {record.name}")
    table.add_column("Server")
    table.add_column("State")
    table.add_column("Effective Tools")
    for server in state.mcp_servers:
        server_tools = [tool for tool in server.registered_tools if tool in effective_tools]
        if is_all_scope(configured_scope):
            scope = "allowed"
        elif server.server.name in configured:
            scope = "allowed"
        elif server_tools:
            scope = "available"
        else:
            scope = "unavailable"
        table.add_row(server.server.name, scope, ", ".join(server_tools) or "-")
    state.console.print(table)


def _set_subagent_resource_allowed(state: ReplState, record, kind: str, name: str, *, allowed: bool) -> bool:
    normalized_resource = _normalize_resource_name(state, kind, name, require_active=allowed)
    if normalized_resource is None:
        return False
    definition = getattr(record.tool, "_definition", None)
    subagent_name = normalize_subagent_name(getattr(definition, "name", record.name))
    field_name = {
        "tool": "allowed_tools",
        "skill": "allowed_skills",
        "mcp": "allowed_mcps",
    }
    target_field = field_name.get(kind)
    if target_field is None:
        state.console.print("Resource kind must be one of: tool, skill, mcp")
        return False
    payload = tomllib.loads(state.config.local_config_file.read_text(encoding="utf-8")) if state.config.local_config_file.exists() else {}
    profile = _payload_subagent_profile(payload, subagent_name)
    _set_allowed_list_value(
        profile,
        target_field,
        normalized_resource,
        allowed=allowed,
        current_values=_current_subagent_resource_names(state, record, kind),
    )
    _write_toml(state.config.local_config_file, payload)
    _reload_config(state)
    action = "Allowed" if allowed else "Disallowed"
    state.console.print(f"{action} {kind} for sub-agent {subagent_name}: {normalized_resource}")
    return True


def _normalize_resource_name(state: ReplState, kind: str, name: str, *, require_active: bool) -> str | None:
    if kind == "tool":
        try:
            return state.tool_registry.record(name).name
        except LookupError:
            state.console.print(f"Tool not found: {name}")
            return None
    if kind == "skill":
        skill = state.skill_registry.get(name)
        if skill is None:
            state.console.print(f"Skill not found: {name}")
            return None
        if require_active and skill.name not in state.active_skills:
            state.console.print(f"Skill is not globally active. Activate it first with /skills activate {skill.name}.")
            return None
        return skill.name
    if kind == "mcp":
        active = _active_mcp_server_names(state)
        if require_active and name not in active:
            state.console.print(f"MCP server is not globally active. Activate it first with /mcp activate {name}.")
            return None
        return name
    state.console.print("Resource kind must be one of: tool, skill, mcp")
    return None


def _subagent_records(state: ReplState) -> list:
    return [record for record in state.tool_registry.records() if record.name.startswith("subagent_")]


def _subagent_record(state: ReplState, name: str):
    target_tool = subagent_tool_name(name)
    target_name = normalize_subagent_name(name)
    for record in _subagent_records(state):
        definition = getattr(record.tool, "_definition", None)
        definition_name = normalize_subagent_name(getattr(definition, "name", record.name))
        if record.name == target_tool or definition_name == target_name:
            return record
    return None


def _effective_subagent_tools(state: ReplState, record) -> list[str]:
    definition = getattr(record.tool, "_definition", None)
    names = subagent_tool_names(
        state.config,
        state.tool_registry,
        getattr(definition, "name", record.name),
        base_allowed_tools=getattr(definition, "allowed_tools", None),
        base_allowed_mcps=getattr(definition, "allowed_mcps", ()),
    )
    return sorted(names)


def _effective_subagent_skills(state: ReplState, record) -> list[str]:
    definition = getattr(record.tool, "_definition", None)
    return subagent_skill_names(
        state.config,
        getattr(definition, "name", record.name),
        state.active_skills,
        base_allowed_skills=getattr(definition, "allowed_skills", ()),
    )


def _effective_subagent_mcp_tools(state: ReplState, record) -> list[str]:
    effective = set(_effective_subagent_tools(state, record))
    return sorted(record.name for record in state.tool_registry.records() if record.source == "mcp" and record.name in effective)


def _active_mcp_server_names(state: ReplState) -> set[str]:
    names = {str(entry.get("name", "")).strip() for entry in state.config.mcp_servers if isinstance(entry, dict)}
    names.update(server.server.name for server in state.mcp_servers)
    return {name for name in names if name}


def _current_supervisor_resource_names(state: ReplState, kind: str) -> list[str]:
    if kind == "tool":
        return sorted(
            name
            for name in supervisor_tool_names(state.config, state.tool_registry)
            if state.tool_registry.record(name).source != "mcp"
            and not name.startswith("subagent_")
            and name != "delegate_task"
        )
    if kind == "skill":
        return supervisor_skill_names(state.config, state.active_skills)
    if kind == "mcp":
        effective_tools = supervisor_tool_names(state.config, state.tool_registry)
        return sorted(
            {
                record.origin
                for record in state.tool_registry.records()
                if record.source == "mcp" and record.name in effective_tools and record.origin
            }
        )
    return []


def _current_subagent_resource_names(state: ReplState, record, kind: str) -> list[str]:
    if kind == "tool":
        return sorted(
            name
            for name in _effective_subagent_tools(state, record)
            if state.tool_registry.record(name).source != "mcp"
        )
    if kind == "skill":
        return _effective_subagent_skills(state, record)
    if kind == "mcp":
        effective_tools = set(_effective_subagent_tools(state, record))
        return sorted(
            {
                tool_record.origin
                for tool_record in state.tool_registry.records()
                if tool_record.source == "mcp" and tool_record.name in effective_tools and tool_record.origin
            }
        )
    return []


def _set_allowed_list_value(
    payload: dict[str, object],
    field_name: str,
    value: str,
    *,
    allowed: bool,
    current_values: list[str],
) -> None:
    existing = clean_string_list(payload.get(field_name, []))
    if is_all_scope(existing):
        values = [item for item in current_values if item != value]
    elif existing:
        values = list(existing)
    else:
        values = list(current_values)
    if allowed and value not in values:
        values.append(value)
    if not allowed:
        values = [item for item in values if item != value]
    payload[field_name] = values


def _payload_subagent_profile(payload: dict[str, object], name: str) -> dict[str, object]:
    profiles = payload.get("sub-agents")
    if not isinstance(profiles, list):
        profiles = payload.pop("subagent_profiles", None)
    if not isinstance(profiles, list):
        profiles = []
        payload["sub-agents"] = profiles
    else:
        payload["sub-agents"] = profiles
    for entry in profiles:
        if not isinstance(entry, dict):
            continue
        if normalize_subagent_name(str(entry.get("name", ""))) == name:
            for field_name in ("allowed_tools", "allowed_skills", "allowed_mcps"):
                entry.setdefault(field_name, [])
            return entry
    entry: dict[str, object] = {"name": name}
    for field_name in ("allowed_tools", "allowed_skills", "allowed_mcps"):
        entry[field_name] = []
    profiles.append(entry)
    return entry


def _payload_agent_scope(payload: dict[str, object]) -> dict[str, object]:
    agents = payload.get("agents")
    if not isinstance(agents, dict):
        agents = {}
        payload["agents"] = agents
    return agents


def _print_mcp_status(state: ReplState) -> None:
    table = Table(title="MCP Servers")
    table.add_column("Name")
    table.add_column("Transport")
    table.add_column("Status")
    table.add_column("Prefix")
    table.add_column("Registered")
    table.add_column("Discovered")
    table.add_column("Last Checked")
    table.add_column("Last Error")
    for server in state.mcp_servers:
        table.add_row(
            server.server.name,
            server.server.transport,
            "connected" if server.connected else "disconnected",
            server.server.prefix or "-",
            str(len(server.registered_tools)),
            str(len(server.discovered_tools)),
            server.last_checked_at or "-",
            server.last_error or "-",
        )
    state.console.print(table)


def _print_mcp_help(state: ReplState) -> None:
    _print_subcommand_help(
        state, "mcp", "Inspect and manage MCP server status and tools.",
        (
            ("status",           "Show MCP server status, transport, counts, and last error.", "/mcp status"),
            ("available",        "List MCP servers defined globally or locally and their workspace state.", "/mcp available"),
            ("activate <server>", "Enable a global or local MCP server for this workspace.",     "/mcp activate filesystem"),
            ("deactivate <server>", "Disable an MCP server for this workspace.",                 "/mcp deactivate filesystem"),
            ("tools",            "List registered and discovered MCP tools.",                  "/mcp tools"),
            ("refresh",          "Rediscover and hot-register loaded MCP tools.",              "/mcp refresh"),
            ("refresh <server>", "Rediscover and hot-register one loaded server.",             "/mcp refresh filesystem"),
            ("reload",           "Reload config, restart MCP servers, and register tools.",    "/mcp reload"),
            ("help",             "Show this help.",                                           "/mcp help"),
        ),
    )


def _print_mcp_tools(state: ReplState) -> None:
    table = Table(title="MCP Tools")
    table.add_column("Server")
    table.add_column("Nexus Name")
    table.add_column("Remote Name")
    table.add_column("State")
    table.add_column("Description")
    for server in state.mcp_servers:
        registered = set(server.registered_tools)
        disabled = set(server.server.disabled_tools)
        if not server.discovered_specs:
            table.add_row(server.server.name, "-", "-", "none", "-")
            continue
        for spec in server.discovered_specs:
            display_name = server.display_name(spec.name)
            if server.server.disabled or spec.name in disabled:
                state_text = "disabled"
            elif display_name in registered:
                state_text = "enabled"
            else:
                state_text = "unregistered"
            description = spec.description.replace("\n", " ")[:80] or "-"
            table.add_row(server.server.name, display_name, spec.name, state_text, description)
    state.console.print(table)


def _print_mcp_available(state: ReplState) -> None:
    catalog = _mcp_server_catalog(state)
    if not catalog:
        state.console.print("No MCP servers configured globally or locally.")
        return
    active_names = {str(entry.get("name", "")).strip() for entry in state.config.mcp_servers}
    enabled = set(getattr(state.config, "enabled_mcp_servers", []))
    disabled = set(getattr(state.config, "disabled_mcp_servers", []))
    table = Table(title="Available MCP Servers")
    table.add_column("Name")
    table.add_column("Scope")
    table.add_column("State")
    table.add_column("Transport")
    table.add_column("Prefix")
    table.add_column("Command/URL")
    for name, entry in sorted(catalog.items()):
        if name in active_names:
            state_text = "active"
        elif name in disabled:
            state_text = "disabled"
        elif name in enabled:
            state_text = "missing"
        else:
            state_text = "available"
        command = entry.get("url") or " ".join(str(part) for part in entry.get("command", []))
        table.add_row(
            name,
            str(entry.get("_scope", "-")),
            state_text,
            str(entry.get("transport", "stdio")),
            str(entry.get("prefix", "")) or "-",
            command or "-",
        )
    state.console.print(table)


async def _set_mcp_server_active(state: ReplState, server_name: str, *, active: bool) -> None:
    catalog = _mcp_server_catalog(state)
    if server_name not in catalog:
        state.console.print(f"MCP server not found in global or local config: {server_name}")
        return

    payload = tomllib.loads(state.config.local_config_file.read_text(encoding="utf-8")) if state.config.local_config_file.exists() else {}
    enabled = _string_list(payload.get("enabled_mcp_servers", []))
    disabled = _string_list(payload.get("disabled_mcp_servers", []))
    if active:
        if server_name not in enabled:
            enabled.append(server_name)
        disabled = [name for name in disabled if name != server_name]
        action = "Activated"
    else:
        enabled = [name for name in enabled if name != server_name]
        if server_name not in disabled:
            disabled.append(server_name)
        action = "Deactivated"
    payload["enabled_mcp_servers"] = enabled
    payload["disabled_mcp_servers"] = disabled
    _write_toml(state.config.local_config_file, payload)

    reports = await _reload_mcp_servers(state)
    _refresh_system_prompt_after_mcp_change(state)
    state.console.print(f"{action} MCP server for this workspace: {server_name}")
    if reports:
        _print_mcp_refresh_reports(state, reports, title="MCP Reload")


def _mcp_server_catalog(state: ReplState) -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    for scope, path in (("global", state.config.global_config_file), ("local", state.config.local_config_file)):
        if not path.exists():
            continue
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        servers = payload.get("mcp_servers", [])
        if not isinstance(servers, list):
            continue
        for entry in servers:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            record = dict(entry)
            record["_scope"] = scope
            catalog[name] = record
    return catalog


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _refresh_system_prompt_after_mcp_change(state: ReplState) -> None:
    state.refresh_system_prompt()


async def _refresh_mcp_servers(state: ReplState, target_name: str | None) -> list[MCPRefreshReport]:
    reports: list[MCPRefreshReport] = []
    for server in state.mcp_servers:
        if target_name is not None and server.server.name != target_name:
            continue
        reports.append(await refresh_mcp_server_tools(server, state.tool_registry, state.config))
    return reports


async def _reload_mcp_servers(state: ReplState) -> list[MCPRefreshReport]:
    for server in state.mcp_servers:
        await server.close()

    state.tool_registry.unregister_source(source="mcp")
    state.mcp_servers.clear()
    _reload_config(state)

    reports: list[MCPRefreshReport] = []
    for payload in state.config.mcp_servers:
        try:
            runtime = MCPServerRuntime(server=MCPServerConfig.from_dict(payload))
            await runtime.refresh()
            state.mcp_servers.append(runtime)
            if runtime.last_error:
                reports.append(MCPRefreshReport(server=runtime.server.name, failed=runtime.last_error))
                continue
            registered = register_discovered_mcp_tools(runtime, state.tool_registry, state.config)
            reports.append(MCPRefreshReport(server=runtime.server.name, added=tuple(sorted(registered))))
        except Exception as exc:
            name = str(payload.get("name", "unknown")) if isinstance(payload, dict) else "unknown"
            reports.append(MCPRefreshReport(server=name, failed=str(exc)))
    return reports


def _print_mcp_refresh_reports(state: ReplState, reports: list[MCPRefreshReport], *, title: str = "MCP Refresh") -> None:
    table = Table(title=title)
    table.add_column("Server")
    table.add_column("Added")
    table.add_column("Removed")
    table.add_column("Unchanged")
    table.add_column("Error")
    for report in reports:
        table.add_row(
            report.server,
            ", ".join(report.added) or "-",
            ", ".join(report.removed) or "-",
            str(len(report.unchanged)),
            report.failed or "-",
        )
    state.console.print(table)


async def _handle_config_upgrade(state: ReplState, scope: str) -> None:
    """Merge any new template keys into the existing local or global config file.

    Existing values are **never** modified — only keys that are absent from the
    on-disk file are appended.  Memory, sessions, and storage are untouched.
    """
    if scope == "global":
        path = state.config.global_config_file
        template_str = _global_config_toml()
        label = "global"
    elif scope == "local":
        path = state.config.local_config_file
        template_str = _local_config_toml(
            workspace_root=state.config.workspace_root,
            project_name=state.config.project_name or state.config.workspace_root.name,
            project_description=state.config.project_description,
        )
        label = "local"
    else:
        state.console.print(f"[red]Unknown scope '{scope}'. Use 'local' or 'global'.[/red]")
        return

    report = inspect_config_upgrade(path, template_str)
    if not report.needs_upgrade:
        _reload_config(state)
        count = _reload_tools(state)
        state.console.print(
            f"[green]{label.capitalize()} config is already up to date — no new keys to add. Config, .env, and {count} tool(s) reloaded.[/green]"
        )
        return

    upgrade_config_file(path, template_str)
    _reload_config(state)
    count = _reload_tools(state)
    state.console.print(
        f"[green]Upgraded {label} config at {path}, then reloaded config, .env, and {count} tool(s):[/green]"
    )
    for key in report.deprecated_keys:
        state.console.print(f"  [bold]-[/bold] removed deprecated {key}")
    for key in report.missing_keys:
        state.console.print(f"  [bold]+[/bold] {key}")
    if report.agent_scope_migrated:
        state.console.print("  [bold]~[/bold] migrated agent scope to [agents]")
    if report.subagent_scope_migrated:
        state.console.print("  [bold]~[/bold] migrated sub-agent scope to [[sub-agents]]")
    for tool_name in report.allowed_tool_additions:
        state.console.print(f"  [bold]+[/bold] allowed_tools: {tool_name}")


def _reload_config(state: ReplState) -> None:
    state.config = load_config(
        state.config.workspace_root,
        global_root=state.config.global_root,
        local_config_path=state.config.local_config_file,
        global_config_path=state.config.global_config_file,
        strict=False,
    )
    state.reload_model_client()
    state.refresh_system_prompt()
    for warning in getattr(state.config, "config_warnings", []) or []:
        print_warning = getattr(state.console, "print_warning", None)
        if print_warning is not None:
            print_warning(warning)
        else:
            state.console.print(f"Warning: {warning}")


def _reload_tools(state: ReplState) -> int:
    from nexus.tools.registry import register_core_tools, tool_enabled

    cfg = state.config
    rebuilt_sources = {"core", "agent", "agent-skill", "agent-yaml"}
    preserved_records = [
        record
        for record in state.tool_registry.records()
        if record.source not in rebuilt_sources
    ]

    state.tool_registry.clear()
    register_core_tools(state.tool_registry, cfg)

    for record in preserved_records:
        if record.source == "mcp" or tool_enabled(cfg, record.name):
            state.tool_registry.register(record.tool, source=record.source, origin=record.origin)

    register_subagent_tools(
        state.tool_registry,
        cfg,
        definitions=load_subagent_definitions(cfg),
    )
    register_skill_subagent_tools(
        state.tool_registry,
        cfg,
        state.skill_registry,
    )
    register_yaml_subagent_tools(
        state.tool_registry,
        cfg,
    )
    return len(state.tool_registry.records())


def _update_toml_value(path: Path, key: str, value: str) -> None:
    existing = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing[key] = _coerce_toml_value(value)
    _write_toml(path, existing)


def _remove_toml_key(path: Path, key: str) -> None:
    existing = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing.pop(key, None)
    _write_toml(path, existing)


def _coerce_toml_value(value: str):
    stripped = value.strip()
    if stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    if stripped.isdigit():
        return int(stripped)
    try:
        return float(stripped)
    except ValueError:
        pass
    if stripped.startswith("[") and stripped.endswith("]"):
        parts = [item.strip().strip('"') for item in stripped[1:-1].split(",") if item.strip()]
        return parts
    return stripped.strip('"')


def _write_toml(path: Path, payload: dict[str, object]) -> None:
    lines: list[str] = []
    table_blocks: list[tuple[str, dict[str, object]]] = []
    array_table_blocks: list[tuple[str, list[dict[str, object]]]] = []
    for key, value in payload.items():
        if isinstance(value, dict):
            table_blocks.append((key, value))
            continue
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            array_table_blocks.append((key, [dict(item) for item in value if isinstance(item, dict)]))
            continue
        lines.append(f"{key} = {_render_toml_value(value)}")
    for key, table in table_blocks:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(f"[{key}]")
        for child_key, child_value in table.items():
            lines.append(f"{child_key} = {_render_toml_value(child_value)}")
    for key, entries in array_table_blocks:
        for entry in entries:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[[{key}]]")
            for child_key, child_value in entry.items():
                lines.append(f"{child_key} = {_render_toml_value(child_value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _render_toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [f"{key} = {_render_toml_value(item)}" for key, item in value.items()]
        return "{ " + ", ".join(parts) + " }"
    return json.dumps(value)
