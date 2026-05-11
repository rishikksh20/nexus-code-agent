from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


@dataclass
class TextDelta:
    content: str

    def __str__(self):
        return self.content


class StreamEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    MESSAGE_COMPLETE = "message_complete"
    ERROR = "error"

    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_COMPLETE = "tool_call_complete"

@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_tokens=self.cache_tokens + other.cache_tokens
        )


@dataclass
class ToolCallDelta:
    call_id: str
    name: str | None = None
    arguments_delta: str = None

@dataclass
class ToolCall:
    call_id: str
    name: str | None = None
    arguments: str = None


@dataclass
class StreamEvent:
    type: StreamEventType
    text_delta: TextDelta|None = None
    error: str|None = None
    finish_reason: str|None = None
    tool_call_delta: ToolCallDelta|None = None
    tool_call: ToolCall|None = None
    usage: TokenUsage|None = None

@dataclass
class ToolResultMessage:
    tool_call_id: str
    content: str
    is_error: bool = False

    def to_openai_message(self) -> dict[str, Any] :
        return {
            "role": "tool",
            "content": self.content,
            "tool_call_id": self.tool_call_id,
            "is_error": self.is_error,
        }

def parse_tool_call_arguments(arguments: str) -> dict[str, Any]:
    """
    A simple parser for tool call arguments in the format of "key1=value1;key2=value2"
    """
    result = {}
    if not arguments:
        return result

    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return {"raw_arguments": arguments}