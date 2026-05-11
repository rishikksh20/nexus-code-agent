from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.client.datatype import TokenUsage
from core.tools.base import ToolResult


class AgentEventType(str, Enum):

    # Agent lifecycle
    AGENT_START = "AGENT_START"
    AGENT_STOP = "AGENT_STOP"
    AGENT_RESET = "AGENT_RESET"
    AGENT_ERROR = "AGENT_ERROR"

    # Tool calls
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_COMPLETE = "TOOL_CALL_COMPLETE"


    # Text streaming
    TEXT_DELTA = "TEXT_DELTA"
    TEXT_COMPLETE = "TEXT_COMPLETE"

@dataclass
class AgentEvent:
    type: AgentEventType
    data: dict[str, Any] = field(default_factory=dict) # It's not null but empty dict

    @classmethod
    def agent_start(cls, message: dict[str, Any]) -> AgentEvent:
        return cls(AgentEventType.AGENT_START, data={"message": message})

    @classmethod
    def agent_stop(cls, response: str|None = None, usage: TokenUsage | None = None) -> AgentEvent:
        return cls(AgentEventType.AGENT_STOP, data={"response": response ,"usage": usage.__dict__ if usage else None})

    @classmethod
    def agent_error(cls, error: str|None, detail: dict[str, Any]|None = None) -> AgentEvent:
        return cls(AgentEventType.AGENT_ERROR, data={"error": error, "detail": detail or {}})

    @classmethod
    def text_delta(cls, message: str|None) -> AgentEvent:
        return cls(AgentEventType.TEXT_DELTA, data={"message": message})

    @classmethod
    def text_complete(cls, message: str|None) -> AgentEvent:
        return cls(AgentEventType.TEXT_COMPLETE, data={"message": message})

    @classmethod
    def tool_call_start(cls, call_id: str, name: str, arguments: dict[str, Any ]) -> AgentEvent:
        return cls(AgentEventType.TOOL_CALL_START, data={"call_id": call_id, "name": name, "arguments": arguments})

    @classmethod
    def tool_call_complete(cls, call_id: str, name: str, result: ToolResult) -> AgentEvent:
        return cls(AgentEventType.TOOL_CALL_COMPLETE,
                   data={
                            "call_id": call_id,
                            "name": name,
                            "success": result.success,
                            "output": result.output,
                            "error": result.error,
                            "metadata": result.metadata,
                            "diff": result.diff.to_diff() if result.diff else None,
                            "truncated": result.truncated,
                            "exit_code": result.exit_code,
                        },
                   )

