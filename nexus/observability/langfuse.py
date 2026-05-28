from __future__ import annotations

import logging
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol

from nexus.hooks import HookEvent, HookExecutor
from nexus.observability.event_descriptions import (
    describe_model_observation,
    describe_notification_event,
    describe_tool_observation,
    describe_turn_observation,
    split_notification_payload,
)
from nexus.observability.logging import redact_payload


logger = logging.getLogger(__name__)

_ACTIVE_ROOT: ContextVar[Any | None] = ContextVar("nexus_langfuse_active_root", default=None)
_ACTIVE_TRACE_ID: ContextVar[str | None] = ContextVar("nexus_langfuse_active_trace_id", default=None)
_LOG_REENTRY: ContextVar[bool] = ContextVar("nexus_langfuse_log_reentry", default=False)

_PROMPT_KEYS = {"prompt", "effective_prompt", "system_prompt", "messages", "output", "response"}
_TOOL_OUTPUT_KEYS = {"output", "raw_output", "tool_output", "assistant_output", "shared_context"}


@dataclass(slots=True, frozen=True)
class LangfuseSettings:
    enabled: bool
    public_key: str
    secret_key: str
    base_url: str
    environment: str
    release: str
    trace_content: bool
    trace_tool_outputs: bool
    prompt_name: str
    prompt_version: str
    flush_timeout_seconds: float
    project_root: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.public_key.strip()) and bool(self.secret_key.strip())


class LangfuseObservationProtocol(Protocol):
    trace_id: str | None
    id: str | None

    def update(self, **kwargs: Any) -> None: ...
    def end(self, **kwargs: Any) -> None: ...
    def start_observation(self, **kwargs: Any) -> Any: ...


class LangfuseClientProtocol(Protocol):
    def start_observation(self, **kwargs: Any) -> Any: ...
    def flush(self) -> None: ...


class _NullPropagationContext:
    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


class _LangfuseLogHandler(logging.Handler):
    def __init__(self, monitor: LangfuseMonitor) -> None:
        super().__init__(level=logging.WARNING)
        self._monitor = monitor

    def emit(self, record: logging.LogRecord) -> None:
        self._monitor.capture_log_record(record)


class LangfuseMonitor:
    def __init__(self, settings: LangfuseSettings, client: LangfuseClientProtocol | None = None) -> None:
        self.settings = settings
        self._client = client
        self._initialized = False
        self._propagate_attributes_factory: Any = None
        self._pending_prompts: dict[str, dict[str, Any]] = {}
        self._turn_observations: dict[str, Any] = {}
        self._model_observations: dict[tuple[str, str], Any] = {}
        self._tool_observations: dict[tuple[str, str], Any] = {}
        self._propagation_contexts: dict[str, Any] = {}
        self._root_tokens: dict[str, Token[Any | None]] = {}
        self._trace_tokens: dict[str, Token[str | None]] = {}
        self._log_handler: _LangfuseLogHandler | None = None

    def initialize(self) -> None:
        if not self.settings.active:
            return
        if self._client is None:
            try:
                from langfuse import get_client, propagate_attributes  # type: ignore[import-not-found]
            except ImportError:
                logger.warning("Langfuse is enabled but langfuse is not installed. Run `uv sync --extra observability`.")
                return
            _apply_langfuse_env(self.settings)
            self._client = get_client()
            self._propagate_attributes_factory = propagate_attributes
        else:
            self._propagate_attributes_factory = lambda **_: _NullPropagationContext()
        self._initialized = True

    def enabled(self) -> bool:
        return self._initialized and self._client is not None

    def flush(self) -> None:
        if not self.enabled():
            return
        try:
            self._client.flush()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to flush Langfuse client: %s", exc)

    def close(self) -> None:
        self.flush()
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

    def remember_prompt(self, payload: dict[str, Any]) -> None:
        trace_id = str(payload.get("trace_id", "") or "")
        if trace_id:
            self._pending_prompts[trace_id] = dict(payload)

    def start_turn(self, payload: dict[str, Any]) -> None:
        if not self.enabled():
            return
        trace_id = str(payload.get("trace_id", "") or "")
        if not trace_id or trace_id in self._turn_observations:
            return
        prompt_payload = self._pending_prompts.pop(trace_id, None)
        propagation_context = self._propagation_context_factory(payload, prompt_payload)
        try:
            propagation_context.__enter__()
            root = self._start_root_observation(payload, prompt_payload)
        except Exception:
            propagation_context.__exit__(None, None, None)
            raise
        if root is None:
            propagation_context.__exit__(None, None, None)
            return
        self._turn_observations[trace_id] = root
        self._propagation_contexts[trace_id] = propagation_context
        self._root_tokens[trace_id] = _ACTIVE_ROOT.set(root)
        self._trace_tokens[trace_id] = _ACTIVE_TRACE_ID.set(trace_id)

    def end_turn(self, payload: dict[str, Any]) -> None:
        trace_id = str(payload.get("trace_id", "") or "")
        root = self._turn_observations.pop(trace_id, None)
        propagation_context = self._propagation_contexts.pop(trace_id, None)
        if root is not None:
            self._finish_observation(
                root,
                output=_turn_output(payload, self.settings),
                metadata=_turn_end_metadata(payload),
            )
        token = self._root_tokens.pop(trace_id, None)
        if token is not None:
            _ACTIVE_ROOT.reset(token)
        trace_token = self._trace_tokens.pop(trace_id, None)
        if trace_token is not None:
            _ACTIVE_TRACE_ID.reset(trace_token)
        if propagation_context is not None:
            propagation_context.__exit__(None, None, None)

    def start_model_generation(self, payload: dict[str, Any]) -> None:
        root = self._root_for_payload(payload)
        trace_id = str(payload.get("trace_id", "") or "")
        model_call_id = str(payload.get("model_call_id", "") or "")
        if root is None or not trace_id or not model_call_id:
            return
        metadata = {
            **_base_context(payload),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "turn_index": payload.get("turn_index"),
            "actor": payload.get("actor"),
            "prompt_name": payload.get("prompt_name"),
            "prompt_version": payload.get("prompt_version"),
            "system_prompt_hash": payload.get("system_prompt_hash"),
            "system_prompt_chars": payload.get("system_prompt_chars"),
            "message_count": payload.get("message_count"),
            "tool_schema_count": payload.get("tool_schema_count"),
            "active_skills": payload.get("active_skills") or [],
            "description": describe_model_observation(payload, phase="start"),
        }
        generation = self._start_child_observation(
            root,
            name="nexus.model",
            as_type="generation",
            model=str(payload.get("model", "") or ""),
            input=_generation_input(payload, self.settings),
            metadata=metadata,
        )
        if generation is not None:
            self._model_observations[(trace_id, model_call_id)] = generation

    def end_model_generation(self, payload: dict[str, Any]) -> None:
        trace_id = str(payload.get("trace_id", "") or "")
        model_call_id = str(payload.get("model_call_id", "") or "")
        generation = self._model_observations.pop((trace_id, model_call_id), None)
        if generation is None:
            return
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        metadata = {
            **_base_context(payload),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "finish_reason": payload.get("finish_reason"),
            "tool_call_count": payload.get("tool_call_count"),
            "status": payload.get("status", "completed"),
            "description": describe_model_observation(payload, phase="end"),
        }
        if payload.get("error"):
            metadata["error"] = payload.get("error")
        finish_kwargs: dict[str, Any] = {
            "output": _event_io_payload(payload.get("output"), enabled=self.settings.trace_content),
            "metadata": metadata,
            "usage_details": {
                "input": usage.get("prompt_tokens", 0),
                "output": usage.get("completion_tokens", 0),
                "total": usage.get("total_tokens", 0),
            },
        }
        if usage.get("estimated_cost_usd") is not None:
            finish_kwargs["cost_details"] = {"total_cost": usage.get("estimated_cost_usd")}
        self._finish_observation(generation, **finish_kwargs)

    def start_tool_span(self, payload: dict[str, Any]) -> None:
        root = self._root_for_payload(payload)
        trace_id = str(payload.get("trace_id", "") or "")
        call_id = str(payload.get("call_id", "") or "")
        if root is None or not trace_id or not call_id:
            return
        observation = self._start_child_observation(
            root,
            name=f"tool.{payload.get('tool_name', 'unknown')}",
            as_type="span",
            input=_tool_input(payload, self.settings),
            metadata={
                **_base_context(payload),
                **_tool_context(payload),
                "description": describe_tool_observation(payload),
            },
        )
        if observation is not None:
            self._tool_observations[(trace_id, call_id)] = observation

    def end_tool_span(self, payload: dict[str, Any]) -> None:
        trace_id = str(payload.get("trace_id", "") or "")
        call_id = str(payload.get("call_id", "") or "")
        observation = self._tool_observations.pop((trace_id, call_id), None)
        if observation is None:
            return
        metadata = {
            **_base_context(payload),
            **_tool_context(payload),
            "duration_ms": payload.get("duration_ms"),
            "is_error": bool(payload.get("is_error")),
            "exception_type": payload.get("exception_type"),
            "description": describe_tool_observation(payload, phase="end"),
        }
        self._finish_observation(
            observation,
            output=_tool_output(payload, self.settings),
            metadata=metadata,
        )

    def record_context_event(self, name: str, payload: dict[str, Any]) -> None:
        root = self._root_for_payload(payload)
        if root is None:
            return
        event_name = str(payload.get("event", name) or name)
        input_payload, output_payload = split_notification_payload(payload)
        event = self._start_child_observation(
            root,
            name=name,
            as_type="event",
            input=_notification_input(input_payload, self.settings),
            metadata={
                **_base_context(payload),
                "event": event_name,
                "description": describe_notification_event(event_name, payload),
            },
        )
        if event is not None:
            finish_kwargs: dict[str, Any] = {}
            if output_payload is not None:
                finish_kwargs["output"] = _event_io_payload(output_payload, enabled=self.settings.trace_content)
            self._finish_observation(event, **finish_kwargs)

    def capture_log_record(self, record: logging.LogRecord) -> None:
        if not self.enabled() or record.levelno < logging.WARNING:
            return
        if record.name != "py.warnings" and not record.name.startswith("nexus"):
            return
        root = _ACTIVE_ROOT.get()
        if root is None or _LOG_REENTRY.get():
            return
        token = _LOG_REENTRY.set(True)
        try:
            event = self._start_child_observation(
                root,
                name="nexus.log",
                as_type="event",
                input={
                    "message": redact_payload({"message": record.getMessage()})["message"],
                },
                metadata={
                    "level": record.levelname,
                    "logger": record.name,
                    "pathname": record.pathname,
                    "lineno": record.lineno,
                    "trace_id": _ACTIVE_TRACE_ID.get(),
                },
            )
            if event is not None:
                self._finish_observation(event)
        finally:
            _LOG_REENTRY.reset(token)

    def _install_log_handler(self) -> None:
        if self._log_handler is not None:
            return
        self._log_handler = _LangfuseLogHandler(self)
        logging.getLogger().addHandler(self._log_handler)

    def _propagation_context_factory(self, payload: dict[str, Any], prompt_payload: dict[str, Any] | None) -> Any:
        if self._propagate_attributes_factory is None:
            return _NullPropagationContext()
        metadata = {
            "provider": str(payload.get("provider", "") or ""),
            "model": str(payload.get("model", "") or ""),
            "mode": str(payload.get("mode", "") or ""),
            "agentmode": str(payload.get("agent_mode", "") or ""),
        }
        if prompt_payload is not None:
            prompt = str(prompt_payload.get("effective_prompt") or prompt_payload.get("prompt") or "")
            metadata["promptchars"] = len(prompt)
        metadata = {key: value for key, value in metadata.items() if value not in (None, "")}
        try:
            return self._propagate_attributes_factory(
                session_id=str(payload.get("session_id", "") or ""),
                user_id=str(payload.get("user_id", "") or "") or None,
                metadata={key: str(value)[:200] for key, value in metadata.items()},
                version=self.settings.release or None,
                tags=_trace_tags(payload),
                trace_name="nexus.turn",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to create Langfuse propagation context: %s", exc)
            return _NullPropagationContext()

    def _start_root_observation(self, payload: dict[str, Any], prompt_payload: dict[str, Any] | None) -> Any | None:
        if self._client is None:
            return None
        kwargs: dict[str, Any] = {
            "name": "nexus.turn",
            "as_type": "span",
            "session_id": str(payload.get("session_id", "") or "") or None,
            "input": _turn_input(prompt_payload, payload, self.settings),
            "metadata": {
                **_base_context(payload),
                "provider": payload.get("provider"),
                "model": payload.get("model"),
                "mode": payload.get("mode"),
                "agent_mode": payload.get("agent_mode"),
                "status": payload.get("status"),
                "session_scope": "nexus.session_id",
                "description": describe_turn_observation(payload),
            },
        }
        trace_id = str(payload.get("trace_id", "") or "")
        if _is_hex_trace_id(trace_id):
            kwargs["trace_context"] = {"trace_id": trace_id}
        return self._start_observation(self._client.start_observation, **kwargs)

    def _root_for_payload(self, payload: dict[str, Any]) -> Any | None:
        trace_id = str(payload.get("trace_id", "") or "")
        return self._turn_observations.get(trace_id) or _ACTIVE_ROOT.get()

    def _start_child_observation(self, parent: Any, **kwargs: Any) -> Any | None:
        factory = getattr(parent, "start_observation", None)
        if factory is None:
            return None
        return self._start_observation(factory, **kwargs)

    def _start_observation(self, factory: Any, **kwargs: Any) -> Any | None:
        try:
            return factory(**kwargs)
        except TypeError:
            fallback = dict(kwargs)
            for key in ("trace_context", "session_id", "user_id", "tags", "version"):
                fallback.pop(key, None)
            if fallback.get("as_type") == "event":
                fallback["as_type"] = "span"
            try:
                return factory(**fallback)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to start Langfuse observation: %s", exc)
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to start Langfuse observation: %s", exc)
            return None

    def _finish_observation(self, observation: Any, **kwargs: Any) -> None:
        cleaned = {key: value for key, value in kwargs.items() if value not in (None, {}, [])}
        try:
            observation.end(**cleaned)
            return
        except TypeError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to end Langfuse observation directly: %s", exc)
            return
        try:
            update = getattr(observation, "update", None)
            if update is not None and cleaned:
                update(**cleaned)
            observation.end()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to finalize Langfuse observation: %s", exc)


class LangfuseHookService:
    def __init__(self, monitor: LangfuseMonitor, settings: LangfuseSettings) -> None:
        self.monitor = monitor
        self.settings = settings

    def register(self, hooks: HookExecutor) -> None:
        hooks.register(HookEvent.USER_PROMPT_SUBMIT, self.on_user_prompt)
        hooks.register(HookEvent.TURN_START, self.on_turn_start)
        hooks.register(HookEvent.TURN_END, self.on_turn_end)
        hooks.register(HookEvent.STOP, self.on_stop)

    async def on_user_prompt(self, payload: dict[str, Any]) -> None:
        self.monitor.remember_prompt(payload)

    async def on_turn_start(self, payload: dict[str, Any]) -> None:
        self.monitor.start_turn(payload)

    async def on_turn_end(self, payload: dict[str, Any]) -> None:
        self.monitor.end_turn(payload)

    async def on_pre_tool(self, payload: dict[str, Any]) -> None:
        self.monitor.start_tool_span(payload)

    async def on_post_tool(self, payload: dict[str, Any]) -> None:
        self.monitor.end_tool_span(payload)

    async def on_notification(self, payload: dict[str, Any]) -> None:
        event_name = str(payload.get("event", "") or "")
        if event_name == "model_start":
            self.monitor.start_model_generation(payload)
            return
        if event_name == "model_end":
            self.monitor.end_model_generation(payload)
            return
        if event_name == "model_error":
            self.monitor.end_model_generation({**payload, "status": "error", "output": payload.get("error")})
        self.monitor.record_context_event(f"notification.{event_name or 'unknown'}", payload)

    async def on_context_compaction(self, payload: dict[str, Any]) -> None:
        self.monitor.record_context_event("context.compaction", payload)

    async def on_stop(self, payload: dict[str, Any]) -> None:
        if payload.get("headless"):
            self.monitor.flush()


def langfuse_settings_from_config(config: Any) -> LangfuseSettings:
    return LangfuseSettings(
        enabled=bool(getattr(config, "langfuse_enabled", False)),
        public_key=str(getattr(config, "langfuse_public_key", "") or ""),
        secret_key=str(getattr(config, "langfuse_secret_key", "") or ""),
        base_url=str(getattr(config, "langfuse_base_url", "https://cloud.langfuse.com") or "https://cloud.langfuse.com"),
        environment=str(getattr(config, "langfuse_environment", "development") or "development"),
        release=str(getattr(config, "langfuse_release", "") or ""),
        trace_content=bool(getattr(config, "langfuse_trace_content", True)),
        trace_tool_outputs=bool(getattr(config, "langfuse_trace_tool_outputs", True)),
        prompt_name=str(getattr(config, "langfuse_prompt_name", "nexus-system-prompt") or "nexus-system-prompt"),
        prompt_version=str(getattr(config, "langfuse_prompt_version", "") or ""),
        flush_timeout_seconds=float(getattr(config, "langfuse_flush_timeout_seconds", 2.0)),
        project_root=str(getattr(config, "workspace_root", "") or ""),
    )


def setup_langfuse_monitor(config: Any, client: LangfuseClientProtocol | None = None) -> LangfuseMonitor:
    monitor = LangfuseMonitor(langfuse_settings_from_config(config), client=client)
    monitor.initialize()
    return monitor


def langfuse_monitor_from_hooks(hooks: HookExecutor | None) -> LangfuseMonitor | None:
    monitor = getattr(hooks, "langfuse_monitor", None) if hooks is not None else None
    return monitor if isinstance(monitor, LangfuseMonitor) else None


def _apply_langfuse_env(settings: LangfuseSettings) -> None:
    os.environ["LANGFUSE_PUBLIC_KEY"] = settings.public_key
    os.environ["LANGFUSE_SECRET_KEY"] = settings.secret_key
    os.environ["LANGFUSE_BASE_URL"] = settings.base_url


def _base_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in ("session_id", "turn_id", "trace_id", "worker_id", "call_id", "model_call_id")
        if payload.get(key) not in (None, "")
    }


def _tool_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in ("tool_name", "tool_source", "tool_origin", "is_mutating")
        if payload.get(key) not in (None, "")
    }


def _trace_tags(payload: dict[str, Any]) -> list[str]:
    tags = [str(value) for value in (payload.get("provider"), payload.get("model"), payload.get("mode")) if value]
    agent_mode = str(payload.get("agent_mode", "") or "")
    if agent_mode:
        tags.append(f"agent-mode:{agent_mode}")
    return tags[:8]


def _turn_input(prompt_payload: dict[str, Any] | None, turn_payload: dict[str, Any], settings: LangfuseSettings) -> Any:
    prompt = ""
    effective_prompt = ""
    resumed_paused_turn = False
    if prompt_payload is not None:
        prompt = str(prompt_payload.get("prompt", "") or "")
        effective_prompt = str(prompt_payload.get("effective_prompt", "") or "")
        resumed_paused_turn = bool(prompt_payload.get("resumed_paused_turn"))
    raw = {
        "prompt": prompt,
        "effective_prompt": effective_prompt or prompt,
        "resumed_paused_turn": resumed_paused_turn,
        "mode": turn_payload.get("mode"),
    }
    return _structured_payload(raw, settings=settings)


def _turn_output(payload: dict[str, Any], settings: LangfuseSettings) -> Any:
    response = payload.get("response")
    if response not in (None, ""):
        return _event_io_payload(
            {
                "response": response,
                "status": payload.get("status"),
                "tool_calls": payload.get("tool_calls"),
                "turn_steps": payload.get("turn_steps") or [],
                "duration_ms": payload.get("duration_ms"),
            },
            enabled=settings.trace_content,
        )
    return {
        "status": payload.get("status"),
        "tool_calls": payload.get("tool_calls"),
        "turn_steps": payload.get("turn_steps") or [],
        "duration_ms": payload.get("duration_ms"),
    }


def _turn_end_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        **_base_context(payload),
        "status": payload.get("status"),
        "provider": payload.get("provider"),
        "model": payload.get("model"),
        "mode": payload.get("mode"),
        "agent_mode": payload.get("agent_mode"),
        "tool_calls": payload.get("tool_calls"),
        "duration_ms": payload.get("duration_ms"),
        "session_scope": "nexus.session_id",
        "description": describe_turn_observation(payload),
    }
    if payload.get("usage"):
        metadata["usage"] = payload.get("usage")
    if payload.get("error"):
        metadata["error"] = payload.get("error")
    return metadata


def _generation_input(payload: dict[str, Any], settings: LangfuseSettings) -> Any:
    raw = {
        "system_prompt": payload.get("system_prompt", ""),
        "messages": payload.get("messages") if isinstance(payload.get("messages"), list) else [],
        "max_output_tokens": payload.get("max_output_tokens"),
        "temperature": payload.get("temperature"),
    }
    return _structured_payload(raw, settings=settings)


def _tool_input(payload: dict[str, Any], settings: LangfuseSettings) -> Any:
    raw = {
        "arguments": payload.get("arguments", {}),
        "tool_name": payload.get("tool_name"),
        "tool_source": payload.get("tool_source"),
        "tool_origin": payload.get("tool_origin"),
    }
    return _structured_payload(raw, settings=settings)


def _tool_output(payload: dict[str, Any], settings: LangfuseSettings) -> Any:
    raw = {
        "output": payload.get("output"),
        "is_error": bool(payload.get("is_error")),
    }
    return _event_io_payload(raw, enabled=settings.trace_tool_outputs)


def _notification_input(payload: dict[str, Any], settings: LangfuseSettings) -> Any:
    return _structured_payload(dict(payload), settings=settings)


def _event_io_payload(value: Any, *, enabled: bool) -> Any:
    if enabled:
        if isinstance(value, dict):
            return redact_payload(value)
        return redact_payload({"value": value})
    return {"suppressed": True, "chars": len(str(value or ""))}


def _structured_payload(raw: dict[str, Any], *, settings: LangfuseSettings) -> dict[str, Any]:
    if settings.trace_content:
        return redact_payload(raw)
    suppressed: dict[str, Any] = {}
    for key, value in raw.items():
        normalized = str(key).lower()
        if normalized in _PROMPT_KEYS or normalized in _TOOL_OUTPUT_KEYS:
            suppressed[f"{key}_chars"] = len(str(value or ""))
        else:
            suppressed[key] = value
    return suppressed


def _is_hex_trace_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "LangfuseClientProtocol",
    "LangfuseHookService",
    "LangfuseMonitor",
    "LangfuseObservationProtocol",
    "LangfuseSettings",
    "langfuse_monitor_from_hooks",
    "langfuse_settings_from_config",
    "setup_langfuse_monitor",
]