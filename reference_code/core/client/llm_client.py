import asyncio
import os
from typing import Any, AsyncGenerator, List

from openai import AsyncOpenAI, RateLimitError, APIConnectionError, APIError

from core.config.config import Config
from core.client.datatype import parse_tool_call_arguments
from core.client.datatype import TextDelta, TokenUsage, StreamEvent, StreamEventType, ToolCallDelta, ToolCall


class LLMClient:
    def __init__(self, config: Config) -> None:
        self._client: AsyncOpenAI | None = None
        self._max_attempts = 3
        self.config = config

    def get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key, #"yhu9zneVhKRxeO24RPphduugJTnTm3QI",
                base_url=self.config.base_url #"https://api.mistral.ai/v1",
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _build_tool(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Convert our internal tool representation to the format expected by the API
        return [
            {
                "type": "function",
                "function" : {
                    "name" : tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            }
        }
        for tool in tools
        ]

    async def chat_completion(self, message: list[dict[str, Any]],
                              tools: list[dict[str, Any]]| None = None,
                              stream: bool = True) -> AsyncGenerator[
        StreamEvent, None]:

        client = self.get_client()

        kwargs: dict[str, Any] = {
            "model": self.config.model_name,
            "messages": message,
            "stream": stream,
        }

        if tools:
            kwargs["tools"] = self._build_tool(tools)
            kwargs["tool_choice"] = "auto"


        for attempt in range(self._max_attempts):
            try:
                if stream:
                    async for event in  self._stream_response(client, kwargs):
                        yield event
                else:
                    event = await self._non_stream_response(client, kwargs)
                    yield event

                return
            except RateLimitError as e:
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"Rate limit exceeded: {str(e)}"
                    )
            except APIConnectionError as e:
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"API connection error: {str(e)}"
                    )
            except APIConnectionError as e:
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"API connection error: {str(e)}"
                    )
            except APIError as e:
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
                else:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=f"API error: {str(e)}"
                    )

    async def _stream_response(self, client: AsyncOpenAI, kwargs: dict[str, Any]) -> AsyncGenerator[StreamEvent, None]:
        response = await client.chat.completions.create(**kwargs)
        usage: TokenUsage = None
        finish_reason = None
        tool_calls: dict[int, dict[str, Any]] = {}

        async for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                usage = TokenUsage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    total_tokens=chunk.usage.total_tokens,
                    cache_tokens=chunk.usage.prompt_tokens_details.cached_tokens,
                )

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                finish_reason = choice.finish_reason

            if delta.content:
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(content=delta.content),
                )

            if delta.tool_calls:
                for tool_call_delta in delta.tool_calls:
                    idx = tool_call_delta.index

                    if idx not in delta.tool_calls:
                        tool_calls[idx] = {
                            'id': tool_call_delta.id or "",
                            'name': '',
                            'arguments': ''
                        }

                        if tool_call_delta.function:
                            if tool_call_delta.function.name:
                                tool_calls[idx]['name'] = tool_call_delta.function.name
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL_START,
                                    tool_call_delta=ToolCallDelta(
                                        call_id=tool_calls[idx]['id'],
                                        name=tool_call_delta.function.name,

                                    )

                                )
                            if tool_call_delta.function.arguments:
                                tool_calls[idx]['arguments'] += tool_call_delta.function.arguments
                                yield StreamEvent(
                                    type=StreamEventType.TOOL_CALL_DELTA,
                                    tool_call_delta=ToolCallDelta(
                                        call_id=tool_calls[idx]['id'],
                                        name=tool_call_delta.function.name,
                                        arguments_delta=tool_call_delta.function.arguments
                                    )

                                )

        for idx, tc in tool_calls.items():
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=tc['id'],
                    name=tc['name'],
                    arguments=parse_tool_call_arguments(tc['arguments'])

                )
            )

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
        )


    async def _non_stream_response(self, client: AsyncOpenAI, kwargs: dict[str, Any]) -> StreamEvent:

        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message
        text_delta = None
        if message.content:
            text_delta = TextDelta(content=message.content)

        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        call_id=tc.id or "",
                        name=tc.function.name if tc.function and tc.function.name else "",
                        arguments=parse_tool_call_arguments(tc.function.arguments) if tc.function and tc.function.arguments else {}
                    )
                )

        usage = None
        if response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cache_tokens=response.usage.prompt_tokens_details.cached_tokens,
            )

        return StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            finish_reason=choice.finish_reason,
            usage=usage,
        )
