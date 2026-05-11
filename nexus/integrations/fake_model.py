from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Sequence

from nexus.models import (
    Message,
    RuntimeRequest,
    RuntimeResponse,
    StreamEvent,
    StreamEventType,
    TextDelta,
    ToolCall,
    UsageSnapshot,
)


class FakeModelClient:
    """Deterministic fake provider for development and tests."""

    def __init__(self, scripted: Sequence[RuntimeResponse] | None = None) -> None:
        self._scripted = list(scripted or [])

    async def complete(self, request: RuntimeRequest) -> RuntimeResponse:
        if self._scripted:
            return self._scripted.pop(0)

        messages = list(request.messages)
        last_message = messages[-1] if messages else Message(role="user", content="")

        if last_message.role == "tool":
            content = f"Completed {last_message.name}: {last_message.content}"
            return RuntimeResponse(
                message=Message(
                    role="assistant",
                    content=content,
                ),
                usage=_estimate_usage(request, content),
            )

        lowered = last_message.content.lower()
        if "time" in lowered:
            content = "Checking the current UTC time."
            return RuntimeResponse(
                message=Message(role="assistant", content=content),
                tool_calls=(
                    ToolCall(call_id="call-get-time", tool_name="get_time", arguments={}),
                ),
                usage=_estimate_usage(request, content),
                finish_reason="tool_calls",
            )

        content = f"Echo: {last_message.content}"
        return RuntimeResponse(
            message=Message(role="assistant", content=content),
            usage=_estimate_usage(request, content),
        )

    async def chat_completion(
        self,
        request: RuntimeRequest,
        *,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Wrap :meth:`complete` output as :class:`StreamEvent` objects.

        This provides the same interface as
        :class:`~nexus.integrations.openai_compatible.OpenAICompatibleModelClient`
        so the :class:`~nexus.runtime.agent.Agent` can use the same code path
        for both real and fake providers.
        """
        response = await self.complete(request)

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

    async def stream(self, request: RuntimeRequest) -> AsyncIterator[str]:
        response = await self.complete(request)
        for chunk in response.message.content.split():
            yield chunk + " "


def _estimate_usage(request: RuntimeRequest, response_text: str) -> UsageSnapshot:
    prompt_text = request.system_prompt + "\n" + "\n".join(message.content for message in request.messages)
    prompt_tokens = max(1, len(prompt_text) // 4)
    completion_tokens = max(1, len(response_text) // 4)
    total_tokens = prompt_tokens + completion_tokens
    return UsageSnapshot(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=round(total_tokens * 0.000001, 6),
        provider="fake",
        model=request.model_name,
    )
