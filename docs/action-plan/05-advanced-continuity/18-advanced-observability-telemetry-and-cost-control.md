# Chapter 18: Advanced Observability, Telemetry, And Cost Control

## Objective

Give the harness the ability to explain itself operationally. Earlier chapters added basic structured logging, but the attached OpenAI tutorial material goes further: separate logs from traces, track token and cost data per turn, preserve correlation identifiers, and redact secrets before anything is written to disk.

This chapter turns those ideas into a practical implementation plan.

## Why This Matters

Once the harness can use tools, resume sessions, load plugins, and delegate work, it becomes impossible to reason about failures from print statements alone.

You need to answer questions like:

- which model and provider were active?
- how much did that turn cost?
- which tool call caused the delay?
- was the failure in the permission layer, the provider adapter, or the tool runtime?
- did the system log enough detail without leaking secrets?

## Keep Four Surfaces Separate

Do not mix these together.

- audit trail: accountable records for approvals, denials, and dangerous actions
- structured logs: machine-readable operational events for debugging
- metrics: aggregated counters and latencies for trend analysis
- traces: request or turn timelines with correlated spans

If one JSON blob tries to do all four jobs, it will do each badly.

## Current Nexus Notes

The current Nexus runtime now separates these surfaces more explicitly than before:

- operational logs go to `runtime.jsonl`
- aggregated counters and cost summaries go to `metrics.json`
- mutating-action accountability goes to `.nexus/audit-trail.jsonl`
- turn and tool events now carry `turn_id`, `trace_id`, and `tool_call_id` where applicable

The current implementation also redacts common secret-shaped keys before JSON log serialization and records tool execution duration in hook payloads. Full trace export is still intentionally deferred.

## Add A Canonical Usage Snapshot

Start with one typed usage object and fan it out to logs, metrics, and session summaries.

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class UsageSnapshot:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    provider: str
    model: str
```

This object should be attached to a turn or model response, not rebuilt from scratch in multiple places.

## Add Correlation Identifiers

Every significant event should carry enough information to reconstruct the path of execution.

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class CorrelationContext:
    session_id: str
    turn_id: str
    trace_id: str
    tool_call_id: str | None = None
    worker_id: str | None = None
```

At minimum, include:

- session ID
- turn ID
- trace ID or correlation ID
- tool call ID where relevant
- worker ID once delegation exists

## Write Structured JSON Logs

```python
import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", "runtime.log"),
            "message": record.getMessage(),
            "logger": record.name,
        }

        for field_name in ("session_id", "turn_id", "trace_id", "tool_call_id", "worker_id"):
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = value

        usage = getattr(record, "usage", None)
        if usage is not None:
            payload["usage"] = usage

        return json.dumps(payload)
```

Prefer JSONL output so each event is one line and easy to ingest into other systems.

## Redact Secrets Before Logging

The attached observability chapter is explicit about this: model configuration should be visible, but secrets should never be logged.

Use a redaction layer before serialization.

```python
SENSITIVE_KEYS = {"api_key", "authorization", "token", "cookie", "password"}


def redact_payload(payload: dict) -> dict:
    redacted: dict = {}

    for key, value in payload.items():
        if key.lower() in SENSITIVE_KEYS:
            redacted[key] = "[REDACTED]"
        elif isinstance(value, dict):
            redacted[key] = redact_payload(value)
        else:
            redacted[key] = value

    return redacted
```

Also avoid logging raw environment dumps, raw request headers, or full prompt bodies unless you have a very explicit debug mode and a clear privacy policy.

## Add Pricing Rules

Estimated cost needs a stable source. Keep pricing configuration separate from the raw usage snapshot.

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class PricingRule:
    model: str
    input_cost_per_1k: float
    output_cost_per_1k: float


def estimate_cost(rule: PricingRule, prompt_tokens: int, completion_tokens: int) -> float:
    input_cost = (prompt_tokens / 1000) * rule.input_cost_per_1k
    output_cost = (completion_tokens / 1000) * rule.output_cost_per_1k
    return round(input_cost + output_cost, 6)
```

This gives you a controlled approximation even if the upstream provider does not always return final billing data.

## Measure Tool Latency And Outcomes

Observability is not only about model calls. Tool behavior often dominates user experience.

```python
import time


async def execute_tool_with_metrics(tool: BaseTool, call_id: str, arguments: dict, context: ToolExecutionContext):
    start = time.perf_counter()
    status = "error"  # set early so finally block always sees it
    try:
        result = await tool.execute(call_id, arguments, context)
        status = "ok"
        return result, status, time.perf_counter() - start
    except Exception:
        raise
    finally:
        duration = time.perf_counter() - start
        logger.info(
            "Tool execution finished",
            extra={
                "event": "agent.tool.completed",
                "tool_name": tool.name,
                "duration_ms": round(duration * 1000, 2),
                "status": status,
            },
        )
```

The pattern matters more than the exact logging call: measure tool runtime deliberately and tag the result.

## Emit Turn-Level Telemetry

Track a whole turn from model request to stop condition.

```python
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class TurnTelemetry:
    correlation: CorrelationContext
    usage: UsageSnapshot | None
    tool_calls: int
    duration_ms: float
    status: str
```

Persist summarized telemetry in two places:

- the runtime log for detailed inspection
- the session summary so you can reason about the session after it ends

## Optional: Add Traces With OpenTelemetry

Only do this if your deployment needs cross-process or cross-service timing visibility. Many small harnesses do not need full tracing on day one.

If you do add it, keep the mental model clean:

- one span for the turn
- nested spans for model calls
- nested spans for tool execution
- separate spans for worker tasks when delegation is used

Do not treat tracing as a substitute for logs or audit trails.

## Cost Control Policies

Once telemetry exists, put it to use.

Add budget-aware runtime checks such as:

- max tokens per turn
- max tokens per session
- max estimated cost per session
- stop and ask for confirmation if the next step is likely to exceed a threshold

This is especially important for tool-call loops or repeated worker fan-out.

## Action Plan

1. Create a canonical `UsageSnapshot` and `CorrelationContext`.
2. Emit structured JSONL logs with stable field names.
3. Redact secrets before serialization.
4. Track model cost using explicit pricing rules.
5. Record tool latency, status, and correlation identifiers.
6. Persist turn-level telemetry in session summaries.
7. Add optional trace export only if your deployment truly benefits from it.
8. Use telemetry to enforce budget thresholds, not just to generate dashboards.

## Validation Checklist

- Every important runtime event carries session and turn context.
- Model usage and estimated cost are visible per turn.
- Secrets are redacted before log writing.
- Tool latency is recorded for success and failure cases.
- Audit trail entries remain separate from operational logs.
- Budget checks can stop runaway turns before spending grows silently.

## Definition Of Done

This chapter is complete when a bad session is explainable from the recorded data. If you still cannot tell whether cost, latency, or failure came from the model, the tool layer, or the runtime policy layer, the observability design is still incomplete.