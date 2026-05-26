from __future__ import annotations

import logging
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from nexus.hooks import HookEvent, HookExecutor
from nexus.observability.logging import redact_payload


logger = logging.getLogger(__name__)

_SUPPRESSED_BY_DEFAULT_KEYS = {
    "prompt",
    "effective_prompt",
    "system_prompt",
    "messages",
    "response",
    "output",
    "raw_output",
    "tool_output",
    "assistant_output",
    "content",
    "shared_context",
    "multi_agent_packet_summaries",
}
_PROMPT_KEYS = {"prompt", "effective_prompt", "system_prompt", "messages", "content"}
_TOOL_OUTPUT_KEYS = {"output", "raw_output", "tool_output", "assistant_output", "shared_context"}


@dataclass(slots=True, frozen=True)
class SentrySettings:
    enabled: bool
    dsn: str
    environment: str
    release: str
    sample_rate: float
    traces_sample_rate: float
    profiles_sample_rate: float
    profile_session_sample_rate: float
    enable_logs: bool
    send_default_pii: bool
    include_prompts: bool
    include_tool_outputs: bool
    capture_tool_errors: bool
    capture_provider_errors: bool
    capture_mcp_errors: bool
    max_breadcrumbs: int
    max_value_length: int
    flush_timeout_seconds: float
    debug: bool
    project_root: str = ""

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.dsn.strip())


class SentryClientProtocol(Protocol):
    def init(self, **kwargs: Any) -> None: ...
    def capture_exception(self, error: BaseException) -> str | None: ...
    def capture_message(self, message: str, level: str = "info") -> str | None: ...
    def add_breadcrumb(self, **kwargs: Any) -> None: ...
    def set_tag(self, key: str, value: Any) -> None: ...
    def set_context(self, key: str, value: dict[str, Any]) -> None: ...
    def start_transaction(self, **kwargs: Any) -> Any: ...
    def start_span(self, **kwargs: Any) -> Any: ...
    def update_current_span(self, **kwargs: Any) -> None: ...
    def flush(self, timeout: float | None = None) -> bool: ...


class _SentrySDKClient:
    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def init(self, **kwargs: Any) -> None:
        self._sdk.init(**kwargs)

    def capture_exception(self, error: BaseException) -> str | None:
        return self._sdk.capture_exception(error)

    def capture_message(self, message: str, level: str = "info") -> str | None:
        return self._sdk.capture_message(message, level=level)

    def add_breadcrumb(self, **kwargs: Any) -> None:
        self._sdk.add_breadcrumb(**kwargs)

    def set_tag(self, key: str, value: Any) -> None:
        self._sdk.set_tag(key, value)

    def set_context(self, key: str, value: dict[str, Any]) -> None:
        self._sdk.set_context(key, value)

    def start_transaction(self, **kwargs: Any) -> Any:
        return self._sdk.start_transaction(**kwargs)

    def start_span(self, **kwargs: Any) -> Any:
        return self._sdk.start_span(**kwargs)

    def update_current_span(self, **kwargs: Any) -> None:
        update = getattr(self._sdk, "update_current_span", None)
        if update is not None:
            update(**kwargs)

    def flush(self, timeout: float | None = None) -> bool:
        return bool(self._sdk.flush(timeout=timeout))


class SentryMonitor:
    def __init__(self, settings: SentrySettings, client: SentryClientProtocol | None = None) -> None:
        self.settings = settings
        self._client = client
        self._initialized = False

    def initialize(self) -> None:
        if not self.settings.active:
            return
        if self._client is None:
            try:
                import sentry_sdk  # type: ignore[import-not-found]
            except ImportError:
                logger.warning("Sentry is enabled but sentry-sdk is not installed.")
                return
            self._client = _SentrySDKClient(sentry_sdk)

        self._client.init(
            dsn=self.settings.dsn,
            environment=self.settings.environment,
            release=self.settings.release or None,
            sample_rate=self.settings.sample_rate,
            traces_sample_rate=self.settings.traces_sample_rate,
            profiles_sample_rate=self.settings.profiles_sample_rate,
            profile_session_sample_rate=self.settings.profile_session_sample_rate,
            enable_logs=self.settings.enable_logs,
            send_default_pii=self.settings.send_default_pii,
            max_breadcrumbs=self.settings.max_breadcrumbs,
            max_value_length=self.settings.max_value_length,
            debug=self.settings.debug,
            include_local_variables=False,
            in_app_include=["nexus"],
            project_root=self.settings.project_root or None,
            before_send=self._before_send,
            before_send_transaction=self._before_send,
            before_breadcrumb=self._before_breadcrumb,
            before_send_log=self._before_send,
        )
        self._initialized = True

    def enabled(self) -> bool:
        return self._initialized and self._client is not None

    def capture_exception(self, exc: BaseException, *, context: dict[str, Any] | None = None) -> str | None:
        if not self.enabled():
            return None
        if context:
            self.set_runtime_context(context)
        return self._client.capture_exception(exc)

    def capture_message(
        self,
        message: str,
        *,
        level: str = "info",
        context: dict[str, Any] | None = None,
    ) -> str | None:
        if not self.enabled():
            return None
        if context:
            self.set_runtime_context(context)
        return self._client.capture_message(message, level=level)

    def breadcrumb(
        self,
        category: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        if not self.enabled():
            return
        self._client.add_breadcrumb(
            category=category,
            message=message,
            data=_scrub_value(data or {}, self.settings),
            level=level,
        )

    def set_runtime_context(self, payload: dict[str, Any]) -> None:
        if not self.enabled():
            return
        scrubbed = _scrub_value(payload, self.settings)
        if not isinstance(scrubbed, dict):
            return
        for key in (
            "session_id",
            "turn_id",
            "trace_id",
            "provider",
            "model",
            "mode",
            "agent_mode",
            "verification_id",
            "event_type",
            "verification_kind",
        ):
            value = scrubbed.get(key)
            if value not in (None, ""):
                self._client.set_tag(f"nexus.{key}", value)
        self._client.set_context("nexus", scrubbed)

    def start_transaction(self, **kwargs: Any) -> Any:
        if not self.enabled() or self.settings.traces_sample_rate <= 0:
            return nullcontext()
        scrubbed = _scrub_value(kwargs, self.settings)
        try:
            return self._client.start_transaction(**scrubbed)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to start Sentry transaction: %s", exc)
            return nullcontext()

    def start_span(self, **kwargs: Any) -> Any:
        if not self.enabled() or self.settings.traces_sample_rate <= 0:
            return nullcontext()
        scrubbed = _scrub_value(kwargs, self.settings)
        try:
            return self._client.start_span(**scrubbed)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to start Sentry span: %s", exc)
            return nullcontext()

    def update_current_span(self, **kwargs: Any) -> None:
        if not self.enabled():
            return
        try:
            self._client.update_current_span(**_scrub_value(kwargs, self.settings))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to update Sentry span: %s", exc)

    def flush(self) -> None:
        if not self.enabled():
            return
        self._client.flush(timeout=self.settings.flush_timeout_seconds)

    def _before_send(self, event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
        scrubbed = _scrub_value(event, self.settings)
        return scrubbed if isinstance(scrubbed, dict) else {}

    def _before_breadcrumb(
        self,
        breadcrumb: dict[str, Any],
        hint: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        scrubbed = _scrub_value(breadcrumb, self.settings)
        return scrubbed if isinstance(scrubbed, dict) else None


class SentryHookService:
    def __init__(self, monitor: SentryMonitor, settings: SentrySettings) -> None:
        self.monitor = monitor
        self.settings = settings

    def register(self, hooks: HookExecutor) -> None:
        hooks.register(HookEvent.USER_PROMPT_SUBMIT, self.on_user_prompt)
        hooks.register(HookEvent.PRE_TOOL_USE, self.on_pre_tool)
        hooks.register(HookEvent.POST_TOOL_USE, self.on_post_tool)
        hooks.register(HookEvent.NOTIFICATION, self.on_notification)
        hooks.register(HookEvent.STOP, self.on_stop)
        hooks.register(HookEvent.TURN_START, self.on_turn_start)
        hooks.register(HookEvent.TURN_END, self.on_turn_end)

    async def on_user_prompt(self, payload: dict[str, Any]) -> None:
        prompt = str(payload.get("prompt", ""))
        context = _base_context(payload)
        context.update({"mode": payload.get("mode"), "headless": payload.get("headless")})
        self.monitor.set_runtime_context(context)
        data = {**context, "prompt_chars": len(prompt)}
        if self.settings.include_prompts:
            data["prompt_preview"] = prompt[:200]
        self.monitor.breadcrumb("nexus.prompt", "prompt submitted", data=data)

    async def on_pre_tool(self, payload: dict[str, Any]) -> None:
        context = _base_context(payload)
        context.update(_tool_context(payload))
        self.monitor.set_runtime_context(context)
        self.monitor.breadcrumb("nexus.tool", "tool started", data=context)

    async def on_post_tool(self, payload: dict[str, Any]) -> None:
        output = str(payload.get("output", ""))
        context = _base_context(payload)
        context.update(_tool_context(payload))
        context.update(
            {
                "is_error": bool(payload.get("is_error")),
                "duration_ms": payload.get("duration_ms"),
                "output_chars": len(output),
                "exception_type": payload.get("exception_type"),
            }
        )
        if self.settings.include_tool_outputs:
            context["output_preview"] = output[:500]
        level = "error" if payload.get("is_error") else "info"
        self.monitor.breadcrumb("nexus.tool", "tool completed", data=context, level=level)
        if payload.get("is_error") and self.settings.capture_tool_errors:
            tool_name = str(payload.get("tool_name", "unknown"))
            self.monitor.capture_message(f"Nexus tool error: {tool_name}", level="warning", context=context)

    async def on_notification(self, payload: dict[str, Any]) -> None:
        event_name = str(payload.get("event", "")).strip()
        context = _base_context(payload)
        if event_name == "model_usage":
            context.update(
                {
                    "provider": payload.get("provider"),
                    "model": payload.get("model"),
                    "prompt_tokens": payload.get("prompt_tokens"),
                    "completion_tokens": payload.get("completion_tokens"),
                    "total_tokens": payload.get("total_tokens"),
                    "estimated_cost_usd": payload.get("estimated_cost_usd"),
                }
            )
            self.monitor.set_runtime_context(context)
            self.monitor.breadcrumb("nexus.model", "model usage", data=context)
            return
        if event_name in {"confirmation_requested", "clarification_requested", "tool_denied"}:
            context.update(_tool_context(payload))
            context.update(
                {
                    "event": event_name,
                    "reason": payload.get("reason"),
                    "field": payload.get("field"),
                    "risk_level": payload.get("risk_level"),
                }
            )
            level = "warning" if event_name == "tool_denied" else "info"
            self.monitor.breadcrumb(f"nexus.{event_name}", event_name.replace("_", " "), data=context, level=level)
            if event_name == "tool_denied" and str(payload.get("risk_level", "")).lower() in {"high", "dangerous"}:
                self.monitor.capture_message("Nexus high-risk tool denial", level="warning", context=context)
            return
        if event_name == "model_error":
            context.update(
                {
                    "provider": payload.get("provider"),
                    "model": payload.get("model"),
                    "turn_index": payload.get("turn_index"),
                    "actor": payload.get("actor"),
                    "error": payload.get("error"),
                }
            )
            self.monitor.breadcrumb("nexus.model", "model error", data=context, level="error")
            if self.settings.capture_provider_errors:
                self.monitor.capture_message("Nexus provider stream error", level="error", context=context)
            return
        if event_name == "mcp_server_error":
            context.update(
                {
                    "server_name": payload.get("server_name"),
                    "transport": payload.get("transport"),
                    "command_name": payload.get("command_name"),
                    "error": payload.get("error"),
                }
            )
            self.monitor.breadcrumb("nexus.mcp", "mcp server error", data=context, level="warning")
            if self.settings.capture_mcp_errors:
                self.monitor.capture_message("Nexus MCP server error", level="warning", context=context)
            return
        self.monitor.breadcrumb("nexus.notification", event_name or "notification", data={**context, "event": event_name})

    async def on_stop(self, payload: dict[str, Any]) -> None:
        self.monitor.breadcrumb("nexus.stop", "runtime stopped", data=payload)
        if payload.get("headless"):
            self.monitor.flush()

    async def on_turn_start(self, payload: dict[str, Any]) -> None:
        self.monitor.set_runtime_context(payload)
        self.monitor.breadcrumb("nexus.turn", "turn started", data=payload)

    async def on_turn_end(self, payload: dict[str, Any]) -> None:
        self.monitor.set_runtime_context(payload)
        level = "error" if payload.get("status") == "failed" else "info"
        self.monitor.breadcrumb("nexus.turn", "turn ended", data=payload, level=level)
        status = str(payload.get("status", ""))
        if status:
            self.monitor.update_current_span(status=status)


def sentry_settings_from_config(config: Any) -> SentrySettings:
    return SentrySettings(
        enabled=bool(getattr(config, "sentry_enabled", False)),
        dsn=str(getattr(config, "sentry_dsn", "") or ""),
        environment=str(getattr(config, "sentry_environment", "development") or "development"),
        release=str(getattr(config, "sentry_release", "") or ""),
        sample_rate=float(getattr(config, "sentry_sample_rate", 1.0)),
        traces_sample_rate=float(getattr(config, "sentry_traces_sample_rate", 0.1)),
        profiles_sample_rate=float(getattr(config, "sentry_profiles_sample_rate", 0.0)),
        profile_session_sample_rate=float(getattr(config, "sentry_profile_session_sample_rate", 0.0)),
        enable_logs=bool(getattr(config, "sentry_enable_logs", True)),
        send_default_pii=bool(getattr(config, "sentry_send_default_pii", False)),
        include_prompts=bool(getattr(config, "sentry_include_prompts", False)),
        include_tool_outputs=bool(getattr(config, "sentry_include_tool_outputs", False)),
        capture_tool_errors=bool(getattr(config, "sentry_capture_tool_errors", False)),
        capture_provider_errors=bool(getattr(config, "sentry_capture_provider_errors", True)),
        capture_mcp_errors=bool(getattr(config, "sentry_capture_mcp_errors", True)),
        max_breadcrumbs=int(getattr(config, "sentry_max_breadcrumbs", 100)),
        max_value_length=int(getattr(config, "sentry_max_value_length", 4096)),
        flush_timeout_seconds=float(getattr(config, "sentry_flush_timeout_seconds", 2.0)),
        debug=bool(getattr(config, "sentry_debug", False)),
        project_root=str(getattr(config, "workspace_root", "") or ""),
    )


def setup_sentry_monitor(config: Any, client: SentryClientProtocol | None = None) -> SentryMonitor:
    monitor = SentryMonitor(sentry_settings_from_config(config), client=client)
    monitor.initialize()
    return monitor


def sentry_monitor_from_hooks(hooks: HookExecutor | None) -> SentryMonitor | None:
    monitor = getattr(hooks, "sentry_monitor", None) if hooks is not None else None
    return monitor if isinstance(monitor, SentryMonitor) else None


def capture_exception_from_hooks(
    hooks: HookExecutor | None,
    exc: BaseException,
    *,
    context: dict[str, Any] | None = None,
) -> None:
    monitor = sentry_monitor_from_hooks(hooks)
    if monitor is not None:
        monitor.capture_exception(exc, context=context)


def describe_sentry_dsn(dsn: str) -> str:
    parsed = urlparse(dsn.strip())
    host = parsed.hostname or "unknown-host"
    path_parts = [part for part in parsed.path.split("/") if part]
    project_id = path_parts[-1] if path_parts else "unknown-project"
    return f"host={host} project={project_id}"


def _base_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in ("session_id", "turn_id", "trace_id", "tool_call_id", "worker_id")
        if payload.get(key) not in (None, "")
    }


def _tool_context(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in (
            "tool_name",
            "tool_source",
            "tool_origin",
            "call_id",
            "is_mutating",
        )
        if payload.get(key) not in (None, "")
    }


def _scrub_value(value: Any, settings: SentrySettings) -> Any:
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _SUPPRESSED_BY_DEFAULT_KEYS:
                if normalized in _PROMPT_KEYS and settings.include_prompts:
                    scrubbed[key] = _scrub_value(item, settings)
                elif normalized in _TOOL_OUTPUT_KEYS and settings.include_tool_outputs:
                    scrubbed[key] = _scrub_value(item, settings)
                else:
                    scrubbed[f"{key}_chars"] = len(str(item)) if item is not None else 0
                continue
            scrubbed[key] = _scrub_value(item, settings)
        return redact_payload(scrubbed)
    if isinstance(value, list):
        return [_scrub_value(item, settings) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, settings) for item in value)
    if isinstance(value, str):
        return redact_payload({"value": value})["value"]
    return value


__all__ = [
    "SentryClientProtocol",
    "SentryHookService",
    "SentryMonitor",
    "SentrySettings",
    "capture_exception_from_hooks",
    "describe_sentry_dsn",
    "sentry_monitor_from_hooks",
    "sentry_settings_from_config",
    "setup_sentry_monitor",
]
