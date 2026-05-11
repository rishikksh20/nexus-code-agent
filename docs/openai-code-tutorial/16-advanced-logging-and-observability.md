# 16 — Advanced Logging and Observability: Token Cost, Model Configuration, and Tracing

## Prerequisites

Complete [15-advanced-context-and-storage.md](15-advanced-context-and-storage.md) first.

At this point, your tutorial harness can do real work:

- it keeps session history,
- calls tools,
- enforces permissions,
- writes an audit trail,
- saves memory,
- and gradually learns about the workspace and the user.

But there is still a major production gap:

> you can run the harness, but you still cannot **observe it well**.

When something goes wrong, expensive, slow, or confusing, you want answers to questions like:

- Which model and provider were active?
- How many input and output tokens did this turn consume?
- What did that cost?
- Which tool calls were slow?
- Did a failure happen in the model call, the permission layer, or the tool runtime?
- Which session and turn should this log line be correlated with?
- Are we logging useful runtime information without leaking secrets?

This chapter adds a proper observability layer so the harness is no longer a black box.

---

## What you will build

```text
agent/
    telemetry.py        # NEW: UsageSnapshot+, cost estimation, telemetry records
    logging.py          # NEW: structured JSON logging, redaction, correlation IDs
    hooks.py            # updated: TelemetryHook / logging hook examples
    agent.py            # updated: emit turn + tool telemetry
    config.py           # updated: LoggingConfig / TelemetryConfig / PricingConfig
    session.py          # updated: persist summarized usage + cost per session
    prompts.py          # optional: attach prompt-size estimates before calls

agent.toml             # updated: [logging], [telemetry], [pricing]

.agent/
    logs/
        runtime.jsonl   # NEW: newline-delimited structured runtime logs
        errors.jsonl    # NEW: optional error-only stream
    traces/             # optional local span export or debug dumps
```

By the end of the chapter, your harness will be able to:

1. emit **structured logs** instead of ad-hoc print statements,
2. record **model configuration safely** without logging secrets,
3. track **input tokens, output tokens, total tokens, and estimated cost**,
4. attach **session ids, turn ids, and tool ids** to every record,
5. export telemetry using **OpenTelemetry** when you want distributed traces,
6. and still work with lighter alternatives like standard `logging`, JSONL, or `structlog`.

---

## 1. Four different things: logs, traces, metrics, and audit trail

Before writing code, separate four concepts that are often mixed together:

### Structured logs

These are timestamped event records optimized for debugging and operational review.

Examples:

- model call started,
- tool execution completed,
- permission denied,
- session saved,
- cost estimate updated.

Logs are usually the first thing you inspect when debugging a bad session.

### Traces

Traces show how one request or one turn flowed through the system over time.

A trace answers questions like:

- this user turn took 7.1 seconds — where was the time spent?
- was latency in the provider call or the tool call?
- did the turn branch into multiple worker tasks?
- which downstream spans belong to the same user request?

This is where OpenTelemetry shines.

### Metrics

Metrics are aggregated numbers optimized for dashboards and alerting.

Examples:

- average tool latency,
- tokens per session,
- cost per day,
- permission-denial count,
- sessions with compaction triggered,
- model errors per provider.

### Audit trail

Audit is not the same as observability.

An audit trail is about **durable accountability**:

- what dangerous action was requested,
- who approved it,
- what was denied,
- what tool mutated the filesystem,
- what rule caused the block.

Your Chapter 13 `AuditTrail` remains important, but it is not enough on its own to explain operational behavior.

### The rule of thumb

Use each layer for what it is good at:

- **audit trail** for safety/accountability,
- **structured logs** for debugging,
- **metrics** for trends/alerts,
- **traces** for latency/correlation.

---

## 2. Design goals for observability in an agent harness

A good observability layer for an agent runtime should satisfy all of the following:

### Goal A — Every record is correlated

Every important event should carry enough identifiers to reconstruct the path of execution.

At minimum:

- `session_id`
- `turn_id`
- `trace_id` or `correlation_id`
- `tool_call_id` when relevant
- `worker_id` or `agent_id` in swarm scenarios

### Goal B — Model config is visible, but secrets are not

You want to know:

- provider,
- model,
- profile,
- base URL,
- timeout,
- max tokens,
- permission mode,
- compaction thresholds,
- sandbox mode,
- and key runtime flags.

You must **not** log:

- API keys,
- auth tokens,
- full cookies,
- raw `Authorization` headers,
- or sensitive environment variables.

### Goal C — Cost is visible per turn and per session

Every serious harness eventually needs answers to questions like:

- why did this session get expensive?
- which model consumed the most tokens?
- which tools caused repeated loops?
- what was the cumulative cost over the session?

### Goal D — Logs are machine-readable first

Human-readable logs are nice. Machine-readable logs are necessary.

Prefer:

- JSON lines,
- stable field names,
- explicit event kinds,
- and consistent value types.

### Goal E — The same model data powers logs, metrics, and traces

Do not build separate ad-hoc usage tracking for each output surface.

Instead:

- capture a canonical usage snapshot once,
- enrich it with pricing and config,
- fan it out to logs, traces, metrics, session summary, and UI.

---

## 3. What to capture on every turn

A useful advanced logging system usually records a structured payload like this.

```json
{
  "timestamp": "2026-04-25T14:12:09.554Z",
  "event": "agent.turn.completed",
  "session_id": "sess_20260425_141200_7b9f",
  "turn_id": "turn_0008",
  "trace_id": "53f86a2f4b4f0c3d",
  "provider": "anthropic",
  "profile": "claude-api",
  "model": "claude-sonnet-4-6",
  "base_url": "https://api.anthropic.com",
  "permission_mode": "default",
  "sandbox_enabled": true,
  "tool_calls": 3,
  "duration_ms": 7142,
  "usage": {
    "input_tokens": 5210,
    "output_tokens": 811,
    "total_tokens": 6021
  },
  "cost": {
    "estimated_usd": 0.0278,
    "pricing_source": "agent.toml"
  },
  "status": "ok"
}
```

That single event is enough to support:

- debugging,
- postmortems,
- usage dashboards,
- simple budget alerts,
- and session review.

### Recommended event families

Use stable event names. For example:

- `agent.turn.started`
- `agent.turn.completed`
- `agent.turn.failed`
- `agent.model.requested`
- `agent.model.completed`
- `agent.tool.started`
- `agent.tool.completed`
- `agent.tool.failed`
- `agent.permission.denied`
- `agent.confirmation.requested`
- `agent.compaction.triggered`
- `agent.session.saved`
- `agent.session.closed`

This matters more than people think. Stable event names make logs queryable.

---

## 4. Upgrade the usage model: tokens alone are not enough

Earlier chapters can get away with a tiny usage model. For advanced logging, you want a richer version.

## 4.1 Create `agent/telemetry.py`

```python
# agent/telemetry.py

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(slots=True, frozen=True)
class UsageSnapshot:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cached_input_tokens
            + self.reasoning_tokens
        )


@dataclass(slots=True, frozen=True)
class PricingRule:
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal
    cached_input_per_million_usd: Decimal = Decimal("0")
    reasoning_per_million_usd: Decimal = Decimal("0")
    source: str = "config"


@dataclass(slots=True, frozen=True)
class CostEstimate:
    estimated_usd: Decimal
    pricing_source: str


def estimate_cost(model: str, usage: UsageSnapshot, pricing: dict[str, PricingRule]) -> CostEstimate | None:
    rule = pricing.get(model)
    if rule is None:
        return None

    total = (
        (Decimal(usage.input_tokens) / Decimal(1_000_000)) * rule.input_per_million_usd
        + (Decimal(usage.output_tokens) / Decimal(1_000_000)) * rule.output_per_million_usd
        + (Decimal(usage.cached_input_tokens) / Decimal(1_000_000)) * rule.cached_input_per_million_usd
        + (Decimal(usage.reasoning_tokens) / Decimal(1_000_000)) * rule.reasoning_per_million_usd
    )
    return CostEstimate(estimated_usd=total, pricing_source=rule.source)
```

### Why this richer shape helps

Because modern providers do not all report usage the same way.

Some may eventually expose:

- cached prompt tokens,
- reasoning or thinking tokens,
- audio tokens,
- image tokens,
- or vendor-specific usage categories.

If you keep your internal shape extensible early, you avoid rewriting your observability layer later.

### Important note about mutability

If you use `frozen=True`, avoid exposing a mutable `dict` that callers can modify after construction.

Safer options are:

- drop `frozen=True`,
- store provider metadata as an immutable mapping,
- or return a read-only wrapper.

That matters even more in telemetry than elsewhere because logs should reflect past facts, not mutable future state.

---

## 5. Track model configuration safely

Token data without runtime configuration is incomplete. When you investigate a costly or broken turn, you need to know what the model was asked to do and under which runtime settings.

## 5.1 Define a model-runtime snapshot

```python
# agent/telemetry.py

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ModelRuntimeConfig:
    provider: str
    profile: str | None
    model: str
    base_url: str | None
    api_format: str
    max_tokens: int
    timeout_seconds: float
    context_window_tokens: int | None
    auto_compact_threshold_tokens: int | None
    permission_mode: str
    sandbox_enabled: bool
    effort: str | None = None
    passes: int | None = None
```

### Log these fields

These fields are operationally useful and normally safe:

- provider name,
- provider profile name,
- resolved model id,
- API format,
- base URL,
- timeout,
- max tokens,
- compaction threshold,
- permission mode,
- sandbox enabled/disabled,
- effort/passes if your harness uses reasoning modes.

### Never log these fields

Do not log:

- `api_key`
- `Authorization`
- raw auth material
- cookies
- headers containing credentials
- environment variable dumps
- unredacted prompt text in environments with sensitive data

### Practical redaction rule

If a value is not strictly necessary to debug runtime behavior, do not log it by default.

---

## 6. Create a structured logger with redaction

You want one logger that can write predictable JSON to disk or stdout.

## 6.1 Create `agent/logging.py`

```python
# agent/logging.py

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REDACT_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "access_token",
    "refresh_token",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if k.lower() in REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra_payload = getattr(record, "payload", None)
        if isinstance(extra_payload, dict):
            payload.update(_redact(extra_payload))
        return json.dumps(payload, ensure_ascii=False)


def build_json_logger(name: str, log_path: Path) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    logger.propagate = False
    return logger
```

### Why JSONL is a good default

JSON Lines is a sweet spot for local harnesses because it is:

- append-friendly,
- easy to grep,
- easy to stream into collectors later,
- readable enough for humans,
- and simple to parse with Python, `jq`, or ingestion tools.

---

## 7. Log turn telemetry as first-class records

Now define a record type that can be emitted at turn boundaries.

```text
# agent/telemetry.py

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnTelemetry:
    session_id: str
    turn_id: str
    trace_id: str
    event: str
    runtime: ModelRuntimeConfig
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    tool_calls: int = 0
    duration_ms: int = 0
    status: str = "ok"
    error_type: str | None = None
    error_message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_log_payload(self, pricing: dict[str, PricingRule]) -> dict[str, Any]:
        payload = {
            "event": self.event,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "runtime": asdict(self.runtime),
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cached_input_tokens": self.usage.cached_input_tokens,
                "reasoning_tokens": self.usage.reasoning_tokens,
                "total_tokens": self.usage.total_tokens,
            },
            "tool_calls": self.tool_calls,
            "duration_ms": self.duration_ms,
            "status": self.status,
        }
        estimate = estimate_cost(self.runtime.model, self.usage, pricing)
        if estimate is not None:
            payload["cost"] = {
                "estimated_usd": str(estimate.estimated_usd),
                "pricing_source": estimate.pricing_source,
            }
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.error_message:
            payload["error_message"] = self.error_message
        payload.update(self.extra)
        return payload
```

### What this buys you

This gives you one canonical payload that can be:

- written to JSONL,
- attached to an OpenTelemetry span,
- summarized into session metadata,
- displayed in a TUI status panel,
- or exported to a metrics backend.

---

## 8. Add a pricing table to configuration

A major mistake is to scatter model prices across random helper functions.

Instead, keep pricing in configuration.

## 8.1 Add config to `agent.toml`

```toml
[logging]
level = "INFO"
format = "json"
path = ".agent/logs/runtime.jsonl"
log_model_config = true
log_token_usage = true
log_tool_args = false
redact_fields = ["api_key", "authorization", "cookie", "token"]

[telemetry]
enabled = true
write_jsonl = true
otel_enabled = false
otel_service_name = "minimal-agent-harness"
otel_exporter = "otlp"
otel_endpoint = "http://localhost:4318"
otel_sample_rate = 1.0

[pricing.models."claude-sonnet-4-6"]
input_per_million_usd = "3.00"
output_per_million_usd = "15.00"
source = "manual-config"

[pricing.models."gpt-5.4"]
input_per_million_usd = "5.00"
output_per_million_usd = "15.00"
source = "manual-config"
```

### Important pricing note

These values are only examples.

Provider pricing changes over time. The important architectural lesson is:

> keep pricing in one explicit config surface, and record the pricing source in logs.

That way, later session reviews can say not only “estimated cost was $0.07” but also “that estimate was based on pricing table version X.”

---

## 9. Emit logs at the right boundaries

Do not only log “session started” and “session ended.” That is too coarse.

You want logs at **meaningful execution boundaries**.

### Recommended boundaries

#### Before model call

Log:

- session id,
- turn id,
- model config,
- compacted-context estimate if available,
- number of messages in history,
- whether tools are exposed.

#### After model call

Log:

- latency,
- stop reason if available,
- usage snapshot,
- number of tool calls returned,
- whether the turn finished or continued.

#### Before tool execution

Log:

- tool name,
- tool call id,
- safety classification,
- confirmation requirement,
- path or command target if safe to record.

#### After tool execution

Log:

- duration,
- success/failure,
- recoverable vs terminal error,
- output size or truncation indicator,
- not the full raw output by default.

#### On permission or guardrail block

Log:

- rule name,
- decision,
- requested action category,
- why it was blocked.

#### At session close

Log:

- total usage,
- total estimated cost,
- number of turns,
- number of tool calls,
- number of denials/confirmations,
- session persistence result.

---

## 10. OpenTelemetry integration

If you want traces that can flow into systems like Grafana Tempo, Jaeger, Honeycomb, or another OTLP-compatible backend, OpenTelemetry is the cleanest vendor-neutral option.

## 10.1 Optional dependencies

A minimal setup normally looks like:

```bash
pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp
```

For metrics, you may later add more exporters depending on your deployment.

### Keep it optional

As of the current repository state, OpenTelemetry is **not** a baseline dependency in `pyproject.toml`. That is fine.

The right design is:

- the harness works with plain JSON logs by default,
- OpenTelemetry is an optional exporter layer,
- the core telemetry model is library-agnostic.

## 10.2 Create a span around each turn

```text
# agent/logging.py or agent/telemetry.py

from __future__ import annotations

from contextlib import nullcontext

try:
    from opentelemetry import trace
except ImportError:
    trace = None


class TelemetrySpanFactory:
    def __init__(self, service_name: str) -> None:
        self.service_name = service_name

    def start_turn_span(self, name: str):
        if trace is None:
            return nullcontext()
        assert trace is not None
        tracer = trace.get_tracer(self.service_name)
        return tracer.start_as_current_span(name)
```

Then in the agent loop:

```text
with span_factory.start_turn_span("agent.turn") as span:
    if span is not None:
        span.set_attribute("agent.session_id", session_id)
        span.set_attribute("agent.turn_id", turn_id)
        span.set_attribute("gen_ai.provider", runtime.provider)
        span.set_attribute("gen_ai.request.model", runtime.model)
        span.set_attribute("agent.permission_mode", runtime.permission_mode)

    # execute the turn

    if span is not None:
        span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
        span.set_attribute("agent.tool_calls", tool_call_count)
        span.set_attribute("agent.duration_ms", duration_ms)
```

### Semantic-convention note

OpenTelemetry support for GenAI telemetry is evolving. Prefer official semantic-convention keys when your chosen SDK/exporter stack supports them. If not, use a stable internal prefix like:

- `agent.*`
- `gen_ai.*`
- `tool.*`

Consistency matters more than choosing the perfect prefix on day one.

## 10.3 Create child spans for tool calls

A great tracing pattern is:

- one parent span for the turn,
- child spans for each tool call,
- optional child span for session save / compaction / confirmation wait.

Example attributes on tool spans:

- `tool.name`
- `tool.call_id`
- `tool.is_mutating`
- `tool.duration_ms`
- `tool.status`
- `tool.confirmed`

This makes latency debugging far easier.

---

## 11. Lightweight alternatives to OpenTelemetry

Not every local harness needs a full tracing stack immediately.

### Option A — Standard library `logging` + JSONL

Best when you want:

- zero major conceptual overhead,
- local debugging,
- grep-friendly logs,
- and simple ingestion later.

### Option B — `structlog`

Best when you want:

- structured logs first,
- ergonomic context binding,
- better developer experience than raw `logging`,
- and easy JSON output.

A minimal install:

```bash
pip install structlog
```

### Option C — Pydantic/dataclass event objects + JSON writer

Best when you want:

- full control,
- no extra logging abstraction,
- and tutorial clarity.

This is often the best teaching approach because it keeps the data model explicit.

### Option D — Vendor-friendly observability libraries

Some teams may prefer tools like Logfire or similar tracing/logging platforms for a quicker developer experience.

That is acceptable, but the same architectural rule still applies:

> keep your harness telemetry model independent from any one vendor SDK.

---

## 12. Cost tracking: estimate carefully, label honestly

Cost logging is useful, but easy to do badly.

### Golden rule

If your value is not provider-billed ground truth, log it as an estimate.

Use fields like:

- `estimated_usd`
- `pricing_source`
- `pricing_version`
- `estimate_method`

Do **not** label a derived number as exact cost unless the provider actually returned billable cost metadata.

## 12.1 Per-turn and per-session tracking

You want both:

### Per-turn cost

Useful for:

- identifying the expensive prompt,
- finding tool-call loops,
- comparing models.

### Per-session cost

Useful for:

- budget limits,
- user feedback,
- daily summaries,
- testing and evaluation.

## 12.2 Example accumulator

```text
# agent/telemetry.py

from decimal import Decimal


class SessionCostTracker:
    def __init__(self) -> None:
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_estimated_usd = Decimal("0")

    def add(self, usage: UsageSnapshot, estimate: CostEstimate | None) -> None:
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        if estimate is not None:
            self.total_estimated_usd += estimate.estimated_usd
```

### Budget enforcement idea

Once you have this tracker, adding a soft budget is straightforward:

- warn after `$1.00`,
- require confirmation after `$5.00`,
- stop after `$20.00`.

That is not mandatory for the tutorial, but the logging chapter is the right place to mention it.

---

## 13. Hook integration pattern

You already built hooks in Chapter 04. Observability should use them heavily.

### Why hooks are a good fit

Because logging and telemetry are classic cross-cutting concerns.

You do not want every tool implementation to manually reinvent logging behavior.

### Good hook points for telemetry

- `USER_PROMPT_SUBMIT`
- `PRE_TOOL_USE`
- `POST_TOOL_USE`
- `STOP`
- provider call start/stop if your runtime exposes them

### Example telemetry hook shape

```python
# agent/hooks.py

class TelemetryHook:
    def __init__(self, logger, pricing_table):
        self.logger = logger
        self.pricing_table = pricing_table

    async def __call__(self, event_name: str, payload: dict) -> None:
        if event_name == "post_tool_use":
            self.logger.info(
                "tool event",
                extra={
                    "payload": {
                        "event": "agent.tool.completed",
                        "session_id": payload["session_id"],
                        "turn_id": payload["turn_id"],
                        "tool_name": payload["tool_name"],
                        "duration_ms": payload["duration_ms"],
                        "status": payload["status"],
                    }
                },
            )
```

This keeps observability decoupled from business logic.

---

## 14. Persist telemetry summaries with sessions

Observability becomes much more useful if session snapshots carry summarized usage.

### Good fields to persist in `SessionSnapshot`

- total turns,
- total input tokens,
- total output tokens,
- total estimated cost,
- active model,
- active profile,
- started/ended timestamps,
- last error summary if session ended badly.

That lets your session list say things like:

```text
sess_20260425_141200  claude-sonnet-4-6  12 turns  31,204 tokens  ~$0.14  ok
```

This is small, high-value metadata.

---

## 15. Mapping this chapter to the real OpenHarness repository

This tutorial series is generic, but the current repository already contains useful pieces you can learn from and build on.

### Existing usage surface

OpenHarness already has a small usage model in:

- `src/openharness/api/usage.py` → `UsageSnapshot`

It currently tracks:

- `input_tokens`
- `output_tokens`
- `total_tokens`

That is a good base. This chapter extends the concept rather than replacing it.

### Existing session-level accumulation

OpenHarness also already has:

- `src/openharness/engine/cost_tracker.py` → aggregates usage across a session
- `src/openharness/engine/query_engine.py` → exposes `total_usage`

That means the repository already has the beginnings of a cost/usage layer.

### Existing user-facing commands

OpenHarness already exposes usage/cost visibility in:

- `src/openharness/commands/registry.py`

Notably:

- `/status`
- `/usage`
- `/cost`

That is useful proof that token and cost visibility matter in real CLI use.

### Existing model configuration surface

OpenHarness already has a rich provider/model configuration system in:

- `src/openharness/config/settings.py`

Important types there include:

- `Settings`
- `ProviderProfile`

That file is exactly the kind of place where safe model-configuration logging should be derived from.

### Practical takeaway

If you later implement this chapter in the real codebase, do not invent a second disconnected settings model.

Instead:

- derive runtime model config from `Settings` / `ProviderProfile`,
- derive token usage from the existing usage tracker,
- and export those through a shared telemetry layer.

---

## 16. Privacy, redaction, and prompt safety

The fastest way to ruin a logging system is to make it too chatty.

### Default-safe rule set

By default, log:

- ids,
- durations,
- counts,
- decision outcomes,
- model/runtime config,
- safe path metadata,
- and summarized tool output sizes.

By default, do **not** log:

- full raw prompts,
- full raw tool outputs,
- full file contents,
- secret-bearing env vars,
- auth headers,
- cookies,
- private memory entries unless the user opted in.

### Prompt logging policy

A good compromise is:

- log prompt length,
- log compacted-context length,
- log a hash or fingerprint of the system prompt,
- optionally log full prompts only in explicit debug mode.

### Tool output logging policy

A good compromise is:

- log output byte length,
- log whether output was truncated,
- log structured result metadata,
- do not log giant stdout blobs by default.

---

## 17. Recommended `agent.toml` policy defaults

If you add this feature to the tutorial harness, good defaults would be:

- logging enabled,
- JSONL enabled,
- token usage enabled,
- model config logging enabled,
- tool argument logging disabled by default,
- OpenTelemetry disabled by default,
- cost estimation enabled only when a pricing table exists,
- prompt-body logging disabled unless debug mode is on.

This gives readers useful visibility without creating accidental data leaks.

---

## 18. A practical implementation order

If you decide to implement this chapter after reading it, do it in this order:

### Step 1

Create a canonical `UsageSnapshot` and `ModelRuntimeConfig`.

### Step 2

Create a JSONL logger with redaction.

### Step 3

Emit `agent.turn.started` and `agent.turn.completed` logs.

### Step 4

Add tool start/finish logs.

### Step 5

Add pricing rules and estimated cost fields.

### Step 6

Persist summarized usage/cost into the session snapshot.

### Step 7

Add optional OpenTelemetry spans.

### Step 8

Add metrics or dashboards only after the event schema stabilizes.

This ordering matters. Do not jump straight to tracing dashboards before your local event schema is trustworthy.

---

## 19. Exercises

**Exercise A — Add budget warnings**

When per-session estimated cost exceeds a configured threshold, emit a `StatusEvent` like:

```text
[budget] Session estimate crossed $2.00 — consider switching to a cheaper model.
```

**Exercise B — Prompt fingerprints**

Compute a SHA-256 hash of the system prompt and log only the hash by default. This helps you correlate behavior changes with prompt changes without storing the full prompt body.

**Exercise C — Swarm trace tree**

If you implemented swarms, create child spans or child log contexts for worker tasks so one parent request can be traced across coordinator and workers.

**Exercise D — Error-only log stream**

Write `WARNING` and `ERROR` records to `.agent/logs/errors.jsonl` in addition to the main runtime log.

**Exercise E — Provider pricing versioning**

Store pricing metadata in config like:

```toml
[pricing]
version = "2026-04-25"
source = "manual-copy-from-provider-pricing-page"
```

Then attach that metadata to every cost estimate.

---

## 20. Checklist before moving on

- [ ] Structured logs are JSON, not free-form print text
- [ ] Every major record includes `session_id` and `turn_id`
- [ ] Tool logs include `tool_name`, duration, and status
- [ ] Model config is logged without secrets
- [ ] Token usage is captured per turn and accumulated per session
- [ ] Cost is labeled as estimated unless provider-billed cost is available
- [ ] Pricing rules live in config, not scattered in code
- [ ] Redaction exists for API keys, tokens, cookies, and auth headers
- [ ] Prompt-body logging is opt-in, not default
- [ ] Session summaries persist total usage and estimated cost
- [ ] OpenTelemetry is optional rather than a mandatory dependency
- [ ] The same telemetry data model can power logs, traces, metrics, and session summaries

---

## 21. Final note

A serious agent harness is not only defined by what it can do.

It is also defined by whether you can answer, with evidence:

- what happened,
- why it happened,
- how long it took,
- what it cost,
- and which exact runtime configuration produced the result.

That is what this chapter adds.

Next: [17-cli-flags-and-headless-mode.md](17-cli-flags-and-headless-mode.md) — add full `argparse` flags, a headless runner, exit codes, and CI/pipe usage patterns so the harness can be driven without a human present.

