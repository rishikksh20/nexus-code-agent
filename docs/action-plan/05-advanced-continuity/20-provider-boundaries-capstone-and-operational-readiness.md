# Chapter 20: Provider Boundaries, Capstone Integration, And Operational Readiness

## Objective

Finish the continuity pass by connecting three final ideas that were still missing or too implicit:

- provider-neutral runtime boundaries from the attached `openai/understanding.md`
- a true end-to-end capstone scenario that walks through the whole harness
- honest operational readiness guidance so the result is described accurately

This chapter is the bridge between architecture comprehension and disciplined rollout.

## Part 1: Keep The Runtime Provider-Neutral

One of the most useful concepts in the attached `openai/understanding.md` file is the adapter boundary. The runtime should work with its own request and response shapes. Provider-specific JSON should stay inside the client wrapper.

### Add Internal Request And Response Types

```python
from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class RuntimeRequest:
    model_name: str
    system_prompt: str
    messages: tuple[Message, ...]
    tool_schemas: tuple[dict, ...] = ()
    max_output_tokens: int | None = None


@dataclass(slots=True, frozen=True)
class RuntimeResponse:
    message: Message
    tool_calls: tuple[ToolCall, ...] = ()
    usage: UsageSnapshot | None = None
    finish_reason: str = "done"
```

These are the only shapes the runtime loop should care about.

### Keep Provider JSON In The Adapter

```python
import json

# from models import Message, ToolCall, UsageSnapshot (wherever these live in your package)


class OpenAICompatibleAdapter:
    def to_wire_request(self, request: RuntimeRequest) -> dict:
        wire_messages = [{"role": "system", "content": request.system_prompt}]
        for message in request.messages:
            wire_messages.append({"role": message.role, "content": message.content})

        return {
            "model": request.model_name,
            "messages": wire_messages,
            "tools": list(request.tool_schemas),
            "max_tokens": request.max_output_tokens,
        }

    def from_wire_response(self, payload: dict) -> RuntimeResponse:
        choice = payload["choices"][0]["message"]
        message = Message(role="assistant", content=choice.get("content") or "")

        # Parse tool calls from the wire format
        raw_tool_calls = choice.get("tool_calls") or []
        tool_calls = tuple(
            ToolCall(
                call_id=tc["id"],
                tool_name=tc["function"]["name"],
                arguments=json.loads(tc["function"]["arguments"]),
            )
            for tc in raw_tool_calls
        )
        usage_raw = payload.get("usage")
        usage = None
        if usage_raw:
            usage = UsageSnapshot(
                prompt_tokens=usage_raw["prompt_tokens"],
                completion_tokens=usage_raw["completion_tokens"],
                total_tokens=usage_raw["total_tokens"],
                estimated_cost_usd=0.0,  # populate with PricingRule.estimate_cost()
                provider="openai",
                model=payload.get("model", ""),
            )
        return RuntimeResponse(message=message, tool_calls=tool_calls, usage=usage)
```

Why this matters:

- permission checks should not care about provider JSON
- tools should not parse wire-format fields
- hooks should observe normalized events, not vendor-specific blobs
- switching providers should mostly touch the adapter layer

If provider details leak into the runtime, every later change gets more expensive.

## Current Nexus Notes

The current Nexus implementation now reinforces this chapter in a few concrete ways:

- runtime request and response types remain provider-neutral
- provider-specific wire translation is still isolated inside the OpenAI-compatible adapter
- usage snapshots now carry provider and model identifiers directly
- a live OpenAI-compatible client now uses the adapter for `/chat/completions` requests
- bounded retries now distinguish retryable connection and selected HTTP failures from non-retryable ones
- provider misconfiguration is now rejected during config loading instead of deferring failure into runtime startup
- `mistral` is now the default provider; `mistral-medium-latest` is the default model and `https://api.mistral.ai/v1` is the default `api_base_url`; the fake provider remains available for CI and offline development via `--provider fake`
- auth resolution for Mistral checks `MISTRAL_API_KEY` → `NEXUS_API_KEY` → `OPENAI_API_KEY` in that order; a `.env` file in the workspace root is parsed at startup before any env-var lookup
- the REPL startup banner shows the active provider, model, and mode; if no API key is detected for a live provider, a warning is printed before the first prompt so users are not left guessing why responses look wrong
- the built-in model catalogue (`nexus/config/model_catalog.py`) is used at startup to auto-tune compaction thresholds to 65% and 85% of the active model's context window
- `nexus init` now prints a numbered API key setup guide when no key is found for the configured provider: `.env` file method, environment variable method, and config TOML method; for Mistral, a direct link to `https://console.mistral.ai` is included
- provider errors during REPL turns and headless runs are now caught and presented as a friendly `✗ Request failed.` message; covered cases include: no API key, 401/403 auth failure, 429 rate limit, missing `api_base_url`, and connection errors; in the REPL the failed turn is removed from history and the session stays open; in headless mode `HeadlessResult(exit_code=EXIT_ERROR)` is returned

## Part 2: Add Provider Resilience

The audit-style materials also imply an operational reality: provider calls fail transiently and need bounded retries.

```python
import asyncio
import random


async def call_with_backoff(send_request, retries: int = 3, base_delay: float = 0.5):
    for attempt in range(retries):
        try:
            return await send_request()
        except Exception:
            if attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.2)
            await asyncio.sleep(delay)
```

Keep retries:

- bounded
- logged
- differentiated between retryable and non-retryable failures

Do not hide repeated failures inside a silent adapter loop.

In the current Nexus runtime, retryable failures are network errors plus selected HTTP statuses like `429` and `5xx`. Other HTTP failures surface immediately instead of being retried blindly.

## Part 3: Walk Through A True Capstone Scenario

The attached audit notes correctly identify the absence of a single integration walkthrough. Add one to your project notes or internal docs using a scenario like this.

### Scenario

The user asks the harness to update a repository note and run a lightweight verification task.

### End-To-End Flow

1. The REPL creates a new user turn.
2. The context builder composes the system prompt from base instruction, workspace knowledge, memory, and active task focus.
3. The provider adapter converts the internal runtime request into the provider wire format and the live client posts it to `/chat/completions`.
4. The model returns a request to read a file and then write a note.
5. The read tool runs immediately because it is safe.
6. The write tool is classified as mutating and routed through the permission system.
7. A confirmation request is shown to the user with scope and rollback information.
8. After approval, the write executes and the audit trail records the action.
9. The coordinator spawns a worker to run a validation task.
10. The worker reports back through the mailbox.
11. The session is saved.
12. Post-session hooks update `.agent/knowledge.md` and `~/.agent/profile.md`.
13. Structured logs and turn telemetry record cost, latency, and outcomes.

In Nexus specifically, local state lives under `.nexus/` and global state under `~/.nexus/`.

That scenario is valuable because it crosses nearly every subsystem without requiring a large product surface.

## Part 4: Add An Environment And Dependency Matrix

One of the remaining audit gaps is that later-stage environment assumptions can surprise readers. Add a compact matrix to your project docs.

```text
Chapter 1-3   : Python only
Chapter 4-6   : pytest, tomllib or Python 3.11+, JSON logging
Chapter 7     : optional Docker, optional MCP servers
Chapter 8     : no new hard dependency, but stronger concurrency assumptions
Chapter 17    : writable .agent directories locally and globally
Chapter 18    : optional OpenTelemetry exporter
Chapter 19-20 : retry policy, lock strategy, and audit log retention guidance
```

Also label optional chapters clearly so users know what can be deferred.

## Part 5: Be Honest About Readiness

The post-audit materials make a useful distinction that this action plan should keep: a solid tutorial harness is usually a serious prototype or reference architecture before it becomes a full production platform.

Use that language deliberately.

### A Good Serious Prototype Has

- typed runtime models
- permission enforcement in code
- tests for core flows and hardening paths
- observability and cost visibility
- resumable sessions and durable local knowledge
- bounded dangerous execution behind sandbox or policy

### A Production System Still Needs

- deployment-specific secrets management
- stronger authn and authz for multi-user settings
- stricter rate limiting and quotas
- retention and redaction policy review
- backup and restore strategy for state stores
- alerting, on-call ownership, and incident playbooks

Do not compress those concerns into a vague phrase like "production ready".

## Operational Release Gates

Before rollout, require each of these gates.

### Gate 1: Runtime Integrity

- provider adapter uses normalized internal request and response types
- no provider JSON leaks into permissions or tools
- retry policy is bounded and logged

### Gate 2: Safety Integrity

- hard-deny policy wins over mode shortcuts
- dangerous tools show scope and rollback status
- audit trail is written for all mutating actions

### Gate 3: Observability Integrity

- logs carry correlation identifiers
- usage and estimated cost are recorded per turn
- redaction is tested

### Gate 4: State Integrity

- sessions resume correctly
- knowledge and profile files update atomically
- concurrent file access has a deliberate strategy

### Gate 5: Documentation Integrity

- chapter order and next-step references are consistent
- dependency requirements are documented
- optional chapters are marked clearly
- the system is described honestly as prototype or production depending on actual controls

## Final Action Plan

1. Normalize runtime request and response types.
2. Keep provider wire formats inside adapters only.
3. Add bounded retry and backoff for transient provider failures.
4. Document one capstone scenario that crosses all major subsystems.
5. Publish a small dependency matrix.
6. Apply explicit rollout gates before claiming operational readiness.

## Definition Of Done

This chapter is complete when the harness is understandable end to end, not just piece by piece. If the provider boundary is still fuzzy, the capstone flow still missing, or the release posture still overstated, the continuity pass is incomplete.
