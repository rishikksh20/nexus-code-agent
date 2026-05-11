from __future__ import annotations

import time

from collections.abc import AsyncGenerator
from typing import Any, Protocol, runtime_checkable

from nexus.models import (
    AgentEvent,
    AgentEventType,
    ConfirmationKind,
    ConfirmationRequest,
    Message,
    RuntimeRequest,
    RuntimeResponse,
    StreamEvent,
    StreamEventType,
    ToolCall,
    ToolExecutionContext,
    ToolResult,
    UsageSnapshot,
)
from nexus.runtime.execution import ExecutionMode
from nexus.hooks import HookEvent, HookExecutor
from nexus.runtime.permissions import PermissionChecker, PermissionDecision
from nexus.tools.base import ToolRegistry


@runtime_checkable
class ModelClient(Protocol):
    """Protocol that every model client must satisfy.

    The primary interface is :meth:`chat_completion`, which yields
    :class:`~nexus.models.StreamEvent` objects for both streaming and
    non-streaming provider calls.  The legacy :meth:`complete` method is
    retained for backward compatibility with existing tests.
    """

    async def chat_completion(
        self,
        request: RuntimeRequest,
        *,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        ...


class Agent:
    def __init__(
        self,
        model_client: ModelClient,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker | None = None,
        hooks: HookExecutor | None = None,
    ) -> None:
        self.model_client = model_client
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker or PermissionChecker()
        self.hooks = hooks or HookExecutor()

    async def run(
        self,
        messages: list[Message],
        context: ToolExecutionContext,
        *,
        system_prompt: str = "You are Nexus, a CLI-first agent.",
        model_name: str = "fake-model",
        mode: ExecutionMode = ExecutionMode.DEFAULT,
        approved_tools: set[str] | None = None,
        auto_confirm: bool = False,
        auto_confirm_read_only: bool = True,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        max_turns: int = 3,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Run the agentic loop, yielding :class:`AgentEvent` objects.

        Emits both the new reference-style events (``TEXT_DELTA``,
        ``TEXT_COMPLETE``, ``TOOL_CALL_START``, ``TOOL_CALL_COMPLETE``,
        ``AGENT_START``, ``AGENT_STOP``) and the legacy Nexus events
        (``model_response``, ``tool_call_requested``, ``tool_result``,
        ``turn_completed``) so existing consumers continue to work unchanged.
        """
        history = list(messages)
        approved = approved_tools or set()
        final_response: str | None = None

        yield AgentEvent(kind=AgentEventType.THINKING_STARTED)
        yield AgentEvent.agent_start(messages[-1].content if messages else "")

        async for event in self._agentic_loop(
            history,
            context,
            system_prompt=system_prompt,
            model_name=model_name,
            mode=mode,
            approved_tools=approved,
            auto_confirm=auto_confirm,
            auto_confirm_read_only=auto_confirm_read_only,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            max_turns=max_turns,
        ):
            if event.kind == AgentEventType.TEXT_COMPLETE:
                final_response = event.payload
            yield event

        yield AgentEvent.agent_stop(response=final_response)

    async def _agentic_loop(
        self,
        history: list[Message],
        context: ToolExecutionContext,
        *,
        system_prompt: str,
        model_name: str,
        mode: ExecutionMode,
        approved_tools: set[str],
        auto_confirm: bool,
        auto_confirm_read_only: bool,
        temperature: float,
        max_output_tokens: int | None,
        max_turns: int,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Core agentic loop — processes stream events and executes tools."""

        for _ in range(max_turns):
            request = RuntimeRequest(
                model_name=model_name,
                system_prompt=system_prompt,
                messages=tuple(history),
                tool_schemas=self.tool_registry.schemas(),
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )

            # ----------------------------------------------------------------
            # Stream from the model client
            # ----------------------------------------------------------------
            response_text = ""
            stream_tool_calls: list[ToolCall] = []
            usage: UsageSnapshot | None = None

            async for stream_event in self.model_client.chat_completion(request, stream=True):
                if stream_event.type == StreamEventType.TEXT_DELTA:
                    if stream_event.text_delta and stream_event.text_delta.content:
                        chunk = stream_event.text_delta.content
                        response_text += chunk
                        yield AgentEvent.text_delta(chunk)

                elif stream_event.type == StreamEventType.TOOL_CALL_COMPLETE:
                    if stream_event.tool_call:
                        stream_tool_calls.append(stream_event.tool_call)

                elif stream_event.type == StreamEventType.MESSAGE_COMPLETE:
                    usage = stream_event.usage

                elif stream_event.type == StreamEventType.ERROR:
                    yield AgentEvent.agent_error(stream_event.error)
                    return

            if response_text:
                yield AgentEvent.text_complete(response_text)

            # Build the assistant message and RuntimeResponse for history.
            tool_calls: tuple[ToolCall, ...] = tuple(stream_tool_calls)
            message = Message(
                role="assistant",
                content=response_text or "",
                tool_calls=tool_calls,
            )
            history.append(message)

            runtime_response = RuntimeResponse(
                message=message,
                tool_calls=tool_calls,
                usage=usage,
                finish_reason="tool_calls" if tool_calls else "stop",
            )

            if usage is not None:
                await self.hooks.emit(
                    HookEvent.NOTIFICATION,
                    {
                        "event": "model_usage",
                        "session_id": context.session_id,
                        **_correlation_payload(context),
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                        "estimated_cost_usd": usage.estimated_cost_usd,
                        "provider": usage.provider,
                        "model": usage.model,
                    },
                )

            # Emit the legacy MODEL_RESPONSE event for backward-compatible consumers
            # (history management in apply_events_to_history, etc.).
            yield AgentEvent(kind=AgentEventType.MODEL_RESPONSE, payload=runtime_response)

            if not tool_calls:
                yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload=runtime_response.finish_reason)
                return

            # ----------------------------------------------------------------
            # Tool execution
            # ----------------------------------------------------------------
            for tool_call in tool_calls:
                # Emit both new TOOL_CALL_START and legacy TOOL_CALL_REQUESTED.
                yield AgentEvent.tool_call_start(
                    tool_call.call_id, tool_call.tool_name, tool_call.arguments
                )
                yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)

                record = self.tool_registry.record(tool_call.tool_name)
                tool = record.tool

                missing_fields = _missing_required_fields(tool.input_schema, tool_call.arguments)
                if missing_fields:
                    missing_field = missing_fields[0]
                    await self.hooks.emit(
                        HookEvent.NOTIFICATION,
                        {
                            "event": "clarification_requested",
                            "tool_name": tool.name,
                            "tool_source": record.source,
                            "tool_origin": record.origin,
                            "field": missing_field,
                            "session_id": context.session_id,
                            "call_id": tool_call.call_id,
                            **_correlation_payload(context, tool_call_id=tool_call.call_id),
                        },
                    )
                    yield AgentEvent(
                        kind=AgentEventType.CONFIRMATION_REQUESTED,
                        payload=ConfirmationRequest(
                            kind=ConfirmationKind.CLARIFICATION,
                            tool_name=tool.name,
                            prompt=f"Provide a value for '{missing_field}' before running '{tool.name}'.",
                            reason="Required tool argument is missing.",
                            payload={"tool_name": tool.name, "field": missing_field},
                            arguments=tool_call.arguments,
                        ),
                    )
                    return

                decision = self.permission_checker.evaluate(
                    tool,
                    tool_call.arguments,
                    mode,
                    context=context,
                    auto_confirm_read_only=auto_confirm_read_only,
                )

                if decision.decision is PermissionDecision.DENY:
                    await self.hooks.emit(
                        HookEvent.NOTIFICATION,
                        {
                            "event": "tool_denied",
                            "tool_name": tool.name,
                            "tool_source": record.source,
                            "tool_origin": record.origin,
                            "arguments": tool_call.arguments,
                            "reason": decision.reason,
                            "session_id": context.session_id,
                            "call_id": tool_call.call_id,
                            **_correlation_payload(context, tool_call_id=tool_call.call_id),
                        },
                    )
                    yield AgentEvent(kind=AgentEventType.TOOL_DENIED, payload=decision)
                    continue

                if (
                    decision.decision is PermissionDecision.CONFIRM
                    and not auto_confirm
                    and tool.name not in approved_tools
                ):
                    await self.hooks.emit(
                        HookEvent.NOTIFICATION,
                        {
                            "event": "confirmation_requested",
                            "tool_name": tool.name,
                            "tool_source": record.source,
                            "tool_origin": record.origin,
                            "arguments": tool_call.arguments,
                            "reason": decision.reason,
                            "session_id": context.session_id,
                            "call_id": tool_call.call_id,
                            **_correlation_payload(context, tool_call_id=tool_call.call_id),
                        },
                    )
                    yield AgentEvent(
                        kind=AgentEventType.CONFIRMATION_REQUESTED,
                        payload=ConfirmationRequest(
                            kind=ConfirmationKind.APPROVAL,
                            tool_name=tool.name,
                            prompt=f"Allow tool '{tool.name}'?",
                            reason=decision.reason,
                            payload={"tool_name": tool.name, "reason": decision.reason},
                            arguments=tool_call.arguments,
                        ),
                    )
                    return

                await self.hooks.emit(
                    HookEvent.PRE_TOOL_USE,
                    {
                        "tool_name": tool.name,
                        "tool_source": record.source,
                        "tool_origin": record.origin,
                        "arguments": tool_call.arguments,
                        "call_id": tool_call.call_id,
                        "session_id": context.session_id,
                        "is_mutating": tool.is_mutating,
                        **_correlation_payload(context, tool_call_id=tool_call.call_id),
                    },
                )

                started_at = time.perf_counter()
                result = await tool.execute(tool_call.call_id, tool_call.arguments, context)
                history.append(Message(
                    role="tool",
                    content=result.output,
                    name=result.tool_name,
                    tool_call_id=result.call_id,
                ))

                await self.hooks.emit(
                    HookEvent.POST_TOOL_USE,
                    {
                        "tool_name": tool.name,
                        "tool_source": record.source,
                        "tool_origin": record.origin,
                        "arguments": tool_call.arguments,
                        "call_id": tool_call.call_id,
                        "session_id": context.session_id,
                        "is_mutating": tool.is_mutating,
                        "is_error": result.is_error,
                        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                        "output": result.output,
                        **_correlation_payload(context, tool_call_id=tool_call.call_id),
                    },
                )

                # Emit both new TOOL_CALL_COMPLETE and legacy TOOL_RESULT events.
                yield AgentEvent.tool_call_complete(result)
                yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)

        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="max_turns")


def _missing_required_fields(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    missing: list[str] = []
    for field_name in required:
        if field_name not in arguments or arguments[field_name] is None:
            missing.append(field_name)
            continue
        value = arguments[field_name]
        if isinstance(value, str):
            min_length = properties.get(field_name, {}).get("minLength", 0)
            if min_length > 0 and not value.strip():
                missing.append(field_name)
    return missing


def _correlation_payload(context: ToolExecutionContext, *, tool_call_id: str | None = None) -> dict[str, Any]:
    payload = {
        "turn_id": context.metadata.get("turn_id"),
        "trace_id": context.metadata.get("trace_id"),
        "worker_id": context.metadata.get("worker_id"),
    }
    if tool_call_id is not None:
        payload["tool_call_id"] = tool_call_id
    return {key: value for key, value in payload.items() if value}
