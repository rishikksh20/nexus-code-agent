"""Convenience factory for wiring the full hooks stack from config.

:func:`setup_hooks` is the single entry point used by :mod:`nexus.app`.  It:

1. Creates a fresh :class:`~nexus.hooks.executor.HookExecutor`.
2. Always registers the audit trail (dangerous-action ledger).
3. Registers runtime-logging hooks when the config requests JSON logging.

This keeps all hook wiring out of the application bootstrap and makes the
setup trivially testable by passing a mock config.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from nexus.hooks.executor import HookExecutor

if TYPE_CHECKING:
    from nexus.config.defaults import AgentConfig


def setup_hooks(config: "AgentConfig") -> HookExecutor:
    """Create and configure a :class:`HookExecutor` for the given *config*.

    The following hooks are always registered:

    * **Audit trail** — every dangerous action and confirmation event is
      appended to ``<local_root>/audit-trail.jsonl``.

    The following hooks are registered conditionally:

    * **Runtime logging** — when ``config.log_format == "json"``, all hook
      events are written to ``<log_dir>/runtime.jsonl`` and usage metrics are
      aggregated in ``<log_dir>/metrics.json``.

    Parameters
    ----------
    config:
        The active :class:`~nexus.config.defaults.AgentConfig`.

    Returns
    -------
    HookExecutor
        A fully configured executor ready to be passed to
        :class:`~nexus.runtime.agent.Agent` and :class:`~nexus.runtime.repl_state.ReplState`.
    """
    # Import here to avoid a package-level circular import; observability
    # depends on nexus.hooks (for HookEvent / HookExecutor) but integration
    # must also import observability.
    from nexus.observability import (
        JsonlAuditTrail,
        JsonlRuntimeLogger,
        LangfuseHookService,
        OtelHookService,
        RuntimeMetricsCollector,
        SentryHookService,
        register_audit_hooks,
        register_default_runtime_hooks,
        setup_langfuse_monitor,
        setup_otel_monitor,
        setup_sentry_monitor,
    )

    hooks = HookExecutor()
    monitor = setup_sentry_monitor(config)
    hooks.sentry_monitor = monitor
    langfuse_monitor = setup_langfuse_monitor(config)
    hooks.langfuse_monitor = langfuse_monitor
    otel_monitor = setup_otel_monitor(config)
    hooks.otel_monitor = otel_monitor

    # Audit trail — always active.
    register_audit_hooks(
        hooks,
        JsonlAuditTrail(config.local_root / "audit-trail.jsonl"),
    )

    # Structured runtime logging — opt-in via log_format = "json".
    if config.log_format == "json":
        register_default_runtime_hooks(
            hooks,
            JsonlRuntimeLogger(config.log_dir / "runtime.jsonl"),
            metrics_collector=RuntimeMetricsCollector(
                config.log_dir / "metrics.json"
            ),
        )

    if monitor.enabled():
        SentryHookService(monitor, monitor.settings).register(hooks)
    if langfuse_monitor.enabled():
        LangfuseHookService(langfuse_monitor, langfuse_monitor.settings).register(hooks)
    if otel_monitor.enabled():
        OtelHookService(otel_monitor, otel_monitor.settings).register(hooks)

    return hooks
