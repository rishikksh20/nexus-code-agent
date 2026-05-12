from __future__ import annotations

from pathlib import Path
from typing import Any

import click

_CONTEXT_SETTINGS = dict(help_option_names=["-h", "--help"], max_content_width=100)


@click.group(
    invoke_without_command=True,
    context_settings=_CONTEXT_SETTINGS,
    epilog="Run 'nexus COMMAND --help' for help on a specific subcommand.",
)
@click.pass_context
@click.option("--prompt", "-p", default=None, metavar="TEXT", help="Run in headless mode with this prompt.")
@click.option("--prompt-file", "-f", "prompt_file", type=click.Path(path_type=Path), default=None, metavar="FILE", help="Read the headless prompt from a file.")
@click.option("--stdin", "use_stdin", is_flag=True, help="Read the headless prompt from stdin.")
@click.option("--session", "-s", default=None, metavar="NAME", help="Resume or create a named session.")
@click.option("--resume-last", "resume_last", is_flag=True, help="Resume the latest saved session for this workspace.")
@click.option("--no-session", "no_session", is_flag=True, help="Skip session persistence.")
@click.option("--model", "-m", default=None, metavar="NAME", help="Override the model name from config.")
@click.option("--provider", default=None, metavar="NAME", help="Override the provider from config.")
@click.option("--mode", type=click.Choice(["plan", "default", "auto"]), default=None, help="Override the execution mode.")
@click.option("--config", "-c", "config_file", type=click.Path(path_type=Path), default=None, metavar="FILE", help="Path to a local config TOML file.")
@click.option("--global-config", "global_config", type=click.Path(path_type=Path), default=None, metavar="FILE", help="Path to a global config TOML file.")
@click.option("--max-tokens", "max_tokens", type=int, default=None, metavar="N", help="Override the compaction hard limit (tokens).")
@click.option("--max-turns", "max_turns", type=int, default=None, metavar="N", help="Override the max agent loop iterations.")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None, metavar="FILE", help="Write the final response to a file.")
@click.option(
    "--output-format", "output_format",
    type=click.Choice(["text", "json", "jsonl"]),
    default="text", show_default=True,
    help="Format for the final response.",
)
@click.option("--stream", "stream", is_flag=True, help="Enable streamed output (overrides config).")
@click.option("--no-stream", "no_stream", is_flag=True, help="Disable streamed output.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress tool call and progress output.")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug-level logging.")
@click.option("--auto-confirm", "auto_confirm", is_flag=True, help="Automatically confirm all mutating tool calls.")
@click.option("--deny-mutating", "deny_mutating", is_flag=True, help="Deny all mutating tools (implies plan mode).")
@click.option("--allow-hidden-paths", "allow_hidden_paths", is_flag=True, help="Allow the agent to read hidden/private paths other than .nexus for this run.")
@click.option("--allowed-tools", "allowed_tools", default=None, metavar="LIST", help="Comma-separated allowlist of tool names.")
@click.option("--denied-tools", "denied_tools", default=None, metavar="LIST", help="Comma-separated denylist of tool names.")
@click.option("--skill", "skills", multiple=True, metavar="NAME", help="Activate a skill for this run (repeatable).")
@click.option("--no-plugins", "no_plugins", is_flag=True, help="Skip plugin loading for this run.")
@click.option("--no-skills", "no_skills", is_flag=True, help="Skip skill loading for this run.")
def cli(
    ctx: click.Context,
    prompt: str | None,
    prompt_file: Path | None,
    use_stdin: bool,
    session: str | None,
    resume_last: bool,
    no_session: bool,
    model: str | None,
    provider: str | None,
    mode: str | None,
    config_file: Path | None,
    global_config: Path | None,
    max_tokens: int | None,
    max_turns: int | None,
    output: Path | None,
    output_format: str,
    stream: bool,
    no_stream: bool,
    quiet: bool,
    verbose: bool,
    auto_confirm: bool,
    deny_mutating: bool,
    allow_hidden_paths: bool,
    allowed_tools: str | None,
    denied_tools: str | None,
    skills: tuple[str, ...],
    no_plugins: bool,
    no_skills: bool,
) -> int | None:
    """Nexus — an AI coding agent with a REPL, headless runner, and tool ecosystem."""
    if ctx.invoked_subcommand is not None:
        return None

    # Enforce mutual exclusion of prompt-source flags.
    if sum([prompt is not None, prompt_file is not None, use_stdin]) > 1:
        raise click.UsageError("--prompt, --prompt-file, and --stdin are mutually exclusive.")

    if stream and no_stream:
        raise click.UsageError("--stream and --no-stream are mutually exclusive.")

    # Lazy import avoids a circular dependency (nexus.app imports nexus.cli.args at module level).
    from nexus.app import _dispatch_runtime  # noqa: PLC0415

    return _dispatch_runtime(ctx.params)


@cli.command("version")
def version_cmd() -> None:
    """Print the Nexus version and exit."""
    from nexus.app import _dispatch_version  # noqa: PLC0415

    _dispatch_version()


@cli.command("doctor")
@click.option(
    "--output-format", "output_format",
    type=click.Choice(["text", "json", "jsonl"]),
    default="text", show_default=True,
    help="Format for the diagnostic report.",
)
def doctor_cmd(output_format: str) -> int:
    """Run production-readiness checks and display a report."""
    from nexus.app import _dispatch_doctor  # noqa: PLC0415

    return _dispatch_doctor(output_format)


@cli.command("init")
@click.option("--force", is_flag=True, help="Overwrite existing config files with fresh defaults.")
def init_cmd(force: bool) -> None:
    """Create local and global Nexus config files in the current workspace."""
    from nexus.app import _dispatch_init  # noqa: PLC0415

    _dispatch_init(force)


@cli.command("config")
@click.argument("scope", default="merged", type=click.Choice(["global", "local", "merged"]))
def config_cmd(scope: str) -> None:
    """Inspect the Nexus configuration.

    \b
    SCOPE selects which configuration layer to display:
      global  — the user-level ~/.nexus/config.toml
      local   — the workspace-level .nexus/config.toml
      merged  — the effective merged config (default)
    """
    from nexus.app import _dispatch_config  # noqa: PLC0415

    _dispatch_config(scope)


def args_to_config_overrides(
    *,
    model: str | None = None,
    provider: str | None = None,
    mode: str | None = None,
    max_tokens: int | None = None,
    max_turns: int | None = None,
    stream: bool = False,
    no_stream: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    allow_hidden_paths: bool = False,
    allowed_tools: str | None = None,
    denied_tools: str | None = None,
    **_ignored: Any,
) -> dict[str, object]:
    """Build a config-override mapping from click option values."""
    overrides: dict[str, object] = {}
    if model:
        overrides["model_name"] = model
    if provider:
        overrides["provider"] = provider
    if mode:
        overrides["default_mode"] = mode
    if max_tokens:
        overrides["compaction_hard_limit"] = max_tokens
    if max_turns:
        overrides["max_loop_iterations"] = max_turns
    if stream:
        overrides["stream_output"] = True
    elif no_stream:
        overrides["stream_output"] = False
    if quiet:
        overrides["show_tool_calls"] = False
    if verbose:
        overrides["log_level"] = "DEBUG"
    if allow_hidden_paths:
        overrides["allow_hidden_paths"] = True
    if allowed_tools:
        overrides["allowed_tools"] = [t.strip() for t in allowed_tools.split(",") if t.strip()]
    if denied_tools:
        overrides["denied_tools"] = [t.strip() for t in denied_tools.split(",") if t.strip()]
    return overrides
