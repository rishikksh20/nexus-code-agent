from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from rich.console import Console

from nexus.cli.headless import EXIT_NEEDS_INPUT, EXIT_OK, run_headless
from nexus.config import load_config
from nexus.integrations.fake_model import FakeModelClient
from nexus.memory.store import MemoryStore
from nexus.models import (
    AgentEvent,
    AgentEventType,
    ConfirmationKind,
    ConfirmationResponse,
    Message,
    RuntimeResponse,
    ToolCall,
)
from nexus.runtime.agent import Agent
from nexus.runtime.agent_scope import subagent_tool_names, supervisor_tool_names
from nexus.runtime.clarifications import (
    ClarificationManager,
    ask_user_request_from_confirmation,
    parse_ask_user_response,
)
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import EphemeralSessionStore, new_snapshot
from nexus.runtime.turn_runner import run_agent_turn
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import AskUserTool, GetTimeTool


def _ask_call(call_id: str = "ask-1") -> ToolCall:
    return ToolCall(
        call_id=call_id,
        tool_name="ask_user",
        arguments={
            "question": "Which config scope should I use?",
            "reason": "This changes override behavior.",
            "answer_type": "choice",
            "options": [
                {"id": "global", "label": "Global"},
                {"id": "project", "label": "Project"},
            ],
            "default_option_id": "project",
        },
    )


def _state(tmp_path, registry: ToolRegistry, *, session_id: str = "ask-user") -> ReplState:
    config = load_config(tmp_path, global_root=tmp_path / "global")
    return ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot(session_id),
        session_store=EphemeralSessionStore(),
        tool_registry=registry,
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True, width=200),
    )


def test_ask_user_tool_validates_choice_and_fixed_yes_no(tool_context):
    tool = AskUserTool()

    request = tool.get_user_input_request(_ask_call().call_id, _ask_call().arguments, tool_context)
    yes_no = tool.get_user_input_request(
        "ask-yes-no",
        {"question": "Continue?", "answer_type": "yes_no"},
        tool_context,
    )

    assert [option.id for option in request.options] == ["global", "project"]
    assert request.default_option_id == "project"
    assert [option.id for option in yes_no.options] == ["yes", "no"]
    assert yes_no.default_option_id == "no"

    with pytest.raises(ValueError, match="unique"):
        tool.get_user_input_request(
            "ask-duplicate",
            {
                "question": "Pick one",
                "answer_type": "choice",
                "options": [{"id": "same", "label": "One"}, {"id": "same", "label": "Two"}],
            },
            tool_context,
        )
    with pytest.raises(ValueError, match="runtime-owned"):
        tool.get_user_input_request(
            "ask-yes-no-options",
            {
                "question": "Continue?",
                "answer_type": "yes_no",
                "options": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}],
            },
            tool_context,
        )


def test_ask_user_parser_accepts_number_id_label_and_default(tool_context):
    tool = AskUserTool()
    manager = ClarificationManager({}, turn_id="turn-1", max_questions_per_turn=3)
    confirmation = manager.create_request(
        _ask_call(),
        tool.get_user_input_request(_ask_call().call_id, _ask_call().arguments, tool_context),
    )
    assert not isinstance(confirmation, tuple)
    assert confirmation.kind is ConfirmationKind.CLARIFICATION

    assert parse_ask_user_response(confirmation, "1")[0].selected_option_id == "global"
    assert parse_ask_user_response(confirmation, "project")[0].selected_option_id == "project"
    assert parse_ask_user_response(confirmation, "Global")[0].selected_option_id == "global"
    assert parse_ask_user_response(confirmation, "")[0].selected_option_id == "project"
    assert parse_ask_user_response(confirmation, "unknown")[0] is None


def test_ask_user_is_supervisor_only_even_for_all_subagent_scope():
    registry = ToolRegistry()
    registry.register(AskUserTool())
    registry.register(GetTimeTool())
    config = SimpleNamespace(
        agent_mode="advanced",
        agent_allowed_tools=[],
        agent_allowed_mcp_servers=[],
        subagent_profiles=[{"name": "execution", "allowed_tools": "all"}],
    )

    assert supervisor_tool_names(config, registry) == {"ask_user"}
    assert subagent_tool_names(
        config,
        registry,
        "execution",
        base_allowed_tools=None,
    ) == {"get_time"}


def test_ask_user_manager_stops_after_three_questions(tool_context):
    metadata = {}
    tool = AskUserTool()
    manager = ClarificationManager(metadata, turn_id="turn-1", max_questions_per_turn=3)
    request = tool.get_user_input_request(_ask_call().call_id, _ask_call().arguments, tool_context)

    results = [manager.create_request(_ask_call(f"ask-{index}"), request) for index in range(1, 5)]

    assert all(getattr(result, "kind", None) is ConfirmationKind.CLARIFICATION for result in results[:3])
    assert results[3].is_error is True
    assert results[3].metadata["ask_user_limit_exceeded"] is True


@pytest.mark.asyncio
async def test_ask_user_answer_commits_exact_tool_result_and_discards_later_siblings(tmp_path):
    registry = ToolRegistry()
    registry.register(AskUserTool())
    registry.register(GetTimeTool())
    ask_call = _ask_call()
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="I need one decision."),
                tool_calls=(
                    ToolCall(call_id="time-before", tool_name="get_time", arguments={}),
                    ask_call,
                    ToolCall(call_id="time-after", tool_name="get_time", arguments={}),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content="Using project config.")),
        ]
    )
    state = _state(tmp_path, registry)
    state.history.append(Message(role="user", content="Configure providers"))

    async def answer(_request):
        return ConfirmationResponse(clarification="Project", selected_option_id="project")

    events = await run_agent_turn(
        state,
        Agent(model_client=model, tool_registry=registry),
        prompt_text="Configure providers",
        approval_callback=answer,
    )
    state.apply_events(events)

    tool_messages = [message for message in state.history if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == ["time-before", "ask-1"]
    ask_message = tool_messages[-1]
    assert json.loads(ask_message.content)["selected_option_id"] == "project"
    assistant_tool_calls = [
        call.call_id
        for message in state.history
        if message.role == "assistant"
        for call in message.tool_calls
    ]
    assert assistant_tool_calls == ["time-before", "ask-1"]
    assert state.history[-1].content == "Using project config."


@pytest.mark.asyncio
async def test_headless_non_tty_returns_structured_needs_input_without_selecting_default(tmp_path, monkeypatch):
    registry = ToolRegistry()
    registry.register(AskUserTool())
    state = _state(tmp_path, registry, session_id="headless-ask")
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Need config scope."),
                    tool_calls=(_ask_call(),),
                    finish_reason="tool_calls",
                )
            ]
        ),
        tool_registry=registry,
    )
    monkeypatch.setattr("nexus.cli.headless._can_prompt_for_confirmation", lambda: False)

    result = await run_headless(
        state,
        agent,
        "Configure providers",
        auto_confirm=True,
        output_path=None,
        output_format="json",
        quiet=True,
    )

    assert result.exit_code == EXIT_NEEDS_INPUT
    assert result.needs_input["status"] == "needs_input"
    assert result.needs_input["request"]["default_option_id"] == "project"
    assert not [message for message in result.history if message.role == "tool"]


@pytest.mark.asyncio
async def test_headless_tty_accepts_advertised_default_and_continues(tmp_path, monkeypatch):
    registry = ToolRegistry()
    registry.register(AskUserTool())
    state = _state(tmp_path, registry, session_id="headless-ask-tty")
    agent = Agent(
        model_client=FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Need config scope."),
                    tool_calls=(_ask_call(),),
                    finish_reason="tool_calls",
                ),
                RuntimeResponse(message=Message(role="assistant", content="Done.")),
            ]
        ),
        tool_registry=registry,
    )
    monkeypatch.setattr("nexus.cli.headless._can_prompt_for_confirmation", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    result = await run_headless(
        state,
        agent,
        "Configure providers",
        auto_confirm=False,
        output_path=None,
        output_format="text",
        quiet=True,
    )

    assert result.exit_code == EXIT_OK
    assert result.response == "Done."
    tool_message = next(message for message in result.history if message.role == "tool")
    assert json.loads(tool_message.content)["selected_option_id"] == "project"
