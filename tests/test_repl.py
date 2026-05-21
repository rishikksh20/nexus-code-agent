from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from nexus.config import load_config
from nexus.integrations.fake_model import FakeModelClient
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
from nexus.runtime.repl_state import ReplState
from nexus.runtime.sessions import EphemeralSessionStore, new_snapshot
from nexus.runtime.turn_runner import collect_turn_events, prompt_for_confirmation
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, MemoryTool, WriteFileTool


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


class _CountingFakeModelClient(FakeModelClient):
    def __init__(self, scripted):
        super().__init__(scripted=scripted)
        self.calls = 0

    async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
        self.calls += 1
        return await super().complete(request)


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
        del messages, context
        resume_tool_calls = kwargs.get("resume_tool_calls") or ()
        if resume_tool_calls:
            for tool_call in resume_tool_calls:
                yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                yield AgentEvent(
                    kind=AgentEventType.TOOL_RESULT,
                    payload=ToolResult(
                        call_id=tool_call.call_id,
                        tool_name=tool_call.tool_name,
                        output=f"Created {tool_call.arguments.get('path', 'file')}",
                    ),
                )
            return
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


class _SequentialApprovalAgent:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[Message]] = []

    async def run(self, messages, context, **kwargs):
        del context
        resume_tool_calls = kwargs.get("resume_tool_calls") or ()
        if resume_tool_calls:
            self.seen_messages.append(list(messages))
            for tool_call in resume_tool_calls:
                yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                yield AgentEvent(
                    kind=AgentEventType.TOOL_RESULT,
                    payload=ToolResult(
                        call_id=tool_call.call_id,
                        tool_name=tool_call.tool_name,
                        output=f"Created {tool_call.arguments.get('path', 'file')}",
                    ),
                )
            return
        self.calls += 1
        self.seen_messages.append(list(messages))

        calc_call = ToolCall(
            call_id="call-calc",
            tool_name="write_file",
            arguments={"path": "calculator.py", "content": "print('calc')\n"},
        )
        logging_call = ToolCall(
            call_id="call-logging",
            tool_name="write_file",
            arguments={"path": "logging_calculator.py", "content": "print('logging')\n"},
        )

        if self.calls == 1:
            yield AgentEvent(
                kind=AgentEventType.MODEL_RESPONSE,
                payload=RuntimeResponse(
                    message=Message(role="assistant", content="Creating calculator.", tool_calls=(calc_call,)),
                    tool_calls=(calc_call,),
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
                    arguments=calc_call.arguments,
                ),
            )
            return

        if self.calls == 2:
            assert any(message.role == "tool" and message.tool_call_id == "call-calc" for message in messages)
            yield AgentEvent(
                kind=AgentEventType.MODEL_RESPONSE,
                payload=RuntimeResponse(
                    message=Message(
                        role="assistant",
                        content="Creating logging file.",
                        tool_calls=(logging_call,),
                    ),
                    tool_calls=(logging_call,),
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
                    arguments=logging_call.arguments,
                ),
            )
            return

        assert any(message.role == "tool" and message.tool_call_id == "call-calc" for message in messages)
        assert any(message.role == "tool" and message.tool_call_id == "call-logging" for message in messages)

        yield AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(
                message=Message(role="assistant", content="Done."),
                finish_reason="stop",
            ),
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

    monkeypatch.setattr("nexus.runtime.turn_runner.ContextCompactor.should_compact", lambda self, messages: True)
    monkeypatch.setattr(
        "nexus.runtime.turn_runner.ContextCompactor.compact",
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


def test_prompt_for_confirmation_accepts_yes_turn_aliases():
    request = ConfirmationRequest(
        kind=ConfirmationKind.APPROVAL,
        tool_name="write_file",
        prompt="Allow write_file?",
        reason="Mutating tool requires confirmation.",
        payload={"approval_policy": "on-request"},
    )

    for answer in ("t", "turn", "yes(turn)", "yes (turn)", "yes-turn"):
        response = prompt_for_confirmation(request, input_func=lambda _prompt, value=answer: value)

        assert response.approved is True
        assert response.scope == "turn"


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
    assert len(model_responses) == 2
    assert model_responses[0].payload.message.tool_calls[0].call_id == "call-1"
    assert model_responses[1].payload.message.content == "Created the file."
    assert not any(event.kind == AgentEventType.CONFIRMATION_REQUESTED for event in events)


@pytest.mark.asyncio
async def test_collect_turn_events_requires_confirmation_for_each_mutating_call(tmp_path):
    state = _build_state(tmp_path)
    state.history.append(Message(role="user", content="create two files"))
    agent = _SequentialApprovalAgent()
    seen_requests: list[ConfirmationRequest] = []

    async def _approve(request):
        seen_requests.append(request)
        return ConfirmationResponse(approved=True)

    events = await collect_turn_events(
        state,
        agent,
        prompt_text="create two files",
        approval_callback=_approve,
    )

    tool_results = [event.payload for event in events if event.kind == AgentEventType.TOOL_RESULT]

    assert agent.calls == 3
    assert [request.arguments["path"] for request in seen_requests] == [
        "calculator.py",
        "logging_calculator.py",
    ]
    assert [result.tool_name for result in tool_results] == ["write_file", "write_file"]
    assert [result.call_id for result in tool_results] == ["call-calc", "call-logging"]
    assert not any(event.kind == AgentEventType.CONFIRMATION_REQUESTED for event in events)


@pytest.mark.asyncio
async def test_collect_turn_events_executes_approved_call_without_regenerating_tool_call(tmp_path):
    state = _build_state(tmp_path)
    state.history.append(Message(role="user", content="write hello.py"))
    state.tool_registry.register(WriteFileTool())
    tool_call = ToolCall(
        call_id="note-1",
        tool_name="write_file",
        arguments={"path": "hello.txt", "content": "hello"},
    )
    model = _CountingFakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Writing hello.", tool_calls=(tool_call,)),
                tool_calls=(tool_call,),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content="Done.")),
        ]
    )
    agent = Agent(model_client=model, tool_registry=state.tool_registry)
    seen_requests: list[ConfirmationRequest] = []

    async def _approve(request):
        seen_requests.append(request)
        return ConfirmationResponse(approved=True)

    events = await collect_turn_events(
        state,
        agent,
        prompt_text="write hello.py",
        approval_callback=_approve,
    )

    assert model.calls == 2
    assert [request.call_id for request in seen_requests] == ["note-1"]
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello"
    assert [event.payload.call_id for event in events if event.kind == AgentEventType.TOOL_RESULT] == ["note-1"]


@pytest.mark.asyncio
async def test_collect_turn_events_yes_turn_skips_later_non_dangerous_mutating_confirmations(tmp_path):
    state = _build_state(tmp_path)
    state.tool_registry.register(WriteFileTool())
    state.history.append(Message(role="user", content="create two notes"))

    note_one = ToolCall(
        call_id="note-1",
        tool_name="write_file",
        arguments={"path": "notes/one.txt", "content": "one"},
    )
    note_two = ToolCall(
        call_id="note-2",
        tool_name="write_file",
        arguments={"path": "notes/two.txt", "content": "two"},
    )
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="Creating notes."),
                tool_calls=(note_one, note_two),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(message=Message(role="assistant", content="Done.")),
        ]
    )
    agent = Agent(model_client=model, tool_registry=state.tool_registry)
    seen_requests: list[ConfirmationRequest] = []

    async def _approve(request):
        seen_requests.append(request)
        return ConfirmationResponse(approved=True, scope="turn")

    events = await collect_turn_events(
        state,
        agent,
        prompt_text="create two notes",
        approval_callback=_approve,
    )

    tool_results = [event.payload for event in events if event.kind == AgentEventType.TOOL_RESULT]

    assert len(seen_requests) == 1
    assert seen_requests[0].arguments["path"] == "notes/one.txt"
    assert [result.call_id for result in tool_results] == ["note-1", "note-2"]
    assert (tmp_path / "notes" / "one.txt").read_text(encoding="utf-8") == "one"
    assert (tmp_path / "notes" / "two.txt").read_text(encoding="utf-8") == "two"
    assert not any(event.kind == AgentEventType.CONFIRMATION_REQUESTED for event in events)


@pytest.mark.asyncio
async def test_collect_turn_events_denied_nexus_memory_file_write_continues_turn(tmp_path):
    state = _build_state(tmp_path)
    state.tool_registry.register(WriteFileTool())
    state.history.append(Message(role="user", content="remember my name"))
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="I'll save that."),
                tool_calls=(
                    ToolCall(
                        call_id="call-note",
                        tool_name="write_file",
                        arguments={
                            "path": ".nexus/memory/rishikesh_name.md",
                            "content": "Rishikesh",
                        },
                    ),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(
                message=Message(role="assistant", content="I can't write directly under `.nexus/memory`; I should use the memory tool instead."),
            ),
        ]
    )
    agent = Agent(model_client=model, tool_registry=state.tool_registry)

    events = await collect_turn_events(state, agent, prompt_text="remember my name")

    denied_results = [event.payload for event in events if event.kind == AgentEventType.TOOL_RESULT]
    assert len(denied_results) == 1
    assert denied_results[0].tool_name == "write_file"
    assert denied_results[0].is_error is True
    assert "use the `memory` tool" in denied_results[0].output.lower()
    assert any(event.kind == AgentEventType.TOOL_DENIED for event in events)
    assert any(
        event.kind == AgentEventType.MODEL_RESPONSE
        and event.payload.message.content.startswith("I can't write directly")
        for event in events
    )
    assert any(event.kind == AgentEventType.TURN_COMPLETED for event in events)


@pytest.mark.asyncio
async def test_collect_turn_events_denied_memory_write_is_not_reprompted_same_turn(tmp_path):
    state = _build_state(tmp_path)
    state.tool_registry.register(MemoryTool(memory_dir=state.config.memory_dir))
    state.history.append(Message(role="user", content="remember my name"))
    model = FakeModelClient(
        scripted=[
            RuntimeResponse(
                message=Message(role="assistant", content="I'll remember that."),
                tool_calls=(
                    ToolCall(
                        call_id="call-memory-1",
                        tool_name="memory",
                        arguments={"action": "set", "key": "user_name", "value": "rishikesh"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(
                message=Message(role="assistant", content="I'll try again."),
                tool_calls=(
                    ToolCall(
                        call_id="call-memory-2",
                        tool_name="memory",
                        arguments={"action": "set", "key": "user_name", "value": "rishikesh"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            RuntimeResponse(
                message=Message(role="assistant", content="Okay, I won't store it right now. What's next?"),
            ),
        ]
    )
    agent = Agent(model_client=model, tool_registry=state.tool_registry)
    seen_requests: list[ConfirmationRequest] = []

    async def _deny(request):
        seen_requests.append(request)
        return ConfirmationResponse()

    events = await collect_turn_events(
        state,
        agent,
        prompt_text="remember my name",
        approval_callback=_deny,
    )

    tool_results = [event.payload for event in events if event.kind == AgentEventType.TOOL_RESULT]

    assert len(seen_requests) == 1
    assert len(tool_results) == 1
    assert tool_results[0].call_id == "call-memory-2"
    assert tool_results[0].is_error is True
    assert "previously denied" in tool_results[0].output.lower()
    assert not (state.config.memory_dir / "user_memory.json").exists()
    assert any(
        event.kind == AgentEventType.MODEL_RESPONSE
        and event.payload.message.content == "Okay, I won't store it right now. What's next?"
        for event in events
    )
    assert not any(event.kind == AgentEventType.CONFIRMATION_REQUESTED for event in events)
