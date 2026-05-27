from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from uuid import uuid4

from dataclasses import dataclass, field
from collections.abc import AsyncGenerator
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

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
from nexus.observability import capture_exception_from_hooks, sentry_monitor_from_hooks
from nexus.runtime.execution import ExecutionMode
from nexus.hooks import HookEvent, HookExecutor
from nexus.security import PermissionChecker, PermissionDecision
from nexus.context import LoopDetector, prune_tool_outputs
from nexus.security.manager import ApprovalManager
from nexus.tools.base import FileDiff, ToolConfirmation, ToolKind, ToolRegistry, tool_to_schema


logger = logging.getLogger(__name__)


TOOL_CALL_LIMIT_FINISH_REASON = "tool_call_limit"
MAX_TURNS_FINISH_REASON = "max_turns"
INVALID_TOOL_CALL_RETRY_LIMIT = 2
_READ_RESULT_CACHE_METADATA_KEY = "nexus_read_result_cache"
_CACHEABLE_READ_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "lsp",
        "git_status",
        "git_diff",
        "code_index",
        "semantic_search",
    }
)


@dataclass(slots=True)
class _PreparedToolCall:
    tool_call: ToolCall
    record: Any | None = None
    tool: Any | None = None
    confirmation_preview: dict[str, Any] = field(default_factory=dict)
    affected_paths: set[str] = field(default_factory=set)
    immediate_result: ToolResult | None = None
    confirmation_request: ConfirmationRequest | None = None
    permission_decision: Any | None = None
    emit_start_event: bool = False
    loop_result: str | None = None
    stop_message: str | None = None


@dataclass(slots=True)
class _ToolBatchState:
    tool_result_messages: list[Message] = field(default_factory=list)
    executed_count: int = 0
    stop_reason: str | None = None


@runtime_checkable
class ModelClient(Protocol):
    """Protocol that every model client must satisfy.

    The primary interface is :meth:`chat_completion`, which yields
    :class:`~nexus.models.StreamEvent` objects for both streaming and
    non-streaming provider calls.  The legacy :meth:`complete` method is
    retained for backward compatibility with existing tests.
    """

    def chat_completion(
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
        approval_manager: ApprovalManager | None = None,
        auto_confirm: bool = False,
        auto_confirm_read_only: bool = True,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        max_turns: int = 3,
        max_tool_calls_per_turn: int = 30,
        parallel_tools: bool = False,
        parallel_tool_window: int = 4,
        resume_tool_calls: tuple[ToolCall, ...] = (),
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

        if resume_tool_calls:
            async for event in self._execute_approved_tool_calls(
                resume_tool_calls,
                context,
                approval_manager=approval_manager,
            ):
                yield event
            return

        yield AgentEvent.thinking_started(actor=_tool_actor(context))
        yield AgentEvent.agent_start(messages[-1].content if messages else "")

        async for event in self._agentic_loop(
            history,
            context,
            system_prompt=system_prompt,
            model_name=model_name,
            mode=mode,
            approved_tools=approved,
            approval_manager=approval_manager,
            auto_confirm=auto_confirm,
            auto_confirm_read_only=auto_confirm_read_only,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            max_turns=max_turns,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
            parallel_tools=parallel_tools,
            parallel_tool_window=parallel_tool_window,
        ):
            if event.kind == AgentEventType.TEXT_COMPLETE:
                final_response = event.payload
            yield event

        yield AgentEvent.agent_stop(response=final_response)

    async def _execute_approved_tool_calls(
        self,
        tool_calls: tuple[ToolCall, ...],
        context: ToolExecutionContext,
        *,
        approval_manager: ApprovalManager | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        mutating_paths_this_batch: set[str] = set()
        for tool_call in tool_calls:
            try:
                record = self.tool_registry.record(tool_call.tool_name)
            except LookupError:
                result = _unknown_tool_result(
                    tool_call,
                    self.tool_registry,
                    retry_count=1,
                    retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
                )
                yield AgentEvent.tool_call_start(
                    tool_call.call_id,
                    str(tool_call.tool_name),
                    tool_call.arguments,
                    actor=_tool_actor(context),
                    display=_tool_display_metadata(),
                )
                yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                yield AgentEvent.tool_call_complete(result)
                yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)
                continue
            if not _tool_available_in_context(record, context):
                result = _tool_not_available_result(tool_call, self.tool_registry, context)
                yield AgentEvent.tool_call_start(
                    tool_call.call_id,
                    str(tool_call.tool_name),
                    tool_call.arguments,
                    actor=_tool_actor(context),
                    display=_tool_display_metadata(),
                )
                yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                yield AgentEvent.tool_call_complete(result)
                yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)
                continue
            tool = record.tool
            confirmation = await _get_tool_confirmation(tool, tool_call.call_id, tool_call.arguments, context)
            confirmation_preview = _confirmation_preview(confirmation)
            affected_paths = _affected_file_paths(tool, tool_call.arguments, confirmation, context)
            duplicate_paths = affected_paths & mutating_paths_this_batch if tool.is_mutating else set()
            if duplicate_paths:
                blocked_result = _same_file_mutation_result(tool_call, duplicate_paths)
                yield AgentEvent.tool_call_complete(blocked_result)
                yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=blocked_result)
                continue
            if tool.is_mutating:
                mutating_paths_this_batch.update(affected_paths)
            async for event in self._execute_tool_call(
                record,
                tool,
                tool_call,
                context,
                approval_manager=approval_manager,
                confirmation_preview=confirmation_preview,
                affected_paths=affected_paths,
            ):
                yield event

    def preapproved_tool_calls_from_batch(
        self,
        tool_calls: tuple[ToolCall, ...],
        *,
        first_tool_call: ToolCall,
        mode: ExecutionMode,
        context: ToolExecutionContext,
        approval_manager: ApprovalManager,
        auto_confirm_read_only: bool,
    ) -> tuple[ToolCall, ...]:
        """Return same-batch calls executable under the current approval state."""
        if not tool_calls:
            return (first_tool_call,)
        start_index = next(
            (index for index, call in enumerate(tool_calls) if call.call_id == first_tool_call.call_id),
            0,
        )
        approved_calls: list[ToolCall] = []
        mutating_paths_this_batch: set[str] = set()
        for call in tool_calls[start_index:]:
            try:
                record = self.tool_registry.record(call.tool_name)
            except Exception:
                continue
            if not _tool_available_in_context(record, context):
                continue
            affected_paths = _affected_file_paths(record.tool, call.arguments, None, context)
            if record.tool.is_mutating and affected_paths & mutating_paths_this_batch:
                continue
            decision = self.permission_checker.evaluate(
                record.tool,
                call.arguments,
                mode,
                context=context,
                auto_confirm_read_only=auto_confirm_read_only,
            )
            risk_level = _risk_level_name(decision.risk_level)
            if decision.decision is PermissionDecision.DENY:
                continue
            if approval_manager.is_pre_approved(
                call.tool_name,
                call.arguments,
            ) or approval_manager.is_turn_wide_mutating_preapproved(
                call.tool_name,
                is_mutating=record.tool.is_mutating,
                risk_level=risk_level,
            ):
                approved_calls.append(call)
                if record.tool.is_mutating:
                    mutating_paths_this_batch.update(affected_paths)
        return tuple(approved_calls or (first_tool_call,))

    async def _execute_tool_call(
        self,
        record: Any,
        tool: Any,
        tool_call: ToolCall,
        context: ToolExecutionContext,
        *,
        approval_manager: ApprovalManager | None,
        confirmation_preview: dict[str, Any],
        affected_paths: set[str] | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        actor = _tool_actor(context)
        yield AgentEvent.tool_call_start(
            tool_call.call_id,
            tool_call.tool_name,
            tool_call.arguments,
            preview=confirmation_preview,
            actor=actor,
            display=_tool_display_metadata(is_mutating=tool.is_mutating),
        )
        yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)

        cached_result = _cached_read_result(record, tool, tool_call, context)
        if cached_result is not None:
            yield AgentEvent.tool_call_complete(cached_result)
            yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=cached_result)
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
        monitor = sentry_monitor_from_hooks(self.hooks)
        span = (
            monitor.start_span(
                op="nexus.tool",
                name=tool.name,
                attributes={
                    "nexus.tool.name": tool.name,
                    "nexus.tool.source": record.source,
                    "nexus.tool.origin": record.origin,
                    "nexus.tool.kind": getattr(getattr(tool, "kind", None), "value", getattr(tool, "kind", "")),
                    "nexus.tool.is_mutating": tool.is_mutating,
                    "nexus.tool.call_id": tool_call.call_id,
                    **_correlation_payload(context, tool_call_id=tool_call.call_id),
                },
            )
            if monitor is not None
            else None
        )
        try:
            with (span if span is not None else nullcontext()):
                result = await tool.execute(tool_call.call_id, tool_call.arguments, context)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            failed_payload = {
                "tool_name": tool.name,
                "tool_source": record.source,
                "tool_origin": record.origin,
                "arguments": tool_call.arguments,
                "call_id": tool_call.call_id,
                "session_id": context.session_id,
                "is_mutating": tool.is_mutating,
                "is_error": True,
                "duration_ms": duration_ms,
                "output": str(exc) or exc.__class__.__name__,
                "exception_type": exc.__class__.__name__,
                **_correlation_payload(context, tool_call_id=tool_call.call_id),
            }
            await self.hooks.emit(HookEvent.POST_TOOL_USE, failed_payload)
            capture_exception_from_hooks(self.hooks, exc, context=failed_payload)
            setattr(exc, "_nexus_sentry_captured", True)
            setattr(exc, "_nexus_error_area", "tool")
            raise
        if actor:
            result.metadata = {**result.metadata, "actor": actor}
        result.metadata = {**result.metadata, "is_mutating": tool.is_mutating}
        if tool.is_mutating:
            _clear_read_result_cache(context)
            result = _with_post_mutation_refresh(
                result,
                affected_paths or set(),
                context,
            )
        else:
            _store_read_result(record, tool, tool_call, context, result)
        if approval_manager is not None:
            approval_manager.consume_approval(tool.name, arguments=tool_call.arguments)

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

        yield AgentEvent.tool_call_complete(result)
        yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)

    async def _agentic_loop(
        self,
        history: list[Message],
        context: ToolExecutionContext,
        *,
        system_prompt: str,
        model_name: str,
        mode: ExecutionMode,
        approved_tools: set[str],
        approval_manager: ApprovalManager | None,
        auto_confirm: bool,
        auto_confirm_read_only: bool,
        temperature: float,
        max_output_tokens: int | None,
        max_turns: int,
        max_tool_calls_per_turn: int,
        parallel_tools: bool,
        parallel_tool_window: int,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Core agentic loop — processes stream events and executes tools."""

        tool_calls_executed = 0
        loop_detector = LoopDetector()
        unknown_tool_retries: dict[str, int] = {}
        invalid_argument_retries: dict[tuple[str, tuple[str, ...]], int] = {}

        for turn_index in range(max_turns):
            if turn_index > 0:
                yield AgentEvent.thinking_started(actor=_tool_actor(context))
            request = RuntimeRequest(
                model_name=model_name,
                system_prompt=system_prompt,
                messages=tuple(history),
                tool_schemas=_tool_schemas_for_context(self.tool_registry, context),
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )
            logger.debug(
                "agent.model_batch.start session_id=%s actor=%s turn_index=%s max_turns=%s model=%s messages=%s last_role=%s tool_schemas=%s",
                context.session_id,
                _tool_actor(context) or "supervisor",
                turn_index + 1,
                max_turns,
                model_name,
                len(request.messages),
                request.messages[-1].role if request.messages else "",
                len(request.tool_schemas),
            )

            # ----------------------------------------------------------------
            # Stream from the model client
            # ----------------------------------------------------------------
            response_text = ""
            stream_tool_calls: list[ToolCall] = []
            usage: UsageSnapshot | None = None
            stream_finish_reason: str | None = None
            model_call_id = uuid4().hex[:12]

            await self.hooks.emit(
                HookEvent.NOTIFICATION,
                {
                    "event": "model_start",
                    "session_id": context.session_id,
                    "model_call_id": model_call_id,
                    **_correlation_payload(context),
                    "provider": _provider_name_from_context(context),
                    "model": model_name,
                    "turn_index": turn_index + 1,
                    "actor": _tool_actor(context) or "supervisor",
                    "system_prompt": system_prompt,
                    "messages": [_serialize_message_for_observability(message) for message in request.messages],
                    "message_count": len(request.messages),
                    "tool_schema_count": len(request.tool_schemas),
                    "active_skills": list(context.metadata.get("active_skills", [])),
                    "prompt_name": getattr(context.metadata.get("config"), "langfuse_prompt_name", "nexus-system-prompt"),
                    "prompt_version": getattr(context.metadata.get("config"), "langfuse_prompt_version", ""),
                    "system_prompt_hash": _hash_prompt(system_prompt),
                    "system_prompt_chars": len(system_prompt),
                    "max_output_tokens": max_output_tokens,
                    "temperature": temperature,
                },
            )

            stream = cast(AsyncGenerator[StreamEvent, None], self.model_client.chat_completion(request, stream=True))
            monitor = sentry_monitor_from_hooks(self.hooks)
            span = (
                monitor.start_span(
                    op="gen_ai.chat",
                    name=model_name,
                    attributes={
                        "gen_ai.request.model": model_name,
                        "gen_ai.system": _provider_name_from_context(context),
                        "gen_ai.request.max_tokens": max_output_tokens,
                        "gen_ai.request.temperature": temperature,
                        "nexus.session_id": context.session_id,
                        **_correlation_payload(context),
                    },
                )
                if monitor is not None
                else None
            )
            with (span if span is not None else nullcontext()):
                async for stream_event in stream:
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
                        stream_finish_reason = stream_event.finish_reason or stream_finish_reason
                        if monitor is not None and usage is not None:
                            monitor.update_current_span(
                                attributes={
                                    "gen_ai.usage.input_tokens": usage.prompt_tokens,
                                    "gen_ai.usage.output_tokens": usage.completion_tokens,
                                    "gen_ai.usage.total_tokens": usage.total_tokens,
                                }
                            )

                    elif stream_event.type == StreamEventType.ERROR:
                        logger.warning(
                            "agent.model_batch.error session_id=%s actor=%s turn_index=%s error=%s",
                            context.session_id,
                            _tool_actor(context) or "supervisor",
                            turn_index + 1,
                            stream_event.error,
                        )
                        await self.hooks.emit(
                            HookEvent.NOTIFICATION,
                            {
                                "event": "model_error",
                                "session_id": context.session_id,
                                "model_call_id": model_call_id,
                                **_correlation_payload(context),
                                "provider": _provider_name_from_context(context),
                                "model": model_name,
                                "turn_index": turn_index + 1,
                                "actor": _tool_actor(context) or "supervisor",
                                "error": stream_event.error,
                            },
                        )
                        yield AgentEvent.agent_error(stream_event.error)
                        return

            if not response_text and not stream_tool_calls:
                # When the provider hits its output-token limit it returns an
                # empty response instead of an error.  Aggressively prune tool
                # outputs from history to free context space and retry the turn
                # rather than surfacing an opaque error to the user.
                if str(stream_finish_reason or "").strip().lower() in {"max_tokens", "length"}:
                    logger.warning(
                        "agent.model_batch.max_tokens session_id=%s actor=%s turn_index=%s — pruning history and retrying",
                        context.session_id,
                        _tool_actor(context) or "supervisor",
                        turn_index + 1,
                    )
                    prune_tool_outputs(history, protect_tokens=500, minimum_tokens=200)
                    continue

                empty_response_error = _empty_provider_response_message(stream_finish_reason)
                await self.hooks.emit(
                    HookEvent.NOTIFICATION,
                    {
                        "event": "model_error",
                        "session_id": context.session_id,
                        "model_call_id": model_call_id,
                        **_correlation_payload(context),
                        "provider": _provider_name_from_context(context),
                        "model": model_name,
                        "turn_index": turn_index + 1,
                        "actor": _tool_actor(context) or "supervisor",
                        "error": empty_response_error,
                    },
                )
                logger.warning(
                    "agent.model_batch.empty_response session_id=%s actor=%s turn_index=%s finish_reason=%s messages=%s last_role=%s",
                    context.session_id,
                    _tool_actor(context) or "supervisor",
                    turn_index + 1,
                    stream_finish_reason,
                    len(request.messages),
                    request.messages[-1].role if request.messages else "",
                )
                yield AgentEvent.agent_error(empty_response_error)
                return

            if response_text:
                yield AgentEvent.text_complete(response_text)
                loop_detector.record_action("response", text=response_text[:200])

            # Build the assistant message and RuntimeResponse for history.
            tool_calls: tuple[ToolCall, ...] = tuple(stream_tool_calls)
            message = Message(
                role="assistant",
                content=response_text or "",
                tool_calls=tool_calls,
            )
            should_record_message = bool(message.content or message.tool_calls)
            if should_record_message:
                history.append(message)
            logger.debug(
                "agent.model_batch.end session_id=%s actor=%s turn_index=%s response_chars=%s tool_calls=%s finish_reason=%s recorded=%s",
                context.session_id,
                _tool_actor(context) or "supervisor",
                turn_index + 1,
                len(response_text),
                len(tool_calls),
                "tool_calls" if tool_calls else (stream_finish_reason or "stop"),
                should_record_message,
            )

            runtime_response = RuntimeResponse(
                message=message,
                tool_calls=tool_calls,
                usage=usage,
                finish_reason="tool_calls" if tool_calls else (stream_finish_reason or "stop"),
            )

            await self.hooks.emit(
                HookEvent.NOTIFICATION,
                {
                    "event": "model_end",
                    "session_id": context.session_id,
                    "model_call_id": model_call_id,
                    **_correlation_payload(context),
                    "provider": _provider_name_from_context(context),
                    "model": model_name,
                    "turn_index": turn_index + 1,
                    "actor": _tool_actor(context) or "supervisor",
                    "finish_reason": runtime_response.finish_reason,
                    "tool_call_count": len(tool_calls),
                    "output": response_text,
                    "usage": {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                        "estimated_cost_usd": usage.estimated_cost_usd,
                        "provider": usage.provider,
                        "model": usage.model,
                    }
                    if usage is not None
                    else {},
                    "status": "completed",
                },
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
            if should_record_message:
                yield AgentEvent(kind=AgentEventType.MODEL_RESPONSE, payload=runtime_response)

            if not tool_calls:
                yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload=runtime_response.finish_reason)
                return

            # ----------------------------------------------------------------
            # Tool execution
            # ----------------------------------------------------------------
            if parallel_tools and _parallel_tool_execution_enabled(context):
                batch_state = _ToolBatchState()
                async for event in self._execute_parallel_first_tool_batch(
                    tool_calls,
                    context,
                    mode=mode,
                    approved_tools=approved_tools,
                    approval_manager=approval_manager,
                    auto_confirm=auto_confirm,
                    auto_confirm_read_only=auto_confirm_read_only,
                    max_tool_calls_per_turn=max_tool_calls_per_turn,
                    tool_calls_executed=tool_calls_executed,
                    parallel_tool_window=parallel_tool_window,
                    unknown_tool_retries=unknown_tool_retries,
                    invalid_argument_retries=invalid_argument_retries,
                    loop_detector=loop_detector,
                    batch_state=batch_state,
                ):
                    yield event
                tool_calls_executed += batch_state.executed_count
                for msg in batch_state.tool_result_messages:
                    history.append(msg)
                if batch_state.tool_result_messages:
                    logger.debug(
                        "agent.tool_results.appended session_id=%s actor=%s count=%s next_turn_index=%s",
                        context.session_id,
                        _tool_actor(context) or "supervisor",
                        len(batch_state.tool_result_messages),
                        turn_index + 2,
                    )
                prune_tool_outputs(history, protect_tokens=2000, minimum_tokens=500)
                loop_error = loop_detector.check_for_loop()
                if loop_error:
                    history.append(Message(
                        role="user",
                        content=f"[Loop detected] {loop_error} Try a different approach.",
                    ))
                if batch_state.stop_reason is not None:
                    return
                continue

            tool_result_messages: list[Message] = []
            mutating_paths_this_batch: set[str] = set()
            for tool_call in tool_calls:
                if tool_calls_executed >= max_tool_calls_per_turn:
                    pause_message = _tool_call_limit_pause_message(max_tool_calls_per_turn)
                    yield AgentEvent.text_complete(pause_message)
                    yield AgentEvent(
                        kind=AgentEventType.MODEL_RESPONSE,
                        payload=RuntimeResponse(
                            message=Message(role="assistant", content=pause_message),
                            finish_reason=TOOL_CALL_LIMIT_FINISH_REASON,
                        ),
                    )
                    yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload=TOOL_CALL_LIMIT_FINISH_REASON)
                    return

                try:
                    record = self.tool_registry.record(tool_call.tool_name)
                except LookupError:
                    tool_name = _tool_name_text(tool_call.tool_name)
                    retry_count = unknown_tool_retries.get(tool_name, 0) + 1
                    unknown_tool_retries[tool_name] = retry_count
                    unknown_result = _unknown_tool_result(
                        tool_call,
                        self.tool_registry,
                        retry_count=retry_count,
                        retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
                    )
                    tool_calls_executed += 1
                    tool_result_messages.append(_tool_result_message(unknown_result))
                    yield AgentEvent.tool_call_start(
                        tool_call.call_id,
                        tool_name,
                        tool_call.arguments,
                        actor=_tool_actor(context),
                        display=_tool_display_metadata(),
                    )
                    yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                    yield AgentEvent.tool_call_complete(unknown_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=unknown_result)
                    loop_detector.record_action("tool_call", tool_name=tool_name, result="unknown_tool")
                    if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                        for msg in tool_result_messages:
                            history.append(msg)
                        stop_message = _invalid_tool_call_stop_message(tool_name, "unknown tool name")
                        yield AgentEvent.text_complete(stop_message)
                        yield AgentEvent(
                            kind=AgentEventType.MODEL_RESPONSE,
                            payload=RuntimeResponse(
                                message=Message(role="assistant", content=stop_message),
                                finish_reason="invalid_tool_call",
                            ),
                        )
                        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="invalid_tool_call")
                        return
                    continue
                if not _tool_available_in_context(record, context):
                    tool_name = _tool_name_text(tool_call.tool_name)
                    retry_count = unknown_tool_retries.get(tool_name, 0) + 1
                    unknown_tool_retries[tool_name] = retry_count
                    unavailable_result = _tool_not_available_result(
                        tool_call,
                        self.tool_registry,
                        context,
                        retry_count=retry_count,
                        retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
                    )
                    tool_calls_executed += 1
                    tool_result_messages.append(_tool_result_message(unavailable_result))
                    yield AgentEvent.tool_call_start(
                        tool_call.call_id,
                        tool_name,
                        tool_call.arguments,
                        actor=_tool_actor(context),
                        display=_tool_display_metadata(),
                    )
                    yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                    yield AgentEvent.tool_call_complete(unavailable_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=unavailable_result)
                    loop_detector.record_action("tool_call", tool_name=tool_name, result="unavailable_tool")
                    if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                        for msg in tool_result_messages:
                            history.append(msg)
                        stop_message = _invalid_tool_call_stop_message(tool_name, "tool unavailable in this context")
                        yield AgentEvent.text_complete(stop_message)
                        yield AgentEvent(
                            kind=AgentEventType.MODEL_RESPONSE,
                            payload=RuntimeResponse(
                                message=Message(role="assistant", content=stop_message),
                                finish_reason="invalid_tool_call",
                            ),
                        )
                        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="invalid_tool_call")
                        return
                    continue

                tool = record.tool
                missing_fields = _missing_required_fields(tool.input_schema, tool_call.arguments)
                if missing_fields:
                    if _missing_fields_should_be_repaired_by_model(missing_fields):
                        retry_key = (tool.name, tuple(missing_fields))
                        retry_count = invalid_argument_retries.get(retry_key, 0) + 1
                        invalid_argument_retries[retry_key] = retry_count
                        invalid_result = _missing_required_argument_result(
                            tool_call,
                            tool.name,
                            missing_fields,
                            retry_count=retry_count,
                            retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
                        )
                        tool_calls_executed += 1
                        tool_result_messages.append(_tool_result_message(invalid_result))
                        yield AgentEvent.tool_call_start(
                            tool_call.call_id,
                            tool.name,
                            tool_call.arguments,
                            actor=_tool_actor(context),
                            display=_tool_display_metadata(is_mutating=tool.is_mutating),
                        )
                        yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=tool_call)
                        yield AgentEvent.tool_call_complete(invalid_result)
                        yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=invalid_result)
                        loop_detector.record_action("tool_call", tool_name=tool.name, result="missing_argument")
                        if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                            for msg in tool_result_messages:
                                history.append(msg)
                            stop_message = _invalid_tool_call_stop_message(tool.name, "missing required arguments")
                            yield AgentEvent.text_complete(stop_message)
                            yield AgentEvent(
                                kind=AgentEventType.MODEL_RESPONSE,
                                payload=RuntimeResponse(
                                    message=Message(role="assistant", content=stop_message),
                                    finish_reason="invalid_tool_call",
                                ),
                            )
                            yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="invalid_tool_call")
                            return
                        continue

                    missing_field = missing_fields[0]
                    confirmation_preview: dict[str, Any] = {}
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
                    clarification_request = ConfirmationRequest(
                        kind=ConfirmationKind.CLARIFICATION,
                        tool_name=tool.name,
                        prompt=f"Provide a value for '{missing_field}' before running '{tool.name}'.",
                        reason="Required tool argument is missing.",
                        call_id=tool_call.call_id,
                        payload={"tool_name": tool.name, "field": missing_field, "actor": _tool_actor(context)},
                        arguments=tool_call.arguments,
                        preview=confirmation_preview,
                    )
                    yield AgentEvent(
                        kind=AgentEventType.CONFIRMATION_REQUESTED,
                        payload=clarification_request,
                    )
                    return

                confirmation = await _get_tool_confirmation(tool, tool_call.call_id, tool_call.arguments, context)
                confirmation_preview = _confirmation_preview(confirmation)
                affected_paths = _affected_file_paths(tool, tool_call.arguments, confirmation, context)

                decision = self.permission_checker.evaluate(
                    tool,
                    tool_call.arguments,
                    mode,
                    context=context,
                    auto_confirm_read_only=auto_confirm_read_only,
                )

                if decision.decision is PermissionDecision.DENY:
                    denied_result = _denied_tool_result(
                        tool_call.call_id,
                        tool.name,
                        decision.reason,
                        risk_level=_risk_level_name(decision.risk_level),
                    )
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
                    tool_calls_executed += 1
                    tool_result_messages.append(_tool_result_message(denied_result))
                    yield AgentEvent(kind=AgentEventType.TOOL_DENIED, payload=decision)
                    yield AgentEvent.tool_call_complete(denied_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=denied_result)
                    loop_detector.record_action("tool_call", tool_name=tool_call.tool_name, result="denied")
                    continue

                if approval_manager is not None and approval_manager.is_refused(tool.name, tool_call.arguments):
                    refused_reason = "User previously denied this tool call in the current turn. Continue without running it."
                    refused_result = _denied_tool_result(
                        tool_call.call_id,
                        tool.name,
                        refused_reason,
                        risk_level=_risk_level_name(decision.risk_level),
                    )
                    tool_calls_executed += 1
                    tool_result_messages.append(_tool_result_message(refused_result))
                    yield AgentEvent.tool_call_complete(refused_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=refused_result)
                    loop_detector.record_action("tool_call", tool_name=tool_call.tool_name, result="refused")
                    continue

                duplicate_paths = affected_paths & mutating_paths_this_batch if tool.is_mutating else set()
                if duplicate_paths:
                    blocked_result = _same_file_mutation_result(tool_call, duplicate_paths)
                    tool_calls_executed += 1
                    tool_result_messages.append(_tool_result_message(blocked_result))
                    yield AgentEvent.tool_call_complete(blocked_result)
                    yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=blocked_result)
                    loop_detector.record_action("tool_call", tool_name=tool_call.tool_name, result="same_file_blocked")
                    continue

                if (
                    decision.decision is PermissionDecision.CONFIRM
                    and not auto_confirm
                    and not _is_tool_preapproved(
                        tool.name,
                        tool_call.arguments,
                        is_mutating=tool.is_mutating,
                        risk_level=_risk_level_name(decision.risk_level),
                        approved_tools=approved_tools,
                        approval_manager=approval_manager,
                    )
                ):
                    conf_request = ConfirmationRequest(
                        kind=ConfirmationKind.APPROVAL,
                        tool_name=tool.name,
                        prompt=f"Allow tool '{tool.name}'?",
                        reason=decision.reason,
                        call_id=tool_call.call_id,
                        payload={
                            "tool_name": tool.name,
                            "reason": decision.reason,
                            "approval_policy": str(context.metadata.get("approval_policy", "on-request")),
                            "risk_level": _risk_level_name(decision.risk_level),
                            "actor": _tool_actor(context),
                        },
                        arguments=tool_call.arguments,
                        preview=confirmation_preview,
                    )
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
                    yield AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=conf_request)
                    return

                if tool.is_mutating:
                    mutating_paths_this_batch.update(affected_paths)
                result: ToolResult | None = None
                async for event in self._execute_tool_call(
                    record,
                    tool,
                    tool_call,
                    context,
                    approval_manager=approval_manager,
                    confirmation_preview=confirmation_preview,
                    affected_paths=affected_paths,
                ):
                    if event.kind == AgentEventType.TOOL_RESULT:
                        result = event.payload
                    yield event
                if result is None:
                    logger.warning(
                        "agent.tool_result.missing session_id=%s actor=%s tool_name=%s call_id=%s",
                        context.session_id,
                        _tool_actor(context) or "supervisor",
                        tool_call.tool_name,
                        tool_call.call_id,
                    )
                    continue
                tool_calls_executed += 1
                tool_result_messages.append(_tool_result_message(result))
                logger.debug(
                    "agent.tool_result.recorded session_id=%s actor=%s tool_name=%s call_id=%s is_error=%s output_chars=%s",
                    context.session_id,
                    _tool_actor(context) or "supervisor",
                    result.tool_name,
                    result.call_id,
                    result.is_error,
                    len(result.output),
                )
                loop_detector.record_action("tool_call", tool_name=tool_call.tool_name)

            # Add all tool results to history after the complete batch, then prune
            # and check for loops — matching the reference-code execution model.
            for msg in tool_result_messages:
                history.append(msg)
            if tool_result_messages:
                logger.debug(
                    "agent.tool_results.appended session_id=%s actor=%s count=%s next_turn_index=%s",
                    context.session_id,
                    _tool_actor(context) or "supervisor",
                    len(tool_result_messages),
                    turn_index + 2,
                )
            prune_tool_outputs(history, protect_tokens=2000, minimum_tokens=500)
            loop_error = loop_detector.check_for_loop()
            if loop_error:
                history.append(Message(
                    role="user",
                    content=f"[Loop detected] {loop_error} Try a different approach.",
                ))

        pause_message = _max_turns_pause_message(max_turns)
        yield AgentEvent.text_complete(pause_message)
        yield AgentEvent(
            kind=AgentEventType.MODEL_RESPONSE,
            payload=RuntimeResponse(
                message=Message(role="assistant", content=pause_message),
                finish_reason=MAX_TURNS_FINISH_REASON,
            ),
        )
        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload=MAX_TURNS_FINISH_REASON)

    async def _run_tool_call(
        self,
        record: Any,
        tool: Any,
        tool_call: ToolCall,
        context: ToolExecutionContext,
        *,
        approval_manager: ApprovalManager | None,
        affected_paths: set[str] | None = None,
    ) -> ToolResult:
        cached_result = _cached_read_result(record, tool, tool_call, context)
        if cached_result is not None:
            return cached_result

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
        monitor = sentry_monitor_from_hooks(self.hooks)
        span = (
            monitor.start_span(
                op="nexus.tool",
                name=tool.name,
                attributes={
                    "nexus.tool.name": tool.name,
                    "nexus.tool.source": record.source,
                    "nexus.tool.origin": record.origin,
                    "nexus.tool.kind": getattr(getattr(tool, "kind", None), "value", getattr(tool, "kind", "")),
                    "nexus.tool.is_mutating": tool.is_mutating,
                    "nexus.tool.call_id": tool_call.call_id,
                    **_correlation_payload(context, tool_call_id=tool_call.call_id),
                },
            )
            if monitor is not None
            else None
        )
        try:
            with (span if span is not None else nullcontext()):
                result = await tool.execute(tool_call.call_id, tool_call.arguments, context)
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            failed_payload = {
                "tool_name": tool.name,
                "tool_source": record.source,
                "tool_origin": record.origin,
                "arguments": tool_call.arguments,
                "call_id": tool_call.call_id,
                "session_id": context.session_id,
                "is_mutating": tool.is_mutating,
                "is_error": True,
                "duration_ms": duration_ms,
                "output": str(exc) or exc.__class__.__name__,
                "exception_type": exc.__class__.__name__,
                **_correlation_payload(context, tool_call_id=tool_call.call_id),
            }
            await self.hooks.emit(HookEvent.POST_TOOL_USE, failed_payload)
            capture_exception_from_hooks(self.hooks, exc, context=failed_payload)
            setattr(exc, "_nexus_sentry_captured", True)
            setattr(exc, "_nexus_error_area", "tool")
            raise

        actor = _tool_actor(context)
        if actor:
            result.metadata = {**result.metadata, "actor": actor}
        if tool.is_mutating:
            _clear_read_result_cache(context)
            result = _with_post_mutation_refresh(
                result,
                affected_paths or set(),
                context,
            )
        else:
            _store_read_result(record, tool, tool_call, context, result)
        if approval_manager is not None:
            approval_manager.consume_approval(tool.name, arguments=tool_call.arguments)

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
        return result

    async def _execute_parallel_first_tool_batch(
        self,
        tool_calls: tuple[ToolCall, ...],
        context: ToolExecutionContext,
        *,
        mode: ExecutionMode,
        approved_tools: set[str],
        approval_manager: ApprovalManager | None,
        auto_confirm: bool,
        auto_confirm_read_only: bool,
        max_tool_calls_per_turn: int,
        tool_calls_executed: int,
        parallel_tool_window: int,
        unknown_tool_retries: dict[str, int],
        invalid_argument_retries: dict[tuple[str, tuple[str, ...]], int],
        loop_detector: LoopDetector,
        batch_state: _ToolBatchState,
    ) -> AsyncGenerator[AgentEvent, None]:
        remaining_budget = max_tool_calls_per_turn - tool_calls_executed
        if remaining_budget <= 0:
            pause_message = _tool_call_limit_pause_message(max_tool_calls_per_turn)
            yield AgentEvent.text_complete(pause_message)
            yield AgentEvent(
                kind=AgentEventType.MODEL_RESPONSE,
                payload=RuntimeResponse(
                    message=Message(role="assistant", content=pause_message),
                    finish_reason=TOOL_CALL_LIMIT_FINISH_REASON,
                ),
            )
            yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload=TOOL_CALL_LIMIT_FINISH_REASON)
            batch_state.stop_reason = TOOL_CALL_LIMIT_FINISH_REASON
            return

        limited_tool_calls = tuple(tool_calls[:remaining_budget])
        hit_limit = len(limited_tool_calls) < len(tool_calls)
        parallel_items: list[_PreparedToolCall] = []
        sequential_items: list[_PreparedToolCall] = []
        mutating_paths_this_batch: set[str] = set()

        for tool_call in limited_tool_calls:
            prepared = await self._prepare_parallel_first_tool_call(
                tool_call,
                context,
                mode=mode,
                approved_tools=approved_tools,
                approval_manager=approval_manager,
                auto_confirm=auto_confirm,
                auto_confirm_read_only=auto_confirm_read_only,
                unknown_tool_retries=unknown_tool_retries,
                invalid_argument_retries=invalid_argument_retries,
                mutating_paths_this_batch=mutating_paths_this_batch,
            )
            target_list = parallel_items if _prepared_tool_prefers_parallel(prepared, context) else sequential_items
            target_list.append(prepared)
            if prepared.tool is not None and prepared.tool.is_mutating:
                mutating_paths_this_batch.update(prepared.affected_paths)

        window_size = max(1, min(int(parallel_tool_window or 1), 8))
        for start in range(0, len(parallel_items), window_size):
            window = parallel_items[start:start + window_size]
            pending_confirmation: _PreparedToolCall | None = None
            task_map: dict[str, asyncio.Task[ToolResult]] = {}
            runnable_parallel = [
                prepared
                for prepared in window
                if prepared.record is not None and prepared.tool is not None and prepared.immediate_result is None
            ]
            for parallel_index, prepared in enumerate(runnable_parallel):
                yield AgentEvent.tool_call_start(
                    prepared.tool_call.call_id,
                    prepared.tool_call.tool_name,
                    prepared.tool_call.arguments,
                    preview=prepared.confirmation_preview,
                    actor=_tool_actor(context),
                    display=_tool_display_metadata(
                        is_mutating=prepared.tool.is_mutating,
                        parallel_group_size=len(runnable_parallel),
                        parallel_index=parallel_index,
                    ),
                )
                yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=prepared.tool_call)
                task_map[prepared.tool_call.call_id] = asyncio.create_task(
                    self._run_tool_call(
                        prepared.record,
                        prepared.tool,
                        prepared.tool_call,
                        context,
                        approval_manager=approval_manager,
                        affected_paths=prepared.affected_paths,
                    )
                )

            if task_map:
                results = await asyncio.gather(*task_map.values())
                result_by_call_id = dict(zip(task_map, results, strict=False))
            else:
                result_by_call_id = {}

            for prepared in window:
                if prepared.immediate_result is not None:
                    async for event in self._emit_prepared_tool_result(prepared, context, loop_detector, batch_state):
                        yield event
                    if prepared.stop_message is not None:
                        yield AgentEvent.text_complete(prepared.stop_message)
                        yield AgentEvent(
                            kind=AgentEventType.MODEL_RESPONSE,
                            payload=RuntimeResponse(
                                message=Message(role="assistant", content=prepared.stop_message),
                                finish_reason="invalid_tool_call",
                            ),
                        )
                        yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="invalid_tool_call")
                        batch_state.stop_reason = "invalid_tool_call"
                        return
                    continue
                if prepared.confirmation_request is not None:
                    pending_confirmation = pending_confirmation or prepared
                    continue
                result = result_by_call_id.get(prepared.tool_call.call_id)
                if result is None:
                    logger.warning(
                        "agent.tool_result.missing session_id=%s actor=%s tool_name=%s call_id=%s",
                        context.session_id,
                        _tool_actor(context) or "supervisor",
                        prepared.tool_call.tool_name,
                        prepared.tool_call.call_id,
                    )
                    continue
                batch_state.executed_count += 1
                batch_state.tool_result_messages.append(_tool_result_message(result))
                logger.debug(
                    "agent.tool_result.recorded session_id=%s actor=%s tool_name=%s call_id=%s is_error=%s output_chars=%s",
                    context.session_id,
                    _tool_actor(context) or "supervisor",
                    result.tool_name,
                    result.call_id,
                    result.is_error,
                    len(result.output),
                )
                loop_detector.record_action("tool_call", tool_name=prepared.tool_call.tool_name)
                yield AgentEvent.tool_call_complete(result)
                yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)

            if pending_confirmation is not None and pending_confirmation.confirmation_request is not None:
                await self._emit_confirmation_requested_notification(pending_confirmation.confirmation_request, pending_confirmation, context)
                yield AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=pending_confirmation.confirmation_request)
                batch_state.stop_reason = "confirmation_requested"
                return

        for prepared in sequential_items:
            if prepared.immediate_result is not None:
                async for event in self._emit_prepared_tool_result(prepared, context, loop_detector, batch_state):
                    yield event
                if prepared.stop_message is not None:
                    yield AgentEvent.text_complete(prepared.stop_message)
                    yield AgentEvent(
                        kind=AgentEventType.MODEL_RESPONSE,
                        payload=RuntimeResponse(
                            message=Message(role="assistant", content=prepared.stop_message),
                            finish_reason="invalid_tool_call",
                        ),
                    )
                    yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="invalid_tool_call")
                    batch_state.stop_reason = "invalid_tool_call"
                    return
                continue
            if prepared.confirmation_request is not None:
                await self._emit_confirmation_requested_notification(prepared.confirmation_request, prepared, context)
                yield AgentEvent(kind=AgentEventType.CONFIRMATION_REQUESTED, payload=prepared.confirmation_request)
                batch_state.stop_reason = "confirmation_requested"
                return
            if prepared.record is None or prepared.tool is None:
                continue
            result: ToolResult | None = None
            async for event in self._execute_tool_call(
                prepared.record,
                prepared.tool,
                prepared.tool_call,
                context,
                approval_manager=approval_manager,
                confirmation_preview=prepared.confirmation_preview,
                affected_paths=prepared.affected_paths,
            ):
                if event.kind == AgentEventType.TOOL_RESULT:
                    result = event.payload
                yield event
            if result is None:
                logger.warning(
                    "agent.tool_result.missing session_id=%s actor=%s tool_name=%s call_id=%s",
                    context.session_id,
                    _tool_actor(context) or "supervisor",
                    prepared.tool_call.tool_name,
                    prepared.tool_call.call_id,
                )
                continue
            batch_state.executed_count += 1
            batch_state.tool_result_messages.append(_tool_result_message(result))
            logger.debug(
                "agent.tool_result.recorded session_id=%s actor=%s tool_name=%s call_id=%s is_error=%s output_chars=%s",
                context.session_id,
                _tool_actor(context) or "supervisor",
                result.tool_name,
                result.call_id,
                result.is_error,
                len(result.output),
            )
            loop_detector.record_action("tool_call", tool_name=prepared.tool_call.tool_name)

        if hit_limit:
            pause_message = _tool_call_limit_pause_message(max_tool_calls_per_turn)
            yield AgentEvent.text_complete(pause_message)
            yield AgentEvent(
                kind=AgentEventType.MODEL_RESPONSE,
                payload=RuntimeResponse(
                    message=Message(role="assistant", content=pause_message),
                    finish_reason=TOOL_CALL_LIMIT_FINISH_REASON,
                ),
            )
            yield AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload=TOOL_CALL_LIMIT_FINISH_REASON)
            batch_state.stop_reason = TOOL_CALL_LIMIT_FINISH_REASON

    async def _prepare_parallel_first_tool_call(
        self,
        tool_call: ToolCall,
        context: ToolExecutionContext,
        *,
        mode: ExecutionMode,
        approved_tools: set[str],
        approval_manager: ApprovalManager | None,
        auto_confirm: bool,
        auto_confirm_read_only: bool,
        unknown_tool_retries: dict[str, int],
        invalid_argument_retries: dict[tuple[str, tuple[str, ...]], int],
        mutating_paths_this_batch: set[str],
    ) -> _PreparedToolCall:
        prepared = _PreparedToolCall(tool_call=tool_call)
        try:
            record = self.tool_registry.record(tool_call.tool_name)
        except LookupError:
            tool_name = _tool_name_text(tool_call.tool_name)
            retry_count = unknown_tool_retries.get(tool_name, 0) + 1
            unknown_tool_retries[tool_name] = retry_count
            prepared.immediate_result = _unknown_tool_result(
                tool_call,
                self.tool_registry,
                retry_count=retry_count,
                retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
            )
            prepared.emit_start_event = True
            prepared.loop_result = "unknown_tool"
            if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                prepared.stop_message = _invalid_tool_call_stop_message(tool_name, "unknown tool name")
            return prepared

        prepared.record = record
        prepared.tool = record.tool
        if not _tool_available_in_context(record, context):
            tool_name = _tool_name_text(tool_call.tool_name)
            retry_count = unknown_tool_retries.get(tool_name, 0) + 1
            unknown_tool_retries[tool_name] = retry_count
            prepared.immediate_result = _tool_not_available_result(
                tool_call,
                self.tool_registry,
                context,
                retry_count=retry_count,
                retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
            )
            prepared.emit_start_event = True
            prepared.loop_result = "unavailable_tool"
            if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                prepared.stop_message = _invalid_tool_call_stop_message(tool_name, "tool unavailable in this context")
            return prepared

        missing_fields = _missing_required_fields(record.tool.input_schema, tool_call.arguments)
        if missing_fields:
            read_without_clarification_callback = (
                getattr(record.tool, "kind", None) is ToolKind.READ
                and context.metadata.get("approval_callback") is None
            )
            if not _missing_fields_should_be_repaired_by_model(missing_fields) and not read_without_clarification_callback:
                missing_field = missing_fields[0]
                prepared.confirmation_request = ConfirmationRequest(
                    kind=ConfirmationKind.CLARIFICATION,
                    tool_name=record.tool.name,
                    prompt=f"Provide a value for '{missing_field}' before running '{record.tool.name}'.",
                    reason="Required tool argument is missing.",
                    call_id=tool_call.call_id,
                    payload={"tool_name": record.tool.name, "field": missing_field, "actor": _tool_actor(context)},
                    arguments=tool_call.arguments,
                    preview={},
                )
                return prepared
            retry_key = (record.tool.name, tuple(missing_fields))
            retry_count = invalid_argument_retries.get(retry_key, 0) + 1
            invalid_argument_retries[retry_key] = retry_count
            prepared.immediate_result = _missing_required_argument_result(
                tool_call,
                record.tool.name,
                missing_fields,
                retry_count=retry_count,
                retry_limit=INVALID_TOOL_CALL_RETRY_LIMIT,
            )
            prepared.emit_start_event = True
            prepared.loop_result = "missing_argument"
            if retry_count > INVALID_TOOL_CALL_RETRY_LIMIT:
                prepared.stop_message = _invalid_tool_call_stop_message(record.tool.name, "missing required arguments")
            return prepared

        confirmation = await _get_tool_confirmation(record.tool, tool_call.call_id, tool_call.arguments, context)
        prepared.confirmation_preview = _confirmation_preview(confirmation)
        prepared.affected_paths = _affected_file_paths(record.tool, tool_call.arguments, confirmation, context)

        decision = self.permission_checker.evaluate(
            record.tool,
            tool_call.arguments,
            mode,
            context=context,
            auto_confirm_read_only=auto_confirm_read_only,
        )
        risk_level = _risk_level_name(decision.risk_level)
        prepared.permission_decision = decision

        if decision.decision is PermissionDecision.DENY:
            denied_result = _denied_tool_result(
                tool_call.call_id,
                record.tool.name,
                decision.reason,
                risk_level=risk_level,
            )
            await self.hooks.emit(
                HookEvent.NOTIFICATION,
                {
                    "event": "tool_denied",
                    "tool_name": record.tool.name,
                    "tool_source": record.source,
                    "tool_origin": record.origin,
                    "arguments": tool_call.arguments,
                    "reason": decision.reason,
                    "session_id": context.session_id,
                    "call_id": tool_call.call_id,
                    **_correlation_payload(context, tool_call_id=tool_call.call_id),
                },
            )
            prepared.immediate_result = denied_result
            prepared.loop_result = "denied"
            return prepared

        if approval_manager is not None and approval_manager.is_refused(record.tool.name, tool_call.arguments):
            refused_reason = "User previously denied this tool call in the current turn. Continue without running it."
            prepared.immediate_result = _denied_tool_result(
                tool_call.call_id,
                record.tool.name,
                refused_reason,
                risk_level=risk_level,
            )
            prepared.loop_result = "refused"
            return prepared

        duplicate_paths = prepared.affected_paths & mutating_paths_this_batch if record.tool.is_mutating else set()
        if duplicate_paths:
            prepared.immediate_result = _same_file_mutation_result(tool_call, duplicate_paths)
            prepared.loop_result = "same_file_blocked"
            return prepared

        if (
            decision.decision is PermissionDecision.CONFIRM
            and not auto_confirm
            and not _is_tool_preapproved(
                record.tool.name,
                tool_call.arguments,
                is_mutating=record.tool.is_mutating,
                risk_level=risk_level,
                approved_tools=approved_tools,
                approval_manager=approval_manager,
            )
        ):
            prepared.confirmation_request = ConfirmationRequest(
                kind=ConfirmationKind.APPROVAL,
                tool_name=record.tool.name,
                prompt=f"Allow tool '{record.tool.name}'?",
                reason=decision.reason,
                call_id=tool_call.call_id,
                payload={
                    "tool_name": record.tool.name,
                    "reason": decision.reason,
                    "approval_policy": str(context.metadata.get("approval_policy", "on-request")),
                    "risk_level": risk_level,
                    "actor": _tool_actor(context),
                },
                arguments=tool_call.arguments,
                preview=prepared.confirmation_preview,
            )
        return prepared

    async def _emit_prepared_tool_result(
        self,
        prepared: _PreparedToolCall,
        context: ToolExecutionContext,
        loop_detector: LoopDetector,
        batch_state: _ToolBatchState,
    ) -> AsyncGenerator[AgentEvent, None]:
        result = prepared.immediate_result
        if result is None:
            return
        if prepared.emit_start_event:
            yield AgentEvent.tool_call_start(
                prepared.tool_call.call_id,
                _tool_name_text(prepared.tool_call.tool_name),
                prepared.tool_call.arguments,
                actor=_tool_actor(context),
                display=_tool_display_metadata(
                    is_mutating=bool(getattr(prepared.tool, "is_mutating", False)) if prepared.tool is not None else None,
                ),
            )
            yield AgentEvent(kind=AgentEventType.TOOL_CALL_REQUESTED, payload=prepared.tool_call)
        if prepared.loop_result == "denied" and prepared.permission_decision is not None:
            yield AgentEvent(kind=AgentEventType.TOOL_DENIED, payload=prepared.permission_decision)
        yield AgentEvent.tool_call_complete(result)
        yield AgentEvent(kind=AgentEventType.TOOL_RESULT, payload=result)
        batch_state.executed_count += 1
        batch_state.tool_result_messages.append(_tool_result_message(result))
        loop_detector.record_action(
            "tool_call",
            tool_name=_tool_name_text(prepared.tool_call.tool_name),
            result=prepared.loop_result or "error",
        )

    async def _emit_confirmation_requested_notification(
        self,
        request: ConfirmationRequest,
        prepared: _PreparedToolCall,
        context: ToolExecutionContext,
    ) -> None:
        await self.hooks.emit(
            HookEvent.NOTIFICATION,
            {
                "event": "confirmation_requested",
                "tool_name": request.tool_name,
                "tool_source": prepared.record.source if prepared.record is not None else "unknown",
                "tool_origin": prepared.record.origin if prepared.record is not None else None,
                "arguments": request.arguments,
                "reason": request.reason,
                "session_id": context.session_id,
                "call_id": request.call_id,
                **_correlation_payload(context, tool_call_id=request.call_id),
            },
        )


def _parallel_tool_execution_enabled(context: ToolExecutionContext) -> bool:
    del context
    return True


def _tool_display_metadata(
    *,
    is_mutating: bool | None = None,
    parallel_group_size: int | None = None,
    parallel_index: int | None = None,
) -> dict[str, Any]:
    display: dict[str, Any] = {}
    if is_mutating is not None:
        display["is_mutating"] = is_mutating
    if parallel_group_size is not None and parallel_group_size > 1:
        display["parallel_group_size"] = parallel_group_size
    if parallel_index is not None and parallel_group_size is not None and parallel_group_size > 1:
        display["parallel_index"] = parallel_index
    return display


def _prepared_tool_prefers_parallel(prepared: _PreparedToolCall, context: ToolExecutionContext) -> bool:
    if prepared.record is None:
        return False
    return _tool_call_can_run_in_parallel(prepared.record, context)


def _tool_call_can_run_in_parallel(record: Any, context: ToolExecutionContext) -> bool:
    if not _parallel_tool_execution_enabled(context):
        return False
    tool = getattr(record, "tool", None)
    if tool is None or getattr(tool, "is_mutating", False):
        return False
    name = str(getattr(record, "name", ""))
    if name == "delegate_task" or name.startswith("subagent_"):
        return False
    return getattr(tool, "kind", None) is not ToolKind.AGENT


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


def _tool_result_message(result: ToolResult) -> Message:
    return Message(
        role="tool",
        content=result.output,
        name=result.tool_name,
        tool_call_id=result.call_id,
    )


def _cached_read_result(
    record: Any,
    tool: Any,
    tool_call: ToolCall,
    context: ToolExecutionContext,
) -> ToolResult | None:
    key = _read_result_cache_key(record, tool, tool_call)
    if key is None:
        return None
    cache = context.metadata.get(_READ_RESULT_CACHE_METADATA_KEY)
    if not isinstance(cache, dict):
        return None
    cached = cache.get(key)
    if not isinstance(cached, ToolResult):
        return None
    metadata = {
        **cached.metadata,
        "read_cache_hit": True,
        "cached_from_call_id": cached.call_id,
    }
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=cached.tool_name,
        output=cached.output,
        is_error=cached.is_error,
        metadata=metadata,
    )


def _store_read_result(
    record: Any,
    tool: Any,
    tool_call: ToolCall,
    context: ToolExecutionContext,
    result: ToolResult,
) -> None:
    key = _read_result_cache_key(record, tool, tool_call)
    if key is None:
        return
    cache = context.metadata.setdefault(_READ_RESULT_CACHE_METADATA_KEY, {})
    if isinstance(cache, dict):
        cache[key] = ToolResult(
            call_id=result.call_id,
            tool_name=result.tool_name,
            output=result.output,
            is_error=result.is_error,
            metadata={**result.metadata, "read_cache_hit": False},
        )


def _clear_read_result_cache(context: ToolExecutionContext) -> None:
    cache = context.metadata.get(_READ_RESULT_CACHE_METADATA_KEY)
    if isinstance(cache, dict):
        cache.clear()


def _read_result_cache_key(record: Any, tool: Any, tool_call: ToolCall) -> str | None:
    if getattr(tool, "is_mutating", False):
        return None
    if getattr(tool, "kind", None) is not ToolKind.READ:
        return None
    name = str(getattr(record, "name", "") or getattr(tool, "name", ""))
    if name not in _CACHEABLE_READ_TOOL_NAMES:
        return None
    try:
        arguments = json.dumps(tool_call.arguments, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        arguments = repr(sorted(tool_call.arguments.items()))
    return f"{name}:{arguments}"


def _tool_name_text(tool_name: Any) -> str:
    if isinstance(tool_name, str):
        return tool_name or "(empty)"
    return repr(tool_name)


def _empty_provider_response_message(finish_reason: str | None) -> str:
    reason = str(finish_reason or "").strip()
    if reason:
        return (
            "Provider returned an empty assistant response with no tool calls "
            f"(finish_reason={reason})."
        )
    return "Provider returned an empty assistant response with no tool calls."


def _unknown_tool_result(
    tool_call: ToolCall,
    registry: ToolRegistry,
    *,
    retry_count: int,
    retry_limit: int,
) -> ToolResult:
    tool_name = _tool_name_text(tool_call.tool_name)
    available = ", ".join(record.name for record in registry.records()) or "(no tools registered)"
    if retry_count > retry_limit:
        guidance = (
            f"Retry limit exceeded after {retry_limit} repair attempts. Stop using this hallucinated tool name."
        )
    else:
        guidance = (
            "Retry with one of the available tool names from the current tool schema. "
            f"Repair attempt {retry_count} of {retry_limit}."
        )
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=tool_name,
        output=(
            f"Unknown tool name: {tool_name}. This tool is not registered in the current context. "
            f"Available tools: {available}. {guidance}"
        ),
        is_error=True,
        metadata={
            "unknown_tool": True,
            "tool_name": tool_name,
            "retry_count": retry_count,
            "retry_limit": retry_limit,
            "available_tools": [record.name for record in registry.records()],
        },
    )


def _tool_not_available_result(
    tool_call: ToolCall,
    registry: ToolRegistry,
    context: ToolExecutionContext,
    *,
    retry_count: int = 1,
    retry_limit: int = INVALID_TOOL_CALL_RETRY_LIMIT,
) -> ToolResult:
    tool_name = _tool_name_text(tool_call.tool_name)
    scoped_available = context.metadata.get("supervisor_available_tools")
    if isinstance(scoped_available, list):
        available_names = [str(name) for name in scoped_available if str(name).strip()]
    else:
        available_names = [record.name for record in registry.records() if record.name.startswith("subagent_")]
    available = ", ".join(
        available_names
    ) or "(none)"
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=tool_name,
        output=(
            f"Tool '{tool_name}' is not available to the supervisor in this context. "
            f"Use an available supervisor tool or update the resource allowlist first. Available supervisor tools: {available}."
        ),
        is_error=True,
        metadata={
            "tool_unavailable": True,
            "tool_name": tool_name,
            "retry_count": retry_count,
            "retry_limit": retry_limit,
            "available_tools": available_names,
        },
    )


def _missing_fields_should_be_repaired_by_model(missing_fields: list[str]) -> bool:
    model_owned_fields = {
        "content",
        "new_content",
        "old_content",
        "new_string",
        "old_string",
        "code",
        "patch",
    }
    return any(field in model_owned_fields for field in missing_fields)


def _missing_required_argument_result(
    tool_call: ToolCall,
    tool_name: str,
    missing_fields: list[str],
    *,
    retry_count: int,
    retry_limit: int,
) -> ToolResult:
    fields_text = ", ".join(f"'{field}'" for field in missing_fields)
    supplied_keys = ", ".join(sorted(str(key) for key in tool_call.arguments)) or "(none)"
    alias_hints: list[str] = []
    if "content" in missing_fields and "text" in tool_call.arguments:
        alias_hints.append("You supplied 'text'; use 'content' for the file body instead.")
    if tool_name == "edit":
        if "old_string" in missing_fields and "old_text" in tool_call.arguments:
            alias_hints.append("You supplied 'old_text'; use 'old_string' for the exact text to replace.")
        if "new_string" in missing_fields and "new_text" in tool_call.arguments:
            alias_hints.append("You supplied 'new_text'; use 'new_string' for the replacement text.")
        if any(field in missing_fields for field in {"old_string", "new_string"}):
            alias_hints.append(
                "The edit tool requires 'path', 'old_string', and 'new_string'. old_string must be the current snippet from disk, preferably with a few surrounding lines so the match is unique. Use write_file only for new files or true full-file rewrites."
            )
    alias_hint = f" {' '.join(alias_hints)}" if alias_hints else ""
    if retry_count > retry_limit:
        guidance = (
            f"Retry limit exceeded after {retry_limit} repair attempts. Stop and explain the blocker."
        )
    else:
        guidance = (
            "Retry the tool call with valid arguments generated from the task context. "
            f"Repair attempt {retry_count} of {retry_limit}."
        )
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=tool_name,
        output=(
            f"Missing required argument(s) for tool '{tool_name}': {fields_text}. "
            f"Supplied argument keys: {supplied_keys}.{alias_hint} {guidance}"
        ),
        is_error=True,
        metadata={
            "missing_required_arguments": missing_fields,
            "retry_count": retry_count,
            "retry_limit": retry_limit,
        },
    )


def _invalid_tool_call_stop_message(tool_name: str, reason: str) -> str:
    return (
        f"Stopping because the model repeatedly produced an invalid tool call for '{tool_name}' "
        f"({reason}) after {INVALID_TOOL_CALL_RETRY_LIMIT} repair attempts."
    )


def _is_tool_preapproved(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    is_mutating: bool,
    risk_level: str,
    approved_tools: set[str],
    approval_manager: ApprovalManager | None,
) -> bool:
    if approval_manager is not None:
        return approval_manager.is_pre_approved(tool_name, arguments) or approval_manager.is_turn_wide_mutating_preapproved(
            tool_name,
            is_mutating=is_mutating,
            risk_level=risk_level,
        )
    return tool_name in approved_tools


def _tool_call_limit_pause_message(max_tool_calls_per_turn: int) -> str:
    return (
        "Tool call limit reached for this turn "
        f"({max_tool_calls_per_turn}). Write `continue` to resume the previous task."
    )


def _max_turns_pause_message(max_turns: int) -> str:
    return (
        "Single-query turn limit reached "
        f"({max_turns}). Write `continue` to resume the previous task, "
        "or increase `max_loop_iterations` to allow more turns per query."
    )


def _risk_level_name(value: Any) -> str:
    raw_value = getattr(value, "value", value)
    return str(raw_value).strip().lower().split(".")[-1]


def _provider_name_from_context(context: ToolExecutionContext) -> str:
    config = context.metadata.get("config")
    provider = getattr(config, "provider", "")
    return str(provider or "").strip()


def _hash_prompt(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def _serialize_message_for_observability(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    if message.name:
        payload["name"] = message.name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "call_id": tool_call.call_id,
                "tool_name": tool_call.tool_name,
                "arguments": tool_call.arguments,
            }
            for tool_call in message.tool_calls
        ]
    return payload


def _correlation_payload(context: ToolExecutionContext, *, tool_call_id: str | None = None) -> dict[str, Any]:
    payload = {
        "turn_id": context.metadata.get("turn_id"),
        "trace_id": context.metadata.get("trace_id"),
        "worker_id": context.metadata.get("worker_id"),
    }
    if tool_call_id is not None:
        payload["tool_call_id"] = tool_call_id
    return {key: value for key, value in payload.items() if value}


def _tool_actor(context: ToolExecutionContext) -> str:
    actor = context.metadata.get("tool_display_prefix") or context.metadata.get("subagent")
    return str(actor).strip() if actor else ""


def _tool_schemas_for_context(registry: ToolRegistry, context: ToolExecutionContext) -> tuple[dict[str, Any], ...]:
    scoped_available = context.metadata.get("supervisor_available_tools")
    if isinstance(scoped_available, list):
        allowed = {str(name) for name in scoped_available}
        records = [
            record
            for record in _supervisor_preferred_tool_records(registry.records())
            if record.name in allowed
        ]
        return tuple(
            _supervisor_tool_schema(record, records)
            for record in records
        )
    if not context.metadata.get("supervisor_cognitive_tools_only"):
        return registry.schemas()
    records = [
        record
        for record in _supervisor_preferred_tool_records(registry.records())
        if record.name.startswith("subagent_")
    ]
    return tuple(_supervisor_tool_schema(record, records) for record in records)


def _supervisor_preferred_tool_records(records: list[Any]) -> list[Any]:
    if not any(str(getattr(record, "name", "")).startswith("subagent_") for record in records):
        return list(records)
    return sorted(records, key=_supervisor_tool_priority)


def _supervisor_tool_priority(record: Any) -> tuple[int, int, str]:
    name = str(getattr(record, "name", ""))
    subagent_order = {
        "subagent_explorer": 0,
        "subagent_coding": 1,
        "subagent_impact_analyzer": 2,
        "subagent_code_reviewer": 3,
    }
    if name.startswith("subagent_"):
        return (0, subagent_order.get(name, 50), name)
    if getattr(record, "source", "") == "mcp":
        return (2, 0, name)
    return (1, 0, name)


def _supervisor_tool_schema(record: Any, records: list[Any]) -> dict[str, Any]:
    schema = tool_to_schema(record.tool)
    function = schema.get("function", {})
    description = str(function.get("description", ""))
    has_subagents = any(str(getattr(item, "name", "")).startswith("subagent_") for item in records)
    name = str(getattr(record, "name", ""))
    if name.startswith("subagent_"):
        function["description"] = _subagent_preference_description(name, description)
    elif has_subagents:
        function["description"] = (
            f"{description} Supervisor direct-use path: use this directly for tiny read-only checks, "
            "one-off recovery steps, or slash/config/status work. Delegate when the task exceeds the "
            "supervisor's small local budget or needs isolated mutation, impact analysis, or post-change review."
        ).strip()
    return schema


def _subagent_preference_description(name: str, description: str) -> str:
    routing = {
        "subagent_explorer": "Preferred for bounded read-only exploration, directory summaries, and codebase scans.",
        "subagent_coding": "Preferred for file edits, implementation, and cheap local validation tied to those edits.",
        "subagent_impact_analyzer": "Preferred when blast radius, affected interfaces, or scoped verification targets are unclear.",
        "subagent_code_reviewer": "Preferred for post-change review, scoped automated verification, and failure attribution.",
    }
    prefix = routing.get(name, "Preferred delegation route for focused cognitive work.")
    return f"{prefix} {description}".strip()


def _tool_available_in_context(record: Any, context: ToolExecutionContext) -> bool:
    scoped_available = context.metadata.get("supervisor_available_tools")
    if isinstance(scoped_available, list):
        return str(record.name) in {str(name) for name in scoped_available}
    if not context.metadata.get("supervisor_cognitive_tools_only"):
        return True
    return str(record.name).startswith("subagent_")


async def _get_tool_confirmation(
    tool: Any,
    call_id: str,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
) -> ToolConfirmation | None:
    get_confirmation = getattr(tool, "get_confirmation", None)
    if get_confirmation is None:
        return None
    return await get_confirmation(call_id, arguments, context)


def _confirmation_preview(confirmation: ToolConfirmation | None) -> dict[str, Any]:
    if confirmation is None:
        return {}
    preview: dict[str, Any] = {
        "description": confirmation.description or "",
        "is_dangerous": confirmation.is_dangerous,
    }
    if confirmation.command:
        preview["command"] = confirmation.command
    if confirmation.affected_paths:
        preview["affected_paths"] = [str(path) for path in confirmation.affected_paths]
    if confirmation.diff is not None:
        preview["diff"] = _serialize_file_diff(confirmation.diff)
    return preview


def _affected_file_paths(
    tool: Any,
    arguments: dict[str, Any],
    confirmation: ToolConfirmation | None,
    context: ToolExecutionContext,
) -> set[str]:
    if not getattr(tool, "is_mutating", False):
        return set()

    workspace = context.working_directory.resolve()
    candidates: list[Path] = []
    if confirmation is not None:
        candidates.extend(confirmation.affected_paths)

    raw_path = arguments.get("path")
    if raw_path:
        candidates.append(_resolve_workspace_path(workspace, str(raw_path)))

    if getattr(tool, "name", "") == "apply_patch":
        candidates.extend(_paths_from_patch_argument(workspace, str(arguments.get("patch", "")), int(arguments.get("strip", 1))))

    paths: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        try:
            rel = resolved.relative_to(workspace)
            paths.add(str(rel))
        except ValueError:
            paths.add(str(resolved))
    return paths


def _resolve_workspace_path(workspace: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _paths_from_patch_argument(workspace: Path, patch_text: str, strip: int) -> list[Path]:
    paths: list[Path] = []
    pending_old_path: str | None = None
    for line in patch_text.splitlines():
        if line.startswith("--- "):
            pending_old_path = _clean_patch_path(line[4:])
            continue
        if line.startswith("+++ ") and pending_old_path is not None:
            new_path = _clean_patch_path(line[4:])
            raw_path = pending_old_path if new_path == "/dev/null" else new_path
            pending_old_path = None
            if raw_path == "/dev/null":
                continue
            parts = Path(raw_path).parts
            if strip > 0 and len(parts) > strip:
                raw_path = str(Path(*parts[strip:]))
            paths.append(_resolve_workspace_path(workspace, raw_path))
    return paths


def _clean_patch_path(raw_path: str) -> str:
    path = raw_path.split("\t")[0].strip()
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _same_file_mutation_result(tool_call: ToolCall, duplicate_paths: set[str]) -> ToolResult:
    paths = ", ".join(sorted(duplicate_paths))
    return ToolResult(
        call_id=tool_call.call_id,
        tool_name=tool_call.tool_name,
        output=(
            "Skipped mutating tool call: another mutating tool call in the same model response "
            f"already targeted {paths}. Read the file again after the prior mutation and issue a fresh edit."
        ),
        is_error=True,
        metadata={
            "same_file_mutation_blocked": True,
            "paths": sorted(duplicate_paths),
        },
    )


def _with_post_mutation_refresh(
    result: ToolResult,
    affected_paths: set[str],
    context: ToolExecutionContext,
) -> ToolResult:
    if not affected_paths or (result.is_error and int(result.metadata.get("files_patched", 0) or 0) <= 0):
        return result

    workspace = context.working_directory.resolve()
    refreshes: list[dict[str, Any]] = []
    output_parts = [result.output]
    for path_text in sorted(affected_paths):
        path = _resolve_workspace_path(workspace, path_text)
        try:
            rel = str(path.relative_to(workspace))
        except ValueError:
            rel = str(path)

        if not path.exists():
            refreshes.append({"path": rel, "exists": False, "content": "", "truncated": False})
            output_parts.append(f"\n[Post-mutation refresh]\nRead {rel} after mutation: file no longer exists.")
            continue
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            refreshes.append({"path": rel, "exists": True, "error": str(exc)})
            output_parts.append(f"\n[Post-mutation refresh]\nCould not read {rel} after mutation: {exc}")
            continue

        max_chars = int(context.metadata.get("post_mutation_read_max_chars", 12_000) or 12_000)
        clipped = content[:max_chars]
        truncated = len(content) > max_chars
        refreshes.append(
            {
                "path": rel,
                "exists": True,
                "content": clipped,
                "truncated": truncated,
                "line_count": len(content.splitlines()),
            }
        )
        suffix = "\n...[truncated]" if truncated else ""
        output_parts.append(
            f"\n[Post-mutation refresh]\nRead {rel} after mutation ({len(content.splitlines())} lines):\n"
            f"```text\n{clipped}{suffix}\n```"
        )

    if not refreshes:
        return result

    metadata = dict(result.metadata)
    metadata["post_mutation_reads"] = refreshes
    metadata["affected_paths"] = sorted(affected_paths)
    return ToolResult(
        call_id=result.call_id,
        tool_name=result.tool_name,
        output="\n".join(output_parts),
        is_error=result.is_error,
        metadata=metadata,
    )


def _serialize_file_diff(diff: FileDiff) -> dict[str, Any]:
    return {
        "path": str(diff.path),
        "is_new_file": diff.is_new_file,
        "is_deletion": diff.is_deletion,
        "old_content": diff.old_content,
        "new_content": diff.new_content,
        "unified_diff": diff.to_diff(),
    }


def _denied_tool_result(
    call_id: str,
    tool_name: str,
    reason: str,
    *,
    risk_level: str,
) -> ToolResult:
    return ToolResult(
        call_id=call_id,
        tool_name=tool_name,
        output=f"Permission denied: {reason}",
        is_error=True,
        metadata={
            "denied": True,
            "reason": reason,
            "risk_level": risk_level,
        },
    )
