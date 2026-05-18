from __future__ import annotations

from collections.abc import Awaitable, Callable

from nexus.models import AgentEvent
from nexus.runtime.agent import Agent
from nexus.runtime.repl_state import ReplState
from nexus.runtime.turn_runner import ConfirmationCallback, run_agent_turn
from nexus.ui import TerminalUI


async def run_orchestrated_turn(
    state: ReplState,
    agent: Agent,
    *,
    prompt_text: str,
    ui: TerminalUI | None = None,
    approval_callback: ConfirmationCallback | None = None,
    auto_confirm: bool = False,
    turn_runner: Callable[..., Awaitable[list[AgentEvent]]] = run_agent_turn,
) -> list[AgentEvent]:
    """Run one turn through the shared turn runner.

    Advanced mode is implemented by exposing cognitive sub-agent tools to the
    supervisor model. The old automatic DAG scheduler was removed, so this
    module now only preserves the common REPL/headless call site.
    """
    return await turn_runner(
        state,
        agent,
        prompt_text=prompt_text,
        ui=ui,
        approval_callback=approval_callback,
        auto_confirm=auto_confirm,
    )
