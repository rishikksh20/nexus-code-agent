import asyncio
import sys
from pathlib import Path

import click

from core.agent.session import Session
from core.agent.persistence import PersistenceManager, SessionSnapshot

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from core.config.config import Config, ApprovalPolicy
from core.config.loader import load_config
from core.agent.agent import Agent
from core.agent.events import AgentEventType
from core.ui.tui import get_console, TUI

console = get_console()


def _load_env_file(cwd: Path | None) -> None:
    if load_dotenv is None:
        return

    base_dir = cwd or Path.cwd()
    load_dotenv(dotenv_path=base_dir / ".env")

class CLI:
    def __init__(self, config: Config) -> None:
        self.agent: Agent|None = None
        self.tui = TUI(config, console)
        self.config = config

    async def run_single(self, message: str)-> str|None:
        async with Agent(self.config, confirmation_callback=self.tui.handle_confirmation) as agent:
            self.agent = agent
            return await self._process_message(message)

    async def run_interactive(self)-> None:

        self.tui.print_welcome(
                "Welcome to the Agent CLI! Type your message and press Enter to interact with the agent. Type /exit to quit.",
            lines=[
                f'model : {self.config.model_name}',
                f"cwd: {self.config.cwd}",
                "commands: /help /config /approval /model /exit",
            ],
        )
        async with Agent(self.config, confirmation_callback=self.tui.handle_confirmation) as agent:
            self.agent = agent

            while True:
                try:
                    user_input = console.input("\n[user]>[/user]").strip()
                    if not user_input:
                        continue

                    if user_input.startswith("/"):
                        should_continue = await self._handle_command(user_input)
                        if not should_continue:
                            break
                        continue

                    await self._process_message(user_input)
                except KeyboardInterrupt:
                    console.print("\n[dim]Use /exit to quit[/dim]")
                except EOFError:
                    break

        console.print("\n[dim]Exiting interactive mode...[/dim]")


    async def _process_message(self, message: str) -> str|None:
        if not self.agent:
            return None

        assitant_streaming = False
        final_response: str | None = None

        async  for event in self.agent.run(message):

            if event.type == AgentEventType.TEXT_DELTA:
                content = event.data.get("message", "")
                if not assitant_streaming:
                    self.tui.begin_assistant()
                    assitant_streaming = True
                self.tui.stream_assistant_delta(content)
            elif event.type == AgentEventType.TEXT_COMPLETE:
                final_response = event.data.get("message", "")
                if assitant_streaming:
                    assitant_streaming = False
                    self.tui.end_assistant()
            elif event.type == AgentEventType.AGENT_ERROR:
                error = event.data.get("error", "Unknown error!")
                if assitant_streaming:
                    self.tui.end_assistant()
                    assitant_streaming = False
                self.tui.print_error(error)
            elif event.type == AgentEventType.TOOL_CALL_START:
                tool_name = event.data.get("name", "")

                tool = self.agent.session.tool_registry.get(tool_name)
                if not tool:
                    tool_kind = None
                else:
                    tool_kind = tool.kind.value

                self.tui.tool_call_start(
                    event.data.get("call_id", ""),
                    tool_name,
                    tool_kind if isinstance(tool_kind, str) else None,
                    event.data.get("arguments", {}),
                )
            elif event.type == AgentEventType.TOOL_CALL_COMPLETE:
                tool_name = event.data.get("name", "")

                tool = self.agent.session.tool_registry.get(tool_name)
                if not tool:
                    tool_kind = None
                else:
                    tool_kind = tool.kind.value
                self.tui.tool_call_complete(
                    event.data.get("call_id", ""),
                    tool_name,
                    tool_kind if isinstance(tool_kind, str) else None,
                    event.data.get("success", False),
                    event.data.get("output", ""),
                    event.data.get("error"),
                    event.data.get("metadata"),
                    event.data.get("diff"),
                    event.data.get("truncated", False),
                    event.data.get("exit_code"),

                )




        return final_response

    async def _handle_command(self, command: str) -> bool:
        cmd = command.lower().strip()
        parts = cmd.split(maxsplit=1)
        cmd_name = parts[0]
        cmd_args = parts[1] if len(parts) > 1 else ""
        if cmd_name == "/exit" or cmd_name == "/quit":
            return False
        elif command == "/help":
            self.tui.show_help()
        elif command == "/clear":
            self.agent.session.context_manager.clear()
            self.agent.session.loop_detector.clear()
            console.print("[success]Conversation cleared [/success]")
        elif command == "/config":
            console.print("\n[bold]Current Configuration[/bold]")
            console.print(f"  Model: {self.config.model_name}")
            console.print(f"  Temperature: {self.config.temperature}")
            console.print(f"  Approval: {self.config.approval.value}")
            console.print(f"  Working Dir: {self.config.cwd}")
            console.print(f"  Max Turns: {self.config.max_turns}")
            console.print(f"  Hooks Enabled: {self.config.hooks_enabled}")
        elif cmd_name == "/model":
            if cmd_args:
                self.config.model_name = cmd_args
                console.print(f"[success]Model changed to: {cmd_args} [/success]")
            else:
                console.print(f"Current model: {self.config.model_name}")
        elif cmd_name == "/approval":
            if cmd_args:
                try:
                    approval = ApprovalPolicy(cmd_args)
                    self.config.approval = approval
                    console.print(
                        f"[success]Approval policy changed to: {cmd_args} [/success]"
                    )
                except:
                    console.print(
                        f"[error]Incorrect approval policy: {cmd_args} [/error]"
                    )
                    console.print(
                        f"Valid options: {', '.join(p for p in ApprovalPolicy)}"
                    )
            else:
                console.print(f"Current approval policy: {self.config.approval.value}")
        elif cmd_name == "/stats":
            stats = self.agent.session.get_stats()
            console.print("\n[bold]Session Statistics [/bold]")
            for key, value in stats.items():
                console.print(f"   {key}: {value}")
        elif cmd_name == "/tools":
            tools = self.agent.session.tool_registry.get_tools()
            console.print(f"\n[bold]Available tools ({len(tools)}) [/bold]")
            for tool in tools:
                console.print(f"  • {tool.name}")
        elif cmd_name == "/mcp":
            mcp_servers = self.agent.session.mcp_manager.get_all_servers()
            console.print(f"\n[bold]MCP Servers ({len(mcp_servers)}) [/bold]")
            for server in mcp_servers:
                status = server["status"]
                status_color = "green" if status == "connected" else "red"
                console.print(
                    f"  • {server['name']}: [{status_color}]{status}[/{status_color}] ({server['tools']} tools)"
                )
        elif cmd_name == "/save":
            persistence_manager = PersistenceManager()
            session_snapshot = SessionSnapshot(
                session_id=self.agent.session.session_id,
                created_at=self.agent.session.created_at,
                updated_at=self.agent.session.updated_at,
                turn_count=self.agent.session.turn_count,
                messages=self.agent.session.context_manager.get_messages(),
                total_usage=self.agent.session.context_manager.total_usage,
            )
            persistence_manager.save_session(session_snapshot)
            console.print(
                f"[success]Session saved: {self.agent.session.session_id}[/success]"
            )
        elif cmd_name == "/sessions":
            persistence_manager = PersistenceManager()
            sessions = persistence_manager.list_sessions()
            console.print("\n[bold]Saved Sessions[/bold]")
            for s in sessions:
                console.print(
                    f"  • {s['session_id']} (turns: {s['turn_count']}, updated: {s['updated_at']})"
                )
        elif cmd_name == "/resume":
            if not cmd_args:
                console.print(f"[error]Usage: /resume <session_id> [/error]")
            else:
                persistence_manager = PersistenceManager()
                snapshot = persistence_manager.load_session(cmd_args)
                if not snapshot:
                    console.print(f"[error]Session does not exist [/error]")
                else:
                    session = Session(
                        config=self.config,
                    )
                    await session.initialize()
                    session.session_id = snapshot.session_id
                    session.created_at = snapshot.created_at
                    session.updated_at = snapshot.updated_at
                    session.turn_count = snapshot.turn_count
                    session.context_manager.total_usage = snapshot.total_usage

                    for msg in snapshot.messages:
                        if msg.get("role") == "system":
                            continue
                        elif msg["role"] == "user":
                            session.context_manager.add_user_message(
                                msg.get("content", "")
                            )
                        elif msg["role"] == "assistant":
                            session.context_manager.add_assistant_message(
                                msg.get("content", ""), msg.get("tool_calls")
                            )
                        elif msg["role"] == "tool":
                            session.context_manager.add_tool_result(
                                msg.get("tool_call_id", ""), msg.get("content", "")
                            )

                    await self.agent.session.client.close()
                    await self.agent.session.mcp_manager.shutdown()

                    self.agent.session = session
                    console.print(
                        f"[success]Resumed session: {session.session_id}[/success]"
                    )
        elif cmd_name == "/checkpoint":
            persistence_manager = PersistenceManager()
            session_snapshot = SessionSnapshot(
                session_id=self.agent.session.session_id,
                created_at=self.agent.session.created_at,
                updated_at=self.agent.session.updated_at,
                turn_count=self.agent.session.turn_count,
                messages=self.agent.session.context_manager.get_messages(),
                total_usage=self.agent.session.context_manager.total_usage,
            )
            checkpoint_id = persistence_manager.save_checkpoint(session_snapshot)
            console.print(f"[success]Checkpoint created: {checkpoint_id}[/success]")
        elif cmd_name == "/restore":
            if not cmd_args:
                console.print(f"[error]Usage: /restire <checkpoint_id> [/error]")
            else:
                persistence_manager = PersistenceManager()
                checkpoint_id = cmd_args.strip()
                snapshot = persistence_manager.load_checkpoint(checkpoint_id)
                if not snapshot:
                    console.print(f"[error]Checkpoint does not exist [/error]")
                else:
                    session = Session(
                        config=self.config,
                    )
                    await session.initialize()
                    session.session_id = snapshot.session_id
                    session.created_at = snapshot.created_at
                    session.updated_at = snapshot.updated_at
                    session.turn_count = snapshot.turn_count
                    session.context_manager.total_usage = snapshot.total_usage

                    for msg in snapshot.messages:
                        if msg.get("role") == "system":
                            continue
                        elif msg["role"] == "user":
                            session.context_manager.add_user_message(
                                msg.get("content", "")
                            )
                        elif msg["role"] == "assistant":
                            session.context_manager.add_assistant_message(
                                msg.get("content", ""), msg.get("tool_calls")
                            )
                        elif msg["role"] == "tool":
                            session.context_manager.add_tool_result(
                                msg.get("tool_call_id", ""), msg.get("content", "")
                            )

                    await self.agent.session.client.close()
                    await self.agent.session.mcp_manager.shutdown()

                    self.agent.session = session
                    console.print(
                        f"[success]Resumed session: {session.session_id}, checkpoint: {checkpoint_id}[/success]"
                   )
        else:
            console.print(f"[error]Unknown command: {cmd_name}[/error]")

        return True


@click.command()
@click.argument("prompt", required=False)
@click.option(
    '--cwd',
    '-c',
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    help="Path to current working directory",
)
def main(prompt: str|None, cwd: Path|None , ) -> None:

    _load_env_file(cwd)

    try:
        config = load_config(cwd=cwd)
    except Exception as e:
        console.print(f"[error]Config error: {e}[\\error]")

    errors = config.validate()
    if errors:
        for error in errors:
            console.print(f"[error]Config File: {error}[\\error]")
        sys.exit(1)
    cli = CLI(config)
    if prompt:
        result = asyncio.run(cli.run_single(prompt))
        if result is None:
            sys.exit(1)
    else:
        asyncio.run(cli.run_interactive())
    print("Done")


if __name__ == "__main__":
    main()