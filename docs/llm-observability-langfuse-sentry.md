# OpenTelemetry, Langfuse, And Sentry Observability

Nexus now has three separate observability layers with different purposes:

- Local JSONL logs and metrics stay on disk for deterministic workspace-level debugging.
- OpenTelemetry is the tracing layer: Nexus writes spans to `~/.nexus/logs/traces.jsonl` and can export the same spans over OTLP.
- Langfuse is an OTLP backend option: it receives those spans for session-level visualization, prompt/input/output review, tool timelines, and turn debugging.
- Sentry remains the runtime incident system: exceptions, crashes, provider failures, tool failures, MCP startup failures, breadcrumbs, and performance spans.

Nexus keeps prompts local. It does not import prompts from Langfuse. Prompt metadata and optional content are emitted from the local Nexus runtime only.

## Trace Mapping

- Langfuse session: Nexus `session_id`
- Langfuse trace: one Nexus user turn using Nexus `trace_id`
- Root span: `nexus.turn`
- Model span: `nexus.model` for each model call inside the turn
- Tool span: each tool call
- Event span: notifications, confirmations, denials, clarifications, context compaction, warnings, and errors

## Environment Variables

You can configure tracing in two ways.

Direct OTLP:

```bash
AGENT_OTEL_ENABLED=true
OTEL_SERVICE_NAME=nexus
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer your-token
AGENT_OTEL_ENVIRONMENT=development
AGENT_OTEL_RELEASE=nexus@local
```

Langfuse compatibility mode:

```bash
AGENT_LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_ENVIRONMENT=development
LANGFUSE_RELEASE=nexus@local
```

Notes:

- `AGENT_OTEL_ENABLED` turns on the tracing layer.
- `OTEL_EXPORTER_OTLP_ENDPOINT` may point at a collector root or a path prefix; Nexus appends `/v1/traces` when needed.
- `OTEL_EXPORTER_OTLP_HEADERS` accepts a comma-separated `key=value` list.
- `OTEL_SERVICE_NAME` controls the service label in the remote backend.
- `LANGFUSE_BASE_URL` defaults to `https://cloud.langfuse.com`.
- Use `https://us.cloud.langfuse.com` or your self-hosted URL when needed.
- `AGENT_LANGFUSE_ENABLED` is a compatibility switch. When enabled, Nexus derives the Langfuse OTLP endpoint and a Basic auth header from the `LANGFUSE_*` values.

## Nexus Config Keys

```toml
otel_enabled = true
otel_endpoint = ""
otel_headers = ""
otel_service_name = "nexus"
otel_environment = "development"
otel_release = ""
otel_trace_content = true
otel_trace_tool_outputs = true
otel_prompt_name = "nexus-system-prompt"
otel_prompt_version = ""
otel_jsonl_enabled = true
otel_flush_timeout_seconds = 2.0

# Optional Langfuse compatibility
langfuse_enabled = true
langfuse_public_key = ""
langfuse_secret_key = ""
langfuse_base_url = "https://cloud.langfuse.com"
langfuse_environment = "development"
langfuse_release = ""
```

## Event Coverage

- `TURN_START` and `TURN_END` create and close the root turn span.
- `USER_PROMPT_SUBMIT` provides the turn input.
- `model_start` and `model_end` notifications create and complete `nexus.model` spans.
- `PRE_TOOL_USE` and `POST_TOOL_USE` create tool spans.
- `CONTEXT_COMPACTION` records pruning and compaction summaries.
- Warnings and errors logged through the `nexus` logger during an active turn are added as session events.
- `model_error` closes the model span with error status and emits a notification event span.

## Setup

```bash
uv sync --extra observability
```

Then enable tracing in `.nexus/config.toml` or via env vars.

## Verification

```bash
pytest tests/test_tracing_observability.py -q
pytest tests/test_langfuse_observability.py -q
pytest tests/test_sentry_monitoring.py -q
```