from __future__ import annotations

import json
import shlex
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
from nexus.skills import get_skill_roots, load_skill_registry
from nexus.tools.subagents import (
    load_subagent_definitions,
    register_skill_subagent_tools,
    register_subagent_tools,
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
    router.register(SlashCommand("mode", "Show or switch execution mode.", handle_mode))
    router.register(SlashCommand("provider", "Show or update model provider and session parameters.", handle_provider))
    router.register(SlashCommand("skills", "Inspect and activate skills.", handle_skills))
    router.register(SlashCommand("config", "Show merged, local, or global config.", handle_config))
    router.register(SlashCommand("session", "Inspect or create sessions.", handle_session))
    router.register(SlashCommand("tools", "List registered tools.", handle_tools))
    router.register(SlashCommand("memory", "Inspect workspace memory entries.", handle_memory))
    router.register(SlashCommand("context", "Show system prompt or context usage stats.", handle_context))
    router.register(SlashCommand("history", "Show recent message history.", handle_history))
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
        ("/mcp [status|tools|refresh [server]]", "Inspect MCP server connections and registered tools."),
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
        ("/quit", "Save and exit."),
    ):
        table.add_row(name, description)
    state.console.print(table)


async def handle_mcp(state: ReplState, args: list[str]) -> None:
    subcommand = args[0].lower() if args else "status"
    if subcommand == "help":
        _print_mcp_help(state)
        return

    if subcommand == "reload":
        reports = await _reload_mcp_servers(state)
        if not reports:
            state.console.print("No MCP servers configured. Add mcp_servers to .nexus/config.toml, then run /mcp reload.")
            return
        _print_mcp_refresh_reports(state, reports, title="MCP Reload")
        _print_mcp_status(state)
        return

    if not state.mcp_servers:
        state.console.print("No MCP servers loaded. Add mcp_servers to .nexus/config.toml, then run /mcp reload.")
        state.console.print("Usage: /mcp [status|tools|refresh|reload|help]")
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
        _print_mcp_refresh_reports(state, reports)
        _print_mcp_status(state)
        return
    state.console.print("Usage: /mcp [status|tools|refresh [server]|reload|help]")


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
    elif args[0].lower() == "reinit":
        scope = args[1].lower() if len(args) > 1 else "local"
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
            state, "skills", "Inspect and activate session skills. Skills named subagent-* register specialist cognitive tools in advanced mode.",
            (
                ("(no args) / list",  "List all loaded skills with name, type, description, and active status.", "/skills"),
                ("show <name>",      "Print the full content of a skill file.",                          "/skills show nexus-agent"),
                ("add <name>",       "Activate a skill for this session.",                               "/skills add nexus-agent"),
                ("remove <name>",    "Deactivate a skill.",                                             "/skills remove nexus-agent"),
                ("reload",           "Rescan builtin, user, and workspace skill directories.",          "/skills reload"),
                ("help",             "Show this help.",                                                 "/skills help"),
            ),
        )
        return
    if subcommand in {"list", ""}:
        skills = state.skill_registry.all()
        if not skills:
            state.console.print("No skills loaded.")
            return
        table = Table(title="Skills")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Description")
        table.add_column("Active")
        for skill in skills:
            table.add_row(
                skill.name,
                "subagent" if _is_subagent_skill_name(skill.name) else "skill",
                skill.description,
                "yes" if skill.name in state.active_skills else "no",
            )
        state.console.print(table)
        return
    if subcommand == "show" and len(args) > 1:
        skill = state.skill_registry.get(args[1])
        state.console.print(skill.content if skill is not None else f"Skill not found: {args[1]}")
        return
    if subcommand == "add" and len(args) > 1:
        skill = state.skill_registry.get(args[1])
        if skill is None:
            state.console.print(f"Skill not found: {args[1]}")
            return
        if skill.name not in state.active_skills:
            state.active_skills.append(skill.name)
        state.console.print(f"Activated skill: {skill.name}")
        return
    if subcommand == "remove" and len(args) > 1:
        state.active_skills = [name for name in state.active_skills if name != args[1]]
        state.console.print(f"Removed skill: {args[1]}")
        return
    if subcommand == "reload":
        state.skill_registry = load_skill_registry(*get_skill_roots(state.config))
        state.active_skills = [name for name in state.active_skills if state.skill_registry.get(name) is not None]
        preserved_records = [record for record in state.tool_registry.records() if record.source != "agent-skill"]
        state.tool_registry.clear()
        for record in preserved_records:
            state.tool_registry.register(record.tool, source=record.source, origin=record.origin)
        register_skill_subagent_tools(
            state.tool_registry,
            state.config,
            state.skill_registry,
        )
        state.console.print("Reloaded skills.")
        return
    state.console.print("Usage: /skills [list|show <name>|add <name>|remove <name>|reload]")


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
    table.add_column("Source")
    table.add_column("Origin")
    table.add_column("Mutating")
    table.add_column("Description")
    for record in state.tool_registry.records():
        tool = record.tool
        table.add_row(
            record.name,
            record.source,
            record.origin or "-",
            "yes" if tool.is_mutating else "no",
            tool.description,
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
        estimator = TokenEstimator()
        history_tokens = sum(estimator.estimate(m.content) for m in state.history)
        system_tokens = estimator.estimate(state.current_system_prompt or "")
        total_estimated = history_tokens + system_tokens
        ctx_limit = get_model_context_limit(state.config.model_name)
        soft = state.config.compaction_soft_limit
        hard = state.config.compaction_hard_limit
        pct = round(total_estimated / ctx_limit * 100, 1) if ctx_limit else 0.0

        table = Table(title="Context Usage")
        table.add_column("Field")
        table.add_column("Value", justify="right")
        table.add_row("Provider", state.config.provider)
        table.add_row("Model", state.config.model_name)
        table.add_row("Context window", f"{ctx_limit:,} tokens")
        table.add_row("System prompt (est.)", f"{system_tokens:,} tokens")
        table.add_row("History (est.)", f"{history_tokens:,} tokens")
        table.add_row("Total used (est.)", f"{total_estimated:,} tokens  ({pct}%)")
        table.add_row("Compaction soft limit", f"{soft:,} tokens  ({round(soft/ctx_limit*100,1)}%)")
        table.add_row("Compaction hard limit", f"{hard:,} tokens  ({round(hard/ctx_limit*100,1)}%)")
        state.console.print(table)
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
        state, "mcp", "Inspect MCP server status and tools.",
        (
            ("status",           "Show MCP server status, transport, counts, and last error.", "/mcp status"),
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
                state_text = "filtered"
            description = spec.description.replace("\n", " ")[:80] or "-"
            table.add_row(server.server.name, display_name, spec.name, state_text, description)
    state.console.print(table)


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
    for tool_name in report.allowed_tool_additions:
        state.console.print(f"  [bold]+[/bold] allowed_tools: {tool_name}")


def _reload_config(state: ReplState) -> None:
    state.config = load_config(
        state.config.workspace_root,
        global_root=state.config.global_root,
        local_config_path=state.config.local_config_file,
        global_config_path=state.config.global_config_file,
    )


def _reload_tools(state: ReplState) -> int:
    from nexus.tools.registry import register_core_tools, tool_enabled

    cfg = state.config
    rebuilt_sources = {"core", "agent", "agent-skill"}
    preserved_records = [
        record
        for record in state.tool_registry.records()
        if record.source not in rebuilt_sources
    ]

    state.tool_registry.clear()
    register_core_tools(state.tool_registry, cfg)

    for record in preserved_records:
        if tool_enabled(cfg, record.name):
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
    for key, value in payload.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        elif isinstance(value, list):
            rendered = "[" + ", ".join(json.dumps(item) for item in value) + "]"
        else:
            rendered = json.dumps(value)
        lines.append(f"{key} = {rendered}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
