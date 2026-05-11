from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal


Role = Literal["system", "user", "assistant", "tool"]


# ---------------------------------------------------------------------------
# Agent-level event types (emitted by the agentic loop)
# ---------------------------------------------------------------------------

class AgentEventType(str, Enum):
    """Typed event kinds emitted by the :class:`Agent` agentic loop.

    The enum inherits from ``str`` so that existing ``event.kind == "..."``
    comparisons continue to work without any migration.
    """

    # Reference-style streaming events
    AGENT_START = "AGENT_START"
    AGENT_STOP = "AGENT_STOP"
    AGENT_ERROR = "AGENT_ERROR"
    TEXT_DELTA = "TEXT_DELTA"
    TEXT_COMPLETE = "TEXT_COMPLETE"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_COMPLETE = "TOOL_CALL_COMPLETE"

    # Nexus-specific events (kept for backward compatibility)
    THINKING_STARTED = "thinking_started"
    MODEL_RESPONSE = "model_response"
    TURN_COMPLETED = "turn_completed"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_DENIED = "tool_denied"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    TOOL_RESULT = "tool_result"


# ---------------------------------------------------------------------------
# Model-client stream event types (OpenAI-compatible wire layer)
# ---------------------------------------------------------------------------

class StreamEventType(str, Enum):
    """Event types produced by a streaming model client (wire layer)."""

    TEXT_DELTA = "text_delta"
    MESSAGE_COMPLETE = "message_complete"
    ERROR = "error"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_COMPLETE = "tool_call_complete"


@dataclass
class TextDelta:
    """A single streamed text chunk from the model."""

    content: str

    def __str__(self) -> str:
        return self.content


@dataclass
class ToolCallDelta:
    """Incremental update for an in-progress tool call during streaming."""

    call_id: str
    name: str | None = None
    arguments_delta: str | None = None


@dataclass(slots=True, frozen=True)
class Message:
    role: Role
    content: str
    name: str | None = None
    # Populated on assistant messages that called tools; required for Mistral/OpenAI
    # multi-turn history so the provider can correlate assistant → tool results.
    tool_calls: tuple[ToolCall, ...] = ()
    # Populated on tool-result messages; must match the corresponding call_id.
    tool_call_id: str | None = None


@dataclass(slots=True, frozen=True)
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    call_id: str
    tool_name: str
    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class UsageSnapshot:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float = 0.0
    provider: str = ""
    model: str = ""


# ---------------------------------------------------------------------------
# Stream event — produced by the model client wire layer
# ---------------------------------------------------------------------------

@dataclass
class StreamEvent:
    """A single event from a streaming (or non-streaming) model client call.

    The ``type`` field uses :class:`StreamEventType` to describe what kind of
    information this event carries.  Callers should check ``type`` and read the
    corresponding optional field.
    """

    type: StreamEventType
    text_delta: TextDelta | None = None
    error: str | None = None
    finish_reason: str | None = None
    tool_call_delta: ToolCallDelta | None = None
    # Completed tool call — populated on TOOL_CALL_COMPLETE events.
    # Uses the nexus ToolCall type (arguments already parsed from JSON).
    tool_call: ToolCall | None = None
    usage: UsageSnapshot | None = None


@dataclass(slots=True, frozen=True)
class CorrelationContext:
    session_id: str
    turn_id: str
    trace_id: str
    tool_call_id: str | None = None
    worker_id: str | None = None


@dataclass(slots=True, frozen=True)
class TurnTelemetry:
    correlation: CorrelationContext
    usage: UsageSnapshot | None
    tool_calls: int
    duration_ms: float
    status: str


@dataclass(slots=True, frozen=True)
class RuntimeRequest:
    model_name: str
    system_prompt: str
    messages: tuple[Message, ...]
    tool_schemas: tuple[dict[str, Any], ...] = ()
    max_output_tokens: int | None = None
    temperature: float = 0.0


@dataclass(slots=True, frozen=True)
class RuntimeResponse:
    message: Message
    tool_calls: tuple[ToolCall, ...] = ()
    usage: UsageSnapshot | None = None
    finish_reason: str = "done"


ModelResponse = RuntimeResponse


@dataclass(slots=True)
class ToolExecutionContext:
    session_id: str
    working_directory: Path
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ConfirmationKind(str, Enum):
    APPROVAL = "approval"
    CLARIFICATION = "clarification"


@dataclass(slots=True, frozen=True)
class ConfirmationRequest:
    kind: ConfirmationKind
    tool_name: str
    prompt: str
    reason: str
    payload: dict[str, str] = field(default_factory=dict)
    # Tool arguments carried through so the confirmation UI can show exactly what
    # will be written/executed before asking the user for approval.
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ConfirmationResponse:
    approved: bool = False
    clarification: str = ""

    @property
    def denied(self) -> bool:
        return not self.approved and not self.clarification


@dataclass(slots=True, frozen=True)
class AgentEvent:
    """An event emitted by the :class:`~nexus.runtime.agent.Agent` agentic loop.

    ``kind`` is an :class:`AgentEventType` value.  Because ``AgentEventType``
    inherits from ``str``, existing ``event.kind == "..."`` comparisons remain
    valid without any changes at call sites.
    """

    kind: AgentEventType
    payload: Any | None = None

    # ------------------------------------------------------------------
    # Factory helpers — mirror the reference event-builder pattern
    # ------------------------------------------------------------------

    @classmethod
    def agent_start(cls, message: str) -> AgentEvent:
        return cls(kind=AgentEventType.AGENT_START, payload=message)

    @classmethod
    def agent_stop(cls, response: str | None = None, usage: UsageSnapshot | None = None) -> AgentEvent:
        return cls(kind=AgentEventType.AGENT_STOP, payload={"response": response, "usage": usage})

    @classmethod
    def agent_error(cls, error: str | None, detail: Any = None) -> AgentEvent:
        return cls(kind=AgentEventType.AGENT_ERROR, payload={"error": error, "detail": detail})

    @classmethod
    def text_delta(cls, content: str) -> AgentEvent:
        return cls(kind=AgentEventType.TEXT_DELTA, payload=content)

    @classmethod
    def text_complete(cls, content: str) -> AgentEvent:
        return cls(kind=AgentEventType.TEXT_COMPLETE, payload=content)

    @classmethod
    def tool_call_start(cls, call_id: str, name: str, arguments: dict[str, Any]) -> AgentEvent:
        return cls(
            kind=AgentEventType.TOOL_CALL_START,
            payload={"call_id": call_id, "name": name, "arguments": arguments},
        )

    @classmethod
    def tool_call_complete(cls, result: ToolResult) -> AgentEvent:
        return cls(kind=AgentEventType.TOOL_CALL_COMPLETE, payload=result)
