from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from nexus.config import load_config
from nexus.memory.store import MemoryStore
from nexus.models import (
    AgentEvent,
    AgentEventType,
    ConfirmationKind,
    ConfirmationRequest,
    ConfirmationResponse,
    Message,
    RuntimeRequest,
    RuntimeResponse,
    ToolCall,
    ToolResult,
)
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
        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="done")


class _ApprovalRetryAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, messages, context, **kwargs):
        del messages, context, kwargs
        self.calls += 1
        if self.calls == 1:
            yield AgentEvent(
                kind=AgentEventType.MODEL_RESPONSE,
                payload=RuntimeResponse(
                    message=Message(
                        role="assistant",
                        content="I'll write the file.",
                        tool_calls=(
                            ToolCall(
                                call_id="call-1",
                                tool_name="write_file",
                                arguments={"path": "hello.py", "content": "print('hi')\n"},
                            ),
                        ),
                    ),
                    tool_calls=(
                        ToolCall(
                            call_id="call-1",
                            tool_name="write_file",
                            arguments={"path": "hello.py", "content": "print('hi')\n"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
            yield AgentEvent(
                kind=AgentEventType.CONFIRMATION_REQUESTED,
                payload=ConfirmationRequest(
                    kind=ConfirmationKind.APPROVAL,
                    tool_name="write_file",
                    prompt="Allow write_file?",
                    reason="write_file replaces the entire file.",
                    arguments={"path": "hello.py", "content": "print('hi')\n"},
                ),
            )
            return

        yield AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(
                message=Message(role="assistant", content="Created the file."),
            ),
        )
        yield AgentEvent(
            kind=AgentEventType.TOOL_RESULT,
            payload=ToolResult(call_id="call-1", tool_name="write_file", output="Created hello.py"),
        )
        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="done")


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


def test_apply_events_skips_unmatched_tool_call_messages(tmp_path):
    state = _build_state(tmp_path)
    pending_call = ToolCall(call_id="call-1", tool_name="write_file", arguments={"path": "hello.py"})

    state.apply_events(
        [
            AgentEvent(
                kind=AgentEventType.MODEL_RESPONSE,
                payload=RuntimeResponse(
                    message=Message(
                        role="assistant",
                        content="I'll write the file.",
                        tool_calls=(pending_call,),
                    ),
                    tool_calls=(pending_call,),
                    finish_reason="tool_calls",
                ),
            )
        ]
    )

    assert state.history == []


def test_apply_events_keeps_completed_tool_call_messages(tmp_path):
    state = _build_state(tmp_path)
    completed_call = ToolCall(call_id="call-1", tool_name="write_file", arguments={"path": "hello.py"})

    state.apply_events(
        [
            AgentEvent(
                kind=AgentEventType.MODEL_RESPONSE,
                payload=RuntimeResponse(
                    message=Message(
                        role="assistant",
                        content="I'll write the file.",
                        tool_calls=(completed_call,),
                    ),
                    tool_calls=(completed_call,),
                    finish_reason="tool_calls",
                ),
            ),
            AgentEvent(
                kind=AgentEventType.TOOL_RESULT,
                payload=ToolResult(
                    call_id="call-1",
                    tool_name="write_file",
                    output="Created hello.py",
                ),
            ),
        ]
    )

    assert state.history[0].role == "assistant"
    assert state.history[0].tool_calls[0].call_id == "call-1"
    assert state.history[1].role == "tool"
    assert state.history[1].tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_collect_turn_events_discards_preapproval_batches_on_retry(tmp_path):
    state = _build_state(tmp_path)
    state.history.append(Message(role="user", content="write hello.py"))
    agent = _ApprovalRetryAgent()

    async def _approve(_request):
        return ConfirmationResponse(approved=True)

    events = await collect_turn_events(
        state,
        agent,
        prompt_text="write hello.py",
        approval_callback=_approve,
    )

    model_responses = [event for event in events if event.kind == AgentEventType.MODEL_RESPONSE]

    assert agent.calls == 2
    assert len(model_responses) == 1
    assert model_responses[0].payload.message.content == "Created the file."
    assert not any(event.kind == AgentEventType.CONFIRMATION_REQUESTED for event in events)


