from __future__ import annotations

import base64
import json
import logging
import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

_ACTIVE_ROOT: ContextVar[Any | None] = ContextVar("nexus_otel_active_root", default=None)
_ACTIVE_TRACE_ID: ContextVar[str | None] = ContextVar("nexus_otel_active_trace_id", default=None)
_LOG_REENTRY: ContextVar[bool] = ContextVar("nexus_otel_log_reentry", default=False)

_PROMPT_KEYS = {"prompt", "effective_prompt", "system_prompt", "messages", "output", "response"}
_TOOL_OUTPUT_KEYS = {"output", "raw_output", "tool_output", "assistant_output", "shared_context"}


@dataclass(slots=True, frozen=True)
class OtelSettings:
    enabled: bool
    endpoint: str
    headers: str
    service_name: str
    environment: str
    release: str
    trace_content: bool
    trace_tool_outputs: bool
    prompt_name: str
    prompt_version: str
    flush_timeout_seconds: float
    jsonl_enabled: bool
    jsonl_path: str
    project_root: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and (self.jsonl_enabled or bool(self.endpoint.strip()))


class _OtelLogHandler(logging.Handler):
    def __init__(self, monitor: OtelMonitor) -> None:
        super().__init__(level=logging.WARNING)
        self._monitor = monitor

    def emit(self, record: logging.LogRecord) -> None:
        self._monitor.capture_log_record(record)


class OtelMonitor:
    def __init__(self, settings: OtelSettings) -> None:
        self.settings = settings
        self._initialized = False
        self._trace_api: Any = None
        self._status_cls: Any = None
        self._status_code: Any = None
        self._provider: Any = None
        self._tracer: Any = None
        self._turn_spans: dict[str, Any] = {}
        self._model_spans: dict[tuple[str, str], Any] = {}
        self._tool_spans: dict[tuple[str, str], Any] = {}
        self._pending_prompts: dict[str, dict[str, Any]] = {}
        self._root_tokens: dict[str, Token[Any | None]] = {}
        self._trace_tokens: dict[str, Token[str | None]] = {}
        self._log_handler: _OtelLogHandler | None = None

    def initialize(self) -> None:
        if not self.settings.active:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
            from opentelemetry.trace import Status, StatusCode
        except ImportError:
            logger.warning(
                "OpenTelemetry tracing is enabled but opentelemetry packages are not installed. "
                "Run `uv sync --extra observability`."
            )
            return

        resource_attributes = {
            "service.name": self.settings.service_name or "nexus",
            "deployment.environment": self.settings.environment or "development",
        }
        if self.settings.release:
            resource_attributes["service.version"] = self.settings.release

        provider = TracerProvider(resource=Resource.create(resource_attributes))
        if self.settings.jsonl_enabled:
            provider.add_span_processor(
                SimpleSpanProcessor(JsonlSpanExporter(Path(self.settings.jsonl_path)))
            )
        if self.settings.endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            except ImportError:
                logger.warning(
                    "OTLP tracing is configured but the HTTP exporter is not installed. "
                    "Run `uv sync --extra observability`."
                )
            else:
                provider.add_span_processor(
                    BatchSpanProcessor(
                        OTLPSpanExporter(
                            endpoint=self.settings.endpoint,
                            headers=_parse_header_string(self.settings.headers) or None,
                            timeout=int(self.settings.flush_timeout_seconds * 1000),
                        )
                    )
                )

        self._trace_api = trace
        self._status_cls = Status
        self._status_code = StatusCode
        self._provider = provider
        self._tracer = provider.get_tracer("nexus.observability", self.settings.release or None)
        self._initialized = True
        self._install_log_handler()

    def enabled(self) -> bool:
        return self._initialized and self._provider is not None and self._tracer is not None

    def flush(self) -> None:
        if not self.enabled():
            return
        try:
            self._provider.force_flush(timeout_millis=int(self.settings.flush_timeout_seconds * 1000))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to flush OpenTelemetry spans: %s", exc)

    def close(self) -> None:
        self.flush()
        if self._provider is not None:
            try:
                self._provider.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to shut down OpenTelemetry provider: %s", exc)
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
        if not trace_id or trace_id in self._turn_spans:
            return
        prompt_payload = self._pending_prompts.pop(trace_id, None)
        span = self._tracer.start_span(
            "nexus.turn",
            attributes=_attributes(
                {
                    **_base_context(payload),
                    "nexus.span_type": "turn",
                    "nexus.provider": payload.get("provider"),
                    "nexus.model": payload.get("model"),
                    "nexus.mode": payload.get("mode"),
                    "nexus.agent_mode": payload.get("agent_mode"),
                    "nexus.status": payload.get("status"),
                    "nexus.prompt_name": self.settings.prompt_name,
                    "nexus.prompt_version": self.settings.prompt_version,
                    "nexus.session_scope": "nexus.session_id",
                    "nexus.description": describe_turn_observation(payload),
                }
            ),
        )
        _add_payload_event(span, "turn.input", _turn_input(prompt_payload, payload, self.settings))
        self._turn_spans[trace_id] = span
        self._root_tokens[trace_id] = _ACTIVE_ROOT.set(span)
        self._trace_tokens[trace_id] = _ACTIVE_TRACE_ID.set(trace_id)

    def end_turn(self, payload: dict[str, Any]) -> None:
        trace_id = str(payload.get("trace_id", "") or "")
        span = self._turn_spans.pop(trace_id, None)
        if span is not None:
            _set_attributes(
                span,
                {
                    "nexus.status": payload.get("status"),
                    "nexus.duration_ms": payload.get("duration_ms"),
                    "nexus.tool_calls": payload.get("tool_calls"),
                },
            )
            if payload.get("usage"):
                _add_payload_event(span, "turn.usage", payload.get("usage"))
            if payload.get("error"):
                _mark_error(span, str(payload.get("error") or "turn error"), self._status_cls, self._status_code)
            _add_payload_event(span, "turn.output", _turn_output(payload, self.settings))
            span.end()
        token = self._root_tokens.pop(trace_id, None)
        if token is not None:
            _ACTIVE_ROOT.reset(token)
        trace_token = self._trace_tokens.pop(trace_id, None)
        if trace_token is not None:
            _ACTIVE_TRACE_ID.reset(trace_token)

    def start_model_generation(self, payload: dict[str, Any]) -> None:
        root = self._root_for_payload(payload)
        trace_id = str(payload.get("trace_id", "") or "")
        model_call_id = str(payload.get("model_call_id", "") or "")
        if root is None or not trace_id or not model_call_id:
            return
        span = self._start_child_span(
            root,
            "nexus.model",
            {
                **_base_context(payload),
                "nexus.span_type": "model",
                "nexus.provider": payload.get("provider"),
                "nexus.model": payload.get("model"),
                "nexus.turn_index": payload.get("turn_index"),
                "nexus.actor": payload.get("actor"),
                "nexus.message_count": payload.get("message_count"),
                "nexus.tool_schema_count": payload.get("tool_schema_count"),
                "nexus.prompt_name": payload.get("prompt_name") or self.settings.prompt_name,
                "nexus.prompt_version": payload.get("prompt_version") or self.settings.prompt_version,
                "nexus.system_prompt_hash": payload.get("system_prompt_hash"),
                "nexus.system_prompt_chars": payload.get("system_prompt_chars"),
                "nexus.description": describe_model_observation(payload, phase="start"),
            },
            input_payload=_generation_input(payload, self.settings),
        )
        if span is not None:
            self._model_spans[(trace_id, model_call_id)] = span

    def end_model_generation(self, payload: dict[str, Any]) -> None:
        trace_id = str(payload.get("trace_id", "") or "")
        model_call_id = str(payload.get("model_call_id", "") or "")
        span = self._model_spans.pop((trace_id, model_call_id), None)
        if span is None:
            return
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        _set_attributes(
            span,
            {
                "nexus.finish_reason": payload.get("finish_reason"),
                "nexus.tool_call_count": payload.get("tool_call_count"),
                "nexus.status": payload.get("status", "completed"),
                "nexus.usage.prompt_tokens": usage.get("prompt_tokens"),
                "nexus.usage.completion_tokens": usage.get("completion_tokens"),
                "nexus.usage.total_tokens": usage.get("total_tokens"),
                "nexus.usage.estimated_cost_usd": usage.get("estimated_cost_usd"),
                "nexus.description": describe_model_observation(payload, phase="end"),
            },
        )
        if payload.get("error"):
            _mark_error(span, str(payload.get("error") or "model error"), self._status_cls, self._status_code)
        _add_payload_event(span, "model.output", _event_io_payload(payload.get("output"), enabled=self.settings.trace_content))
        if usage:
            _add_payload_event(span, "model.usage", usage)
        span.end()

    def start_tool_span(self, payload: dict[str, Any]) -> None:
        root = self._root_for_payload(payload)
        trace_id = str(payload.get("trace_id", "") or "")
        call_id = str(payload.get("call_id", "") or "")
        if root is None or not trace_id or not call_id:
            return
        span = self._start_child_span(
            root,
            f"tool.{payload.get('tool_name', 'unknown')}",
            {
                **_base_context(payload),
                **_tool_context(payload),
                "nexus.span_type": "tool",
                "nexus.description": describe_tool_observation(payload),
            },
            input_payload=_tool_input(payload, self.settings),
        )
        if span is not None:
            self._tool_spans[(trace_id, call_id)] = span

    def end_tool_span(self, payload: dict[str, Any]) -> None:
        trace_id = str(payload.get("trace_id", "") or "")
        call_id = str(payload.get("call_id", "") or "")
        span = self._tool_spans.pop((trace_id, call_id), None)
        if span is None:
            return
        _set_attributes(
            span,
            {
                **_tool_context(payload),
                "nexus.duration_ms": payload.get("duration_ms"),
                "nexus.is_error": bool(payload.get("is_error")),
                "nexus.exception_type": payload.get("exception_type"),
                "nexus.description": describe_tool_observation(payload, phase="end"),
            },
        )
        if payload.get("is_error"):
            _mark_error(span, str(payload.get("output") or payload.get("exception_type") or "tool error"), self._status_cls, self._status_code)
        _add_payload_event(span, "tool.output", _tool_output(payload, self.settings))
        span.end()

    def record_context_event(self, name: str, payload: dict[str, Any]) -> None:
        root = self._root_for_payload(payload)
        if root is None:
            return
        event_name = str(payload.get("event", name) or name)
        input_payload, output_payload = split_notification_payload(payload)
        span = self._start_child_span(
            root,
            name,
            {
                **_base_context(payload),
                "nexus.span_type": "event",
                "nexus.event": event_name,
                "nexus.description": describe_notification_event(event_name, payload),
            },
            input_payload=_notification_input(input_payload, self.settings),
        )
        if span is not None:
            if output_payload is not None:
                _add_payload_event(span, "output", _event_io_payload(output_payload, enabled=self.settings.trace_content))
            span.end()

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
            span = self._start_child_span(
                root,
                "nexus.log",
                {
                    "nexus.span_type": "log",
                    "nexus.log.level": record.levelname,
                    "nexus.log.logger": record.name,
                    "nexus.log.pathname": record.pathname,
                    "nexus.log.lineno": record.lineno,
                    "trace_id": _ACTIVE_TRACE_ID.get(),
                },
                input_payload={"message": redact_payload({"message": record.getMessage()})["message"]},
            )
            if span is not None:
                if record.levelno >= logging.ERROR:
                    _mark_error(span, record.getMessage(), self._status_cls, self._status_code)
                span.end()
        finally:
            _LOG_REENTRY.reset(token)

    def _install_log_handler(self) -> None:
        if self._log_handler is not None:
            return
        self._log_handler = _OtelLogHandler(self)
        logging.getLogger().addHandler(self._log_handler)

    def _root_for_payload(self, payload: dict[str, Any]) -> Any | None:
        trace_id = str(payload.get("trace_id", "") or "")
        return self._turn_spans.get(trace_id) or _ACTIVE_ROOT.get()

    def _start_child_span(
        self,
        parent: Any,
        name: str,
        attributes: dict[str, Any],
        *,
        input_payload: Any | None = None,
    ) -> Any | None:
        if self._tracer is None or self._trace_api is None:
            return None
        try:
            span = self._tracer.start_span(
                name,
                context=self._trace_api.set_span_in_context(parent),
                attributes=_attributes(attributes),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to start child span %s: %s", name, exc)
            return None
        if input_payload not in (None, {}, []):
            _add_payload_event(span, "input", input_payload)
        return span


class JsonlSpanExporter:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans: list[Any]) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            for span in spans:
                handle.write(json.dumps(_serialize_span(span), sort_keys=True))
                handle.write("\n")
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class OtelHookService:
    def __init__(self, monitor: OtelMonitor, settings: OtelSettings) -> None:
        self.monitor = monitor
        self.settings = settings

    def register(self, hooks: HookExecutor) -> None:
        hooks.register(HookEvent.USER_PROMPT_SUBMIT, self.on_user_prompt)
        hooks.register(HookEvent.TURN_START, self.on_turn_start)
        hooks.register(HookEvent.TURN_END, self.on_turn_end)
        hooks.register(HookEvent.PRE_TOOL_USE, self.on_pre_tool)
        hooks.register(HookEvent.POST_TOOL_USE, self.on_post_tool)
        hooks.register(HookEvent.NOTIFICATION, self.on_notification)
        hooks.register(HookEvent.CONTEXT_COMPACTION, self.on_context_compaction)
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


def otel_settings_from_config(config: Any) -> OtelSettings:
    langfuse_enabled = bool(getattr(config, "langfuse_enabled", False))
    endpoint = _normalize_otlp_endpoint(str(getattr(config, "otel_endpoint", "") or ""))
    if not endpoint and langfuse_enabled:
        base_url = str(getattr(config, "langfuse_base_url", "https://cloud.langfuse.com") or "https://cloud.langfuse.com")
        endpoint = _normalize_otlp_endpoint(f"{base_url.rstrip('/')}/api/public/otel")

    headers = str(getattr(config, "otel_headers", "") or "")
    if not headers and langfuse_enabled:
        public_key = str(getattr(config, "langfuse_public_key", "") or "")
        secret_key = str(getattr(config, "langfuse_secret_key", "") or "")
        if public_key and secret_key:
            headers = _langfuse_auth_header(public_key, secret_key)

    enabled = bool(getattr(config, "otel_enabled", False) or langfuse_enabled)
    jsonl_enabled = bool(getattr(config, "otel_jsonl_enabled", True))
    log_dir = Path(getattr(config, "log_dir", Path.cwd()))
    return OtelSettings(
        enabled=enabled,
        endpoint=endpoint,
        headers=headers,
        service_name=str(getattr(config, "otel_service_name", "") or getattr(config, "project_name", "") or "nexus"),
        environment=str(
            getattr(config, "otel_environment", "")
            or getattr(config, "langfuse_environment", "development")
            or "development"
        ),
        release=str(getattr(config, "otel_release", "") or getattr(config, "langfuse_release", "") or ""),
        trace_content=bool(
            getattr(config, "otel_trace_content", getattr(config, "langfuse_trace_content", True))
        ),
        trace_tool_outputs=bool(
            getattr(config, "otel_trace_tool_outputs", getattr(config, "langfuse_trace_tool_outputs", True))
        ),
        prompt_name=str(
            getattr(config, "otel_prompt_name", "")
            or getattr(config, "langfuse_prompt_name", "nexus-system-prompt")
            or "nexus-system-prompt"
        ),
        prompt_version=str(
            getattr(config, "otel_prompt_version", "")
            or getattr(config, "langfuse_prompt_version", "")
            or ""
        ),
        flush_timeout_seconds=float(
            getattr(
                config,
                "otel_flush_timeout_seconds",
                getattr(config, "langfuse_flush_timeout_seconds", 2.0),
            )
        ),
        jsonl_enabled=jsonl_enabled,
        jsonl_path=str(log_dir / "traces.jsonl"),
        project_root=str(getattr(config, "workspace_root", "") or ""),
    )


def setup_otel_monitor(config: Any) -> OtelMonitor:
    monitor = OtelMonitor(otel_settings_from_config(config))
    monitor.initialize()
    return monitor


def otel_monitor_from_hooks(hooks: HookExecutor | None) -> OtelMonitor | None:
    monitor = getattr(hooks, "otel_monitor", None) if hooks is not None else None
    return monitor if isinstance(monitor, OtelMonitor) else None


def _base_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in ("session_id", "turn_id", "trace_id", "worker_id", "call_id", "model_call_id")
        if payload.get(key) not in (None, "")
    }


def _tool_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        f"nexus.{key}": payload.get(key)
        for key in ("tool_name", "tool_source", "tool_origin", "is_mutating")
        if payload.get(key) not in (None, "")
    }


def _turn_input(prompt_payload: dict[str, Any] | None, turn_payload: dict[str, Any], settings: OtelSettings) -> Any:
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


def _turn_output(payload: dict[str, Any], settings: OtelSettings) -> Any:
    response = payload.get("response")
    if response not in (None, ""):
        return _event_io_payload({"response": response, "status": payload.get("status")}, enabled=settings.trace_content)
    return {
        "status": payload.get("status"),
        "tool_calls": payload.get("tool_calls"),
        "duration_ms": payload.get("duration_ms"),
    }


def _generation_input(payload: dict[str, Any], settings: OtelSettings) -> Any:
    raw = {
        "system_prompt": payload.get("system_prompt", ""),
        "messages": payload.get("messages") if isinstance(payload.get("messages"), list) else [],
        "max_output_tokens": payload.get("max_output_tokens"),
        "temperature": payload.get("temperature"),
    }
    return _structured_payload(raw, settings=settings)


def _tool_input(payload: dict[str, Any], settings: OtelSettings) -> Any:
    raw = {
        "arguments": payload.get("arguments", {}),
        "tool_name": payload.get("tool_name"),
        "tool_source": payload.get("tool_source"),
        "tool_origin": payload.get("tool_origin"),
    }
    return _structured_payload(raw, settings=settings)


def _tool_output(payload: dict[str, Any], settings: OtelSettings) -> Any:
    raw = {
        "output": payload.get("output"),
        "is_error": bool(payload.get("is_error")),
    }
    return _event_io_payload(raw, enabled=settings.trace_tool_outputs)


def _notification_input(payload: dict[str, Any], settings: OtelSettings) -> Any:
    return _structured_payload(dict(payload), settings=settings)


def _event_io_payload(value: Any, *, enabled: bool) -> Any:
    if enabled:
        if isinstance(value, dict):
            return redact_payload(value)
        return redact_payload({"value": value})
    return {"suppressed": True, "chars": len(str(value or ""))}


def _structured_payload(raw: dict[str, Any], *, settings: OtelSettings) -> dict[str, Any]:
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


def _normalize_otlp_endpoint(value: str) -> str:
    endpoint = value.strip()
    if not endpoint:
        return ""
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/v1/traces"):
        return endpoint
    return f"{endpoint}/v1/traces"


def _langfuse_auth_header(public_key: str, secret_key: str) -> str:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Authorization=Basic {token}"


def _parse_header_string(value: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part or "=" not in part:
            continue
        key, header_value = part.split("=", 1)
        key = key.strip()
        header_value = header_value.strip()
        if key:
            headers[key] = header_value
    return headers


def _attributes(values: dict[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for key, value in values.items():
        if value in (None, "", [], {}):
            continue
        normalized = _attribute_value(value)
        if normalized is not None:
            attributes[str(key)] = normalized
    return attributes


def _attribute_value(value: Any) -> Any:
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for item in value:
            normalized = _attribute_value(item)
            if isinstance(normalized, (bool, int, float, str)):
                items.append(normalized)
        return items if items else None
    return json.dumps(_json_value(value), sort_keys=True)


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(inner) for key, inner in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


def _payload_attributes(payload: Any) -> dict[str, Any]:
    payload_json = json.dumps(_json_value(payload), sort_keys=True)
    return {
        "nexus.payload.json": payload_json,
        "nexus.payload.chars": len(payload_json),
    }


def _add_payload_event(span: Any, name: str, payload: Any) -> None:
    if payload in (None, {}, []):
        return
    try:
        span.add_event(name, _attributes(_payload_attributes(payload)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to add payload event %s: %s", name, exc)


def _set_attributes(span: Any, attributes: dict[str, Any]) -> None:
    for key, value in _attributes(attributes).items():
        try:
            span.set_attribute(key, value)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to set span attribute %s: %s", key, exc)


def _mark_error(span: Any, description: str, status_cls: Any, status_code: Any) -> None:
    try:
        span.set_status(status_cls(status_code.ERROR, description))
        span.add_event("error", {"message": description[:4000]})
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to mark span error: %s", exc)


def _serialize_span(span: Any) -> dict[str, Any]:
    parent = getattr(span, "parent", None)
    parent_span_id = f"{parent.span_id:016x}" if parent is not None else None
    return {
        "name": span.name,
        "trace_id": f"{span.context.trace_id:032x}",
        "span_id": f"{span.context.span_id:016x}",
        "parent_span_id": parent_span_id,
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "attributes": _json_value(dict(span.attributes)),
        "events": [
            {
                "name": event.name,
                "timestamp_unix_nano": event.timestamp,
                "attributes": _json_value(dict(event.attributes)),
            }
            for event in span.events
        ],
        "status": {
            "code": getattr(span.status.status_code, "name", str(span.status.status_code)),
            "description": span.status.description,
        },
        "resource": _json_value(dict(span.resource.attributes)),
        "instrumentation_scope": {
            "name": span.instrumentation_scope.name,
            "version": span.instrumentation_scope.version,
        },
    }


__all__ = [
    "JsonlSpanExporter",
    "OtelHookService",
    "OtelMonitor",
    "OtelSettings",
    "otel_monitor_from_hooks",
    "otel_settings_from_config",
    "setup_otel_monitor",
]