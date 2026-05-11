from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from nexus.config import load_config
from nexus.memory.store import MemoryStore
from nexus.models import AgentEvent, Message, RuntimeRequest, RuntimeResponse, ToolCall
from nexus.runtime.agent import Agent
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl import collect_turn_events
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import EphemeralSessionStore, new_snapshot
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool


class _CaptureModelClient:
    def __init__(self) -> None:
        self.requests: list[RuntimeRequest] = []

    async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
        self.requests.append(request)
        return RuntimeResponse(message=Message(role="assistant", content="done"))

    async def chat_completion(self, request: RuntimeRequest, *, stream: bool = True):
        response = await self.complete(request)
        from nexus.models import StreamEvent, StreamEventType, TextDelta
        if response.message.content:
            yield StreamEvent(
                type=StreamEventType.TEXT_DELTA,
                text_delta=TextDelta(content=response.message.content),
            )
        for tc in response.tool_calls:
            yield StreamEvent(type=StreamEventType.TOOL_CALL_COMPLETE, tool_call=tc)
        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=response.finish_reason,
            usage=response.usage,
        )


class _RecordingAgent:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.kwargs: dict[str, object] = {}

    async def run(self, messages, context, **kwargs):
        del context
        self.messages = list(messages)
        self.kwargs = kwargs
        yield AgentEvent(kind="turn_completed", payload="done")


def _build_state(tmp_path, **overrides) -> ReplState:
    config = load_config(tmp_path, global_root=tmp_path / "global", cli_overrides=overrides)
    session = new_snapshot(session_id="session-1")
    return ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=session,
        session_store=EphemeralSessionStore(),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=Console(file=StringIO(), force_terminal=False, color_system=None),
        history=[],
    )


@pytest.mark.asyncio
async def test_collect_turn_events_forwards_request_settings_from_config(tmp_path):
    state = _build_state(tmp_path, temperature=0.7, max_output_tokens=321)
    state.history.append(Message(role="user", content="hello"))
    model = _CaptureModelClient()
    agent = Agent(model_client=model, tool_registry=state.tool_registry)

    events = await collect_turn_events(state, agent, prompt_text="hello")

    # AGENT_STOP is now the terminal event; turn_completed precedes it
    assert any(event.kind == "turn_completed" for event in events)
    assert model.requests
    assert model.requests[0].temperature == 0.7
    assert model.requests[0].max_output_tokens == 321


@pytest.mark.asyncio
async def test_collect_turn_events_honors_auto_confirm_read_only_flag(tmp_path):
    state = _build_state(tmp_path, auto_confirm_read_only=False)
    state.tool_registry.register(GetTimeTool())
    state.history.append(Message(role="user", content="what time is it?"))
    model = _CaptureModelClient()
    agent = Agent(
        model_client=model,
        tool_registry=state.tool_registry,
    )
    model.complete = lambda request: _read_only_tool_response(request)  # type: ignore[method-assign]

    events = await collect_turn_events(state, agent, prompt_text="what time is it?")

    confirmation = next(event for event in events if event.kind == "confirmation_requested")
    assert confirmation.payload.tool_name == "get_time"


async def _read_only_tool_response(request: RuntimeRequest) -> RuntimeResponse:
    del request
    return RuntimeResponse(
        message=Message(role="assistant", content="Checking time."),
        tool_calls=(ToolCall(call_id="call-1", tool_name="get_time", arguments={}),),
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_collect_turn_events_does_not_apply_second_compaction_pass(tmp_path, monkeypatch):
    state = _build_state(
        tmp_path,
        compaction_soft_limit=1,
        compaction_hard_limit=1,
        compaction_keep_recent=1,
    )
    state.history.extend(
        [
            Message(role="user", content="first"),
            Message(role="assistant", content="second"),
        ]
    )
    expected_messages = [
        Message(role="assistant", content="compacted assistant"),
        Message(role="tool", content="compacted tool", name="get_time"),
    ]

    monkeypatch.setattr("nexus.runtime.repl.ContextCompactor.should_compact", lambda self, messages: True)
    monkeypatch.setattr(
        "nexus.runtime.repl.ContextCompactor.compact",
        lambda self, messages, carry_over, keep_recent: (list(expected_messages), carry_over),
    )
    agent = _RecordingAgent()

    events = await collect_turn_events(state, agent, prompt_text="hello")

    assert events[-1].kind == "turn_completed"
    assert agent.messages == expected_messages

