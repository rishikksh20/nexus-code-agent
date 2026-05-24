# Sentry Monitoring Implementation Plan

Last updated: 2026-05-24

This document describes how to add Sentry monitoring to the live Nexus codebase. It intentionally focuses on the current `nexus/`, `tests/`, `README.md`, and existing docs shape; `reference_code/` was not used.

## Objective

Add rigorous runtime monitoring so failed turns, provider failures, tool errors, slow tool/model spans, and safety/approval events are attributable in Sentry without changing Nexus' core execution invariants.

The integration should answer:

- Which session, turn, trace, provider, model, mode, and tool were involved?
- Did the failure happen in app startup, provider streaming, tool execution, approval handling, MCP/plugin setup, sub-agent execution, or session persistence?
- What breadcrumbs led to the failure?
- Which failures should become Sentry issues versus low-severity breadcrumbs or metrics?
- Did monitoring avoid leaking prompts, tool outputs, API keys, local variables, and managed `.nexus/` state?

## Current Observability Baseline

Nexus already has the right local foundations:

- `nexus/hooks/` has a central `HookExecutor` and stable `HookEvent` values.
- `nexus/hooks/integration.py` wires the hook stack in one place via `setup_hooks(config)`.
- `nexus/observability/logging.py` writes JSONL runtime events and redacts common secret keys/values.
- `nexus/observability/metrics.py` aggregates prompt/tool/usage counters into `metrics.json`.
- `nexus/observability/audit.py` records approvals, denials, and mutating actions in `.nexus/audit-trail.jsonl`.
- `run_repl()`, `run_headless()`, and Textual prompt handling create `turn_id` and `trace_id` before calling `run_orchestrated_turn()`.
- `run_agent_turn()` centralizes the user-facing approval callback and records turn telemetry in session metadata.
- `Agent._execute_tool_call()` emits `PRE_TOOL_USE` and `POST_TOOL_USE` hook events with duration, `is_error`, tool identity, and correlation fields.
- `Agent._agentic_loop()` emits `NOTIFICATION` sub-events for model usage, confirmation requests, clarification requests, and tool denials.

The current gap is that JSONL/metrics are local files only. Runtime exceptions caught by `run_repl()`, `run_headless()`, and Textual are printed to the UI but not shipped anywhere. Provider `StreamEventType.ERROR` becomes an `AGENT_ERROR` event but is not captured as a remote issue. Tool results with `is_error=True` are counted locally but not triaged remotely.

## Sentry SDK Notes

The current Sentry Python SDK API supports `sentry_sdk.init()` with `dsn`, `environment`, `release`, `sample_rate`, `traces_sample_rate`, `profiles_sample_rate`, `profile_session_sample_rate`, `enable_logs`, `before_send`, `before_send_transaction`, `before_breadcrumb`, and `before_send_log`. The SDK docs note that no data is sent when no DSN is configured, and that CLI apps should not set shutdown flushing too low. The docs also expose `capture_exception`, `capture_message`, `add_breadcrumb`, `set_tag`, `set_context`, `start_transaction`, `start_span`, and `update_current_span`.

Sentry's Python OpenAI integration documentation is useful even though Nexus currently uses custom provider adapters: it confirms that AI monitoring should capture prompts, tools, tokens, and models, but treats LLM inputs/outputs as PII by default. For Nexus, default to metadata-only monitoring and make prompt/output capture opt-in.

References:

- https://getsentry.github.io/sentry-python/api.html
- https://docs.sentry.io/platforms/python/integrations/openai/
- https://docs.sentry.io/product/insights/llm-monitoring/

## Design Principles

1. Keep Sentry optional. The runtime must work with no `sentry-sdk` installed and no DSN configured.
2. Preserve approval ownership. Do not add approval callbacks back to `Agent.run()`.
3. Preserve event-driven tool resume. Do not alter `resume_tool_calls` semantics.
4. Preserve provider-safe history ordering. Monitoring must not mutate `state.history`, `working_history`, or persisted messages.
5. Keep scoping logic untouched. Sentry should not compute agent/sub-agent tool or skill visibility.
6. Treat expected operational failures differently from crashes. A file-not-found tool result is a breadcrumb/metric by default, not always a Sentry issue.
7. Redact before send. Prefer lengths, IDs, names, and status over raw prompt/tool output.
8. Keep hook handlers fast. Sentry SDK sends in the background, but Nexus hook handlers still run inline with turns.

## Proposed Files

Add:

- `nexus/observability/sentry.py`: Sentry settings, redaction callbacks, monitor service, and hook service.
- `tests/test_sentry_monitoring.py`: fake-client tests for config, hook breadcrumbs, failure capture, and redaction.

Update:

- `pyproject.toml`: add `sentry-sdk>=2.60.0` or a compatible current v2 pin. If Nexus wants a lean default install, expose it as an optional extra, but still keep the runtime import lazy.
- `nexus/config/defaults.py`: add Sentry config fields to `AgentConfig`.
- `nexus/config/loader.py`: validate Sentry fields and add `SENTRY_*` aliases if desired.
- `nexus/config/upgrade.py`: backfill new config keys.
- `nexus/cli/init.py`: document local config Sentry defaults.
- `nexus/observability/__init__.py`: export Sentry monitor helpers.
- `nexus/hooks/integration.py`: initialize/register Sentry hooks.
- `nexus/runtime/turn_runner.py`: emit turn start/end/failure signals and wrap the turn transaction.
- `nexus/runtime/agent.py`: emit provider/model error notifications and optionally model spans.
- `nexus/cli/headless.py`, `nexus/runtime/repl.py`, `nexus/ui/textual_app.py`: capture caught turn exceptions.
- `nexus/app.py`: capture startup/resource failures such as MCP connection failures.
- `nexus/cli/doctor.py`: add a Sentry readiness check.
- `README.md`: add setup instructions after the existing Observability section.

## Configuration Shape

Add these fields to `AgentConfig` in `nexus/config/defaults.py`:

```python
sentry_enabled: bool = False
sentry_dsn: str = ""
sentry_environment: str = "development"
sentry_release: str = ""
sentry_sample_rate: float = 1.0
sentry_traces_sample_rate: float = 0.1
sentry_profiles_sample_rate: float = 0.0
sentry_profile_session_sample_rate: float = 0.0
sentry_enable_logs: bool = True
sentry_send_default_pii: bool = False
sentry_include_prompts: bool = False
sentry_include_tool_outputs: bool = False
sentry_capture_tool_errors: bool = False
sentry_capture_provider_errors: bool = True
sentry_capture_mcp_errors: bool = True
sentry_max_breadcrumbs: int = 100
sentry_max_value_length: int = 4096
sentry_flush_timeout_seconds: float = 2.0
sentry_debug: bool = False
```

Because `loader._read_environment()` already maps `AGENT_<FIELD_NAME>` to config fields, these will automatically support:

```bash
AGENT_SENTRY_ENABLED=true
AGENT_SENTRY_DSN=https://public@example.ingest.sentry.io/123
AGENT_SENTRY_ENVIRONMENT=local
AGENT_SENTRY_TRACES_SAMPLE_RATE=0.25
```

Also add explicit aliases for common Sentry env vars:

```python
("SENTRY_DSN", "sentry_dsn")
("SENTRY_ENVIRONMENT", "sentry_environment")
("SENTRY_RELEASE", "sentry_release")
```

Recommended local config block:

```toml
# Sentry remote monitoring. Local JSONL logs and audit trail remain active separately.
sentry_enabled = false
sentry_dsn = ""
sentry_environment = "development"
sentry_release = ""
sentry_sample_rate = 1.0
sentry_traces_sample_rate = 0.1
sentry_profiles_sample_rate = 0.0
sentry_profile_session_sample_rate = 0.0
sentry_enable_logs = true
sentry_send_default_pii = false
sentry_include_prompts = false
sentry_include_tool_outputs = false
sentry_capture_tool_errors = false
sentry_capture_provider_errors = true
sentry_capture_mcp_errors = true
```

Validation rules:

- all sample rates must be `0.0 <= value <= 1.0`
- `sentry_flush_timeout_seconds > 0`
- `sentry_max_breadcrumbs > 0`
- `sentry_max_value_length > 0`
- Sentry is active only when `sentry_enabled` is true and `sentry_dsn` is non-empty

## Core Classes

Create `nexus/observability/sentry.py`.

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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


class SentryClientProtocol(Protocol):
    def init(self, **kwargs: Any) -> None: ...
    def is_initialized(self) -> bool: ...
    def capture_exception(self, error: BaseException) -> str | None: ...
    def capture_message(self, message: str, level: str = "info") -> str | None: ...
    def add_breadcrumb(self, **kwargs: Any) -> None: ...
    def set_tag(self, key: str, value: Any) -> None: ...
    def set_context(self, key: str, value: dict[str, Any]) -> None: ...
    def start_transaction(self, **kwargs: Any) -> Any: ...
    def start_span(self, **kwargs: Any) -> Any: ...
    def update_current_span(self, **kwargs: Any) -> None: ...
    def flush(self, timeout: float | None = None) -> bool: ...


class SentryMonitor:
    def __init__(self, settings: SentrySettings, client: SentryClientProtocol | None = None) -> None: ...
    def initialize(self) -> None: ...
    def enabled(self) -> bool: ...
    def capture_exception(self, exc: BaseException, *, context: dict[str, Any] | None = None) -> None: ...
    def capture_message(self, message: str, *, level: str = "info", context: dict[str, Any] | None = None) -> None: ...
    def breadcrumb(self, category: str, message: str, *, data: dict[str, Any] | None = None, level: str = "info") -> None: ...
    def set_runtime_context(self, payload: dict[str, Any]) -> None: ...
    def flush(self) -> None: ...
```

Implementation notes:

- `SentryMonitor.initialize()` imports `sentry_sdk` lazily so test and offline installs do not fail at import time.
- If disabled, use a no-op monitor.
- Pass `include_local_variables=False` to `sentry_sdk.init()` by default. Nexus turns may hold API keys, prompts, file contents, and tool output in locals.
- Pass `send_default_pii=settings.send_default_pii`, which defaults false.
- Pass `max_value_length=settings.max_value_length`.
- Use `before_send`, `before_send_transaction`, `before_breadcrumb`, and `before_send_log` to run the same redaction rules as `redact_payload()`.
- Set `in_app_include=["nexus"]` and `project_root=str(config.workspace_root)` so stack traces highlight Nexus frames.
- Add `LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)` if relying on Python logging records for hook/plugin/resource exceptions; otherwise keep explicit capture calls at the runtime boundaries below.
- Use `shutdown_timeout`/`flush()` for CLI/headless mode.
- Reuse the existing `nexus.models.CorrelationContext` where a typed correlation object is useful; otherwise keep hook payloads as dictionaries to match the current hook layer.

## Redaction Policy

Reuse and extend `nexus.observability.logging.redact_payload()` instead of inventing a second scrubber. Sentry payloads should never include:

- API keys, auth tokens, cookies, passwords, bearer headers, or `.env` contents
- raw prompt text unless `sentry_include_prompts = true`
- raw assistant output unless explicitly opted in
- raw tool output unless `sentry_include_tool_outputs = true`
- raw shell environment
- full MCP payloads or headers
- direct `.nexus/` session/memory contents

Default Sentry breadcrumb examples:

```json
{"category": "nexus.prompt", "message": "prompt submitted", "data": {"prompt_chars": 124}}
{"category": "nexus.tool", "message": "tool completed", "data": {"tool_name": "grep", "is_error": false, "duration_ms": 17.4}}
{"category": "nexus.model", "message": "model usage", "data": {"provider": "mistral", "model": "mistral-medium-latest", "total_tokens": 1842}}
```

Only include `prompt_preview` or `output_preview` when the matching opt-in flag is true. The existing JSONL logger can keep its short previews because it writes locally, but Sentry should default stricter.

## Hook Service

Add a hook-backed service that converts existing Nexus events into Sentry breadcrumbs, tags, contexts, and optional issues.

```python
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
```

All registered handler methods must be `async def` because `HookExecutor` awaits hook handlers.

Handler behavior:

- `USER_PROMPT_SUBMIT`: set `session_id`, `turn_id`, `trace_id`, `mode`, add prompt breadcrumb with length only.
- `PRE_TOOL_USE`: add breadcrumb and set tool tags/context.
- `POST_TOOL_USE`: add breadcrumb with duration and status; capture message only when `is_error` and `sentry_capture_tool_errors = true`.
- `NOTIFICATION model_usage`: attach usage context and breadcrumb.
- `NOTIFICATION confirmation_requested`: breadcrumb/tag for approval pressure; do not capture as an issue.
- `NOTIFICATION clarification_requested`: breadcrumb only.
- `NOTIFICATION tool_denied`: breadcrumb and optional warning message only for dangerous/high-risk denials.
- `NOTIFICATION model_error`: capture message when `sentry_capture_provider_errors = true`.
- `NOTIFICATION mcp_server_error`: capture message when `sentry_capture_mcp_errors = true`.
- `STOP`: breadcrumb and flush in headless mode.
- `TURN_START`/`TURN_END`: maintain turn context and transaction status once those hook emissions are added.

## Runtime Hook Additions

`HookEvent.TURN_START` and `HookEvent.TURN_END` already exist but are not emitted. Use them before adding new event enum values.

In `run_agent_turn()`:

- emit `TURN_START` after `turn_id` and `trace_id` are known
- emit `TURN_END` from `_finish_turn()` with:
  - `status`
  - `duration_ms`
  - `tool_calls`
  - usage fields if present
  - `session_id`, `turn_id`, `trace_id`
- wrap the main body with `try/except Exception` only to emit/capture `TURN_END` with `status = "failed"` and re-raise

Do not move approval callback ownership. The Sentry layer should observe approval requests through hooks and caught exceptions only.

## Transactions And Spans

Use Sentry tracing when `sentry_traces_sample_rate > 0`.

Recommended span hierarchy:

```text
nexus.turn                         transaction, one per user turn
  nexus.context.prepare             context build/compaction
  gen_ai.chat                       provider model stream
  nexus.tool                        one span per tool execution
  nexus.subagent                    one span per cognitive sub-agent tool execution
  nexus.mcp                         MCP server refresh/call span when applicable
```

Suggested attributes:

Turn transaction:

- `nexus.session_id`
- `nexus.turn_id`
- `nexus.trace_id`
- `nexus.mode`
- `nexus.agent_mode`
- `nexus.provider`
- `nexus.model`
- `nexus.status`
- `nexus.tool_calls`

Model span:

- `gen_ai.request.model`
- `gen_ai.system` = provider name
- `gen_ai.request.max_tokens`
- `gen_ai.request.temperature`
- `gen_ai.usage.input_tokens`
- `gen_ai.usage.output_tokens`
- `gen_ai.usage.total_tokens`

Tool span:

- `nexus.tool.name`
- `nexus.tool.source`
- `nexus.tool.origin`
- `nexus.tool.kind`
- `nexus.tool.is_mutating`
- `nexus.tool.is_error`
- `nexus.tool.duration_ms`
- `nexus.tool.call_id`

Sentry's newer SDK API prefers span `attributes`; avoid relying on deprecated `data` for new instrumentation.

## Failure Taxonomy

| Failure class | Current path | Recommended Sentry handling |
|---|---|---|
| Config load error | `_dispatch_runtime()` catches `ConfigError` before app init | Optional env-only bootstrap capture; otherwise console only |
| Provider exception thrown | `run_repl()`/`run_headless()` catches broad exception | `capture_exception()` with provider/model/session context |
| Provider stream error event | `Agent._agentic_loop()` yields `AGENT_ERROR` and returns | emit `NOTIFICATION model_error`; capture message |
| Tool returns `is_error=True` | `POST_TOOL_USE` emitted with `is_error` | breadcrumb + metrics; optional message capture |
| Tool raises exception | currently bubbles before `POST_TOOL_USE` | capture exception, emit failed `POST_TOOL_USE`, re-raise initially |
| User denies approval | `NOTIFICATION tool_denied` | breadcrumb/audit; no issue by default |
| Confirmation required | `CONFIRMATION_REQUESTED` event | breadcrumb only |
| Headless needs confirmation | `EXIT_NEEDS_CONFIRM` | breadcrumb/transaction status, not an error |
| Turn cancellation | `asyncio.CancelledError` in REPL/Textual | breadcrumb/status `cancelled`, not error |
| Sub-agent timeout | `SubAgentTool` catches timeout and returns failed `ToolResult` | breadcrumb + optional warning message |
| MCP startup failure | `NexusApp._connect_mcp_server()` logs warning | emit/capture `mcp_server_error` notification |
| Hook handler failure | `HookExecutor.emit()` logs exception | Sentry logging integration or explicit capture in executor |
| JSONL/metrics/audit write failure | observability classes log warning | breadcrumb/warning, not issue unless repeated |

## Capture Points

Add explicit capture calls at these boundaries:

1. `nexus/cli/headless.py`
   - In the `except Exception as exc` around `run_orchestrated_turn()`, call `state.sentry.capture_exception(...)` or a helper that retrieves the monitor from `state.hooks`/config.
   - Flush before returning `EXIT_ERROR`.

2. `nexus/runtime/repl.py`
   - In the `except Exception as exc` around `run_orchestrated_turn()`, capture with turn/session context.
   - Do not capture `asyncio.CancelledError` as an error.

3. `nexus/ui/textual_app.py`
   - Mirror REPL behavior in the Textual prompt task handler.

4. `nexus/runtime/agent.py`
   - On `StreamEventType.ERROR`, emit `HookEvent.NOTIFICATION` with `event = "model_error"`, provider/model if available, and correlation payload.
   - Around `tool.execute(...)`, capture exceptions and emit a failed post-tool notification before re-raising.

5. `nexus/app.py`
   - In `_connect_mcp_server()`, emit/capture MCP setup failures.
   - During `_run_app()`, flush Sentry in `finally` after `app.close()`.

6. `nexus/hooks/executor.py`
   - Option A: rely on Sentry logging integration to capture `logger.exception`.
   - Option B: add an optional monitor to `HookExecutor` and capture handler exceptions explicitly. Prefer option A first to avoid coupling hooks back to observability internals.

## Where To Store The Monitor

Avoid global state in the runtime core. Recommended approach:

- `setup_hooks(config)` creates `SentryMonitor` and registers `SentryHookService`.
- Store the monitor on the returned `HookExecutor` as a private attribute only if direct capture helpers need access:

```python
hooks = HookExecutor()
hooks.sentry_monitor = monitor  # pragmatic, typed with getattr at call sites
```

Better long-term approach:

- Add `observability_monitor: RuntimeMonitor | None` to `ReplState`.
- `RuntimeSession.create()` receives the monitor from resources or hooks.
- Direct exception capture calls use `state.monitor`.

For the first implementation, the pragmatic `getattr(state.hooks, "sentry_monitor", None)` pattern is low blast-radius and does not disturb config/session construction. If this grows, promote it to a typed `RuntimeMonitor` field.

## Recommended Setup Flow

`nexus/hooks/integration.py` should become:

```python
def setup_hooks(config: AgentConfig) -> HookExecutor:
    hooks = HookExecutor()

    monitor = setup_sentry_monitor(config)
    hooks.sentry_monitor = monitor

    register_audit_hooks(...)

    if config.log_format == "json":
        register_default_runtime_hooks(...)

    if monitor.enabled():
        SentryHookService(monitor, monitor.settings).register(hooks)

    return hooks
```

This keeps all observability wiring centralized and matches the existing JSONL/audit pattern.

## AI Monitoring Details

Nexus does not currently call the official OpenAI SDK for its OpenAI-compatible provider; it uses `urllib` in `OpenAICompatibleModelClient`. That means Sentry's automatic OpenAI integration will not see those calls. Implement manual spans in Nexus instead.

For model spans:

- Start a span around `self.model_client.chat_completion(request, stream=True)`.
- Record provider/model/config attributes.
- Accumulate `response_text` length and `tool_calls` count.
- On `MESSAGE_COMPLETE`, attach `UsageSnapshot`.
- On `StreamEventType.ERROR`, mark the span status/error and emit notification.

Do not send `request.messages` or response text by default. If later enabling Sentry's AI Agents dashboard requires specific `gen_ai.*` fields, populate the safe fields first: model, token usage, tool call names/counts, latency, status. Make raw prompts/outputs opt-in.

## Tool Monitoring Details

Current `POST_TOOL_USE` payloads already include:

- `tool_name`
- `tool_source`
- `tool_origin`
- `arguments`
- `call_id`
- `session_id`
- `is_mutating`
- `is_error`
- `duration_ms`
- `output`
- `turn_id`, `trace_id`, `tool_call_id`

For Sentry:

- redact `arguments`
- strip `output` unless `sentry_include_tool_outputs`
- tag `tool_name`, `tool_source`, `tool_origin`
- add context with `duration_ms`, `is_error`, and output length
- capture a warning/error only when configured

Important: many tool errors are normal model feedback. Examples include missing files, invalid regex, denied path, exact string not found, command non-zero exit. These should not become Sentry issues by default. They are valuable breadcrumbs attached to real crashes and turn failures.

## Sub-Agent Monitoring

`SubAgentTool` creates an inner `Agent` without passing hooks:

```python
agent = Agent(model_client=model_factory(), tool_registry=registry)
```

That means inner sub-agent model/tool behavior is currently visible through returned `ToolResult` metadata and UI rendering, but not through the shared hook stack. For better monitoring, pass the parent hook executor into sub-agent context:

- Add `hooks` to `outer_context.metadata` in `run_agent_turn()`.
- In `SubAgentTool._execute_direct()`, create the inner agent as `Agent(..., hooks=outer_context.metadata.get("hooks"))`.
- Add sub-agent identifiers already present in metadata: `subagent`, `parent_session_id`, `context_scope`, `input_packet_ids`, `active_skills`, and `active_mcp_servers`.

Be careful not to expose supervisor-only tools/skills. This change reuses the same hook executor but does not change `agent_scope.py` resource calculations.

## MCP And Plugin Monitoring

MCP startup failures are currently logged and skipped. Add Sentry breadcrumbs/messages for:

- server name
- transport
- command name only, not full env
- startup timeout
- last error
- registered/discovered tool counts

Plugin loader failures in `nexus/extensions/plugins.py` currently log warnings. Use Sentry logging integration for `ERROR` and add explicit breadcrumbs for plugin load attempts only if plugin debugging becomes painful. Do not capture every skipped plugin warning as an issue by default.

## Doctor Check

Add a doctor check under "Operational Integrity":

- pass: disabled intentionally
- warn: `sentry_enabled = true` but no DSN
- pass: enabled with DSN and `sentry_send_default_pii = false`
- warn: `sentry_send_default_pii = true`
- warn: `sentry_include_prompts = true` or `sentry_include_tool_outputs = true`

The check should not call Sentry over the network. It should validate configuration only.

## Testing Plan

Add `tests/test_sentry_monitoring.py` with a fake client; do not require network.

Unit tests:

- disabled Sentry does not import or initialize `sentry_sdk`
- enabled config calls fake client `init()` with expected options
- `before_send` redacts `api_key`, `token`, `authorization`, secret-shaped strings, prompt text, and tool output
- `SentryHookService` turns prompt/tool/model usage hooks into breadcrumbs
- `POST_TOOL_USE` with `is_error=True` does not capture when `sentry_capture_tool_errors=False`
- `POST_TOOL_USE` with `is_error=True` captures message when enabled
- `NOTIFICATION model_error` captures provider error when enabled
- `run_headless()` exception path captures and flushes
- `run_repl()` cancellation path does not capture exception
- config env aliases populate `sentry_dsn`, `sentry_environment`, and `sentry_release`
- invalid sample rates raise `ConfigError`

Integration-style local tests:

- Run a fake provider scripted to emit `StreamEventType.ERROR`; assert one Sentry message and preserved turn behavior.
- Run a tool that raises from `execute()`; assert capture and failed post-tool hook before re-raise.
- Run a tool returning `ToolResult(is_error=True)`; assert breadcrumb and metrics, no issue by default.

Run:

```bash
uv run pytest tests/test_sentry_monitoring.py tests/test_hooks.py tests/test_cli.py tests/test_repl.py
uv run pytest
```

## Implementation Phases

### Phase 1: Optional SDK And Config

- Add dependency.
- Add config fields, validation, env aliases, config upgrade, init template, README docs.
- Add `SentrySettings`, monitor, no-op behavior, and redaction callbacks.
- Add doctor config checks.

Exit criteria:

- Full test suite passes with no DSN and no network.
- `sentry_enabled=false` is completely inert.

### Phase 2: Hook Breadcrumbs And Local Parity

- Register `SentryHookService` from `setup_hooks(config)`.
- Convert current prompt/tool/usage/approval/stop hooks to Sentry breadcrumbs and contexts.
- Keep local JSONL/metrics/audit behavior unchanged.

Exit criteria:

- Existing hook tests still pass.
- Fake client shows useful breadcrumbs for a normal headless turn.

### Phase 3: Turn Failure Capture

- Emit `TURN_START` and `TURN_END` from `run_agent_turn()`.
- Capture caught exceptions in headless, REPL, and Textual paths.
- Flush Sentry on headless exit and app close.
- Add provider `model_error` notifications.

Exit criteria:

- Failed turn creates one Sentry issue with session/turn/trace tags and breadcrumbs.
- Cancellation and confirmation-required flows are not reported as crashes.

### Phase 4: Performance Spans

- Add turn transaction.
- Add model span around provider streaming.
- Add tool spans around `tool.execute()`.
- Add MCP/sub-agent spans where practical.

Exit criteria:

- A normal turn appears in Sentry traces with model and tool timing.
- Token usage attaches to the model/turn span when provider usage is available.

### Phase 5: Production Hardening

- Tune sampling.
- Add repeated-error grouping rules through tags/fingerprints if needed.
- Add release/environment setup in packaging or CI.
- Consider Sentry alerts for provider error rate, high tool failure count, and turn failure rate.

## Acceptance Checklist

- [ ] No Sentry data is sent unless explicitly enabled with a DSN.
- [ ] Sentry can be enabled entirely through env vars for deployments.
- [ ] Every captured event has `session_id`, `turn_id`, and `trace_id` when available.
- [ ] Provider/model/mode/agent_mode tags are attached.
- [ ] Tool events include tool name/source/origin/kind and `is_mutating`.
- [ ] Prompt and tool output capture is opt-in and redacted.
- [ ] `include_local_variables` is disabled by default.
- [ ] Headless failures flush before process exit.
- [ ] Approval callbacks remain centralized in `run_agent_turn()`.
- [ ] `Agent.run()` remains event-driven and uses `resume_tool_calls`.
- [ ] Provider-safe history persistence is untouched.
- [ ] Existing JSONL logs, metrics, and audit trail continue working.
- [ ] Full `uv run pytest` passes.

## Open Decisions

1. Should `sentry_capture_tool_errors` default to false or true? Recommended: false, because many tool errors are normal agent feedback.
2. Should prompt previews be sent remotely? Recommended: no by default; use only lengths and IDs.
3. Should the monitor be stored as a dynamic attribute on `HookExecutor` first, or added as a typed `ReplState` field immediately? Recommended: dynamic attribute for low-risk first patch, typed field once the integration stabilizes.
4. Should provider stream errors become `capture_message()` or synthetic exceptions? Recommended: `capture_message(level="error")` because there may be no Python exception/stack.
5. Should tool exceptions be converted to `ToolResult(is_error=True)`? Recommended: first capture and re-raise to preserve behavior, then consider conversion for specific known-safe tool failures.
