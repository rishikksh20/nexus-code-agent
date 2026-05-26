#!/usr/bin/env python
"""Standalone Sentry push verifier — bypasses all Nexus layers.

WHY THIS SCRIPT EXISTS
======================
The normal Nexus SentryMonitor wraps every event through ``_before_send``,
which runs ``_scrub_value`` (a payload sanitiser designed for prompt text) on
the *entire* Sentry event dict.  The sanitiser contains an aggressive
base64-pattern regex that matches any 32+ char alphanumeric run.  This
silently corrupts Sentry protocol fields such as ``event_id`` (hex-32),
stack-trace ``abs_path`` entries, and ``module`` names — causing Sentry's
ingestion to drop or mangle the events so they never appear on the dashboard.

This script bypasses the Nexus layer entirely: it calls ``sentry_sdk.init()``
with **no** ``before_send``/``before_send_transaction`` hooks so every event is
transmitted intact.

USAGE
=====
    uv run python tests/sentry_push_direct.py

Optional environment overrides (in addition to what is in .env):

    SENTRY_DSN=https://...          # override the DSN
    SENTRY_ENVIRONMENT=development  # override the environment
    SENTRY_RELEASE=nexus-local      # override the release

What gets pushed
----------------
1. A ``nexus.turn`` root transaction (performance trace) with:
   - a ``nexus.tool`` child span marked ``internal_error``
   - breadcrumbs for turn-start, tool-start, tool-error
2. A standalone exception event (same flow as turn_runner.capture_exception)
3. An error-level message event (same flow as SentryHookService.capture_message)

All three items carry a unique ``verification_id`` tag so you can search for
them in Sentry with a single filter:

    nexus.verification_id = <printed at runtime>

Find results at:
    Issues   → https://sentry.io/organizations/<org>/issues/
    Perf     → https://sentry.io/organizations/<org>/performance/
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4


# ---------------------------------------------------------------------------
# Minimal .env loader (no third-party deps)
# ---------------------------------------------------------------------------

def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        env[key.strip()] = raw.strip().strip('"').strip("'")
    return env


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    workspace_root = Path(__file__).resolve().parents[1]

    # Inject .env first (same order as nexus/config/loader.py _inject_dotenv)
    for key, value in _load_dotenv(workspace_root / ".env").items():
        os.environ[key] = value  # .env always wins

    dsn = os.environ.get("SENTRY_DSN") or os.environ.get("AGENT_SENTRY_DSN") or ""
    environment = os.environ.get("SENTRY_ENVIRONMENT", "development")
    release = os.environ.get("SENTRY_RELEASE", "") or None

    if not dsn:
        print("ERROR: No SENTRY_DSN found in .env or environment. Aborting.")
        sys.exit(1)

    try:
        import sentry_sdk
    except ImportError:
        print("ERROR: sentry-sdk is not installed. Run: uv sync")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Init — NO before_send hooks so Sentry event fields are never
    # corrupted by the Nexus payload scrubber.
    # ------------------------------------------------------------------
    print("=" * 60)
    print("Nexus → Sentry direct push verifier")
    print("=" * 60)
    print(f"  sentry-sdk : {sentry_sdk.VERSION}")
    print(f"  DSN        : {dsn[:60]}...")
    print(f"  environment: {environment}")
    print(f"  release    : {release or '(none)'}")
    print()

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        traces_sample_rate=1.0,   # capture ALL transactions
        sample_rate=1.0,          # capture ALL error events
        debug=True,               # print SDK debug output so you can see what's sent
        send_default_pii=False,
        max_breadcrumbs=50,
        in_app_include=["nexus"],
        # ↓ explicitly omit before_send, before_send_transaction, before_send_log
    )

    verification_id = f"nexus-direct-{uuid4().hex[:10]}"
    print(f"verification_id: {verification_id}")
    print()

    # ── 1. Performance trace: nexus.turn transaction + nexus.tool span ──────
    print("[1/3] nexus.turn transaction  +  nexus.tool failed child span ...")

    with sentry_sdk.start_transaction(
        op="nexus.turn",
        name="nexus.turn [direct-push-verifier]",
    ) as txn:
        txn.set_tag("nexus.verification_id", verification_id)
        txn.set_tag("nexus.verification_kind", "direct_sdk_push")
        txn.set_tag("nexus.test.surface", "turn_runner")
        txn.set_data("nexus.session_id", "sentry-push-direct")
        txn.set_data("nexus.mode", "pytest_manual")

        sentry_sdk.add_breadcrumb(
            category="nexus.turn",
            message=f"Turn started — {verification_id}",
            level="info",
            data={"verification_id": verification_id, "mode": "pytest_manual"},
        )

        # child span — mirrors agent.py start_span for tool execution
        with txn.start_child(
            op="nexus.tool",
            name="manual-sentry-push-verifier",
        ) as span:
            span.set_tag("nexus.tool.name", "manual-sentry-push-verifier")
            span.set_tag("nexus.tool.source", "sentry_push_direct")
            span.set_tag("nexus.tool.is_mutating", False)
            span.set_data("nexus.verification_id", verification_id)

            sentry_sdk.add_breadcrumb(
                category="nexus.tool",
                message="Tool started",
                level="info",
                data={"tool": "manual-sentry-push-verifier"},
            )

            try:
                raise RuntimeError(
                    f"[nexus-direct-push] Intentional tool span failure: {verification_id}"
                )
            except RuntimeError as exc:
                sentry_sdk.add_breadcrumb(
                    category="nexus.tool",
                    message="Tool raised exception",
                    level="error",
                    data={"error": str(exc)},
                )
                span.set_status("internal_error")

        txn.set_status("internal_error")

    print("  -> transaction + span closed\n")

    # ── 2. Exception event — mirrors turn_runner capture_exception_from_hooks ─
    print("[2/3] Capturing exception event ...")

    sentry_sdk.set_tag("nexus.verification_id", verification_id)
    sentry_sdk.set_tag("nexus.verification_kind", "direct_sdk_push")
    sentry_sdk.set_context("nexus", {
        "verification_id": verification_id,
        "verification_kind": "direct_sdk_push",
        "session_id": "sentry-push-direct",
        "turn_id": verification_id,
        "trace_id": verification_id,
        "mode": "pytest_manual",
        "error_area": "tool",
    })

    try:
        raise RuntimeError(
            f"[nexus-direct-push] Intentional exception capture: {verification_id}"
        )
    except RuntimeError as exc:
        exception_event_id = sentry_sdk.capture_exception(exc)

    print(f"  -> exception_event_id: {exception_event_id}\n")

    # ── 3. Error-level message — mirrors SentryHookService.on_notification ───
    print("[3/3] Capturing error message ...")

    message_event_id = sentry_sdk.capture_message(
        f"[nexus-direct-push] Verification error message: {verification_id}",
        level="error",
    )
    print(f"  -> message_event_id: {message_event_id}\n")

    # ── Flush (long timeout so SDK background thread finishes) ───────────────
    print("Flushing (timeout=15 s) ...")
    flushed = sentry_sdk.flush(timeout=15.0)
    print(f"  -> flush OK: {flushed}\n")

    # ── Summary ──────────────────────────────────────────────────────────────
    print("=" * 60)
    print("DONE — find your data in Sentry:")
    print(f"  verification_id : {verification_id}")
    print(f"  exception event : {exception_event_id}")
    print(f"  message  event  : {message_event_id}")
    print()
    print("Search in Sentry:")
    print(f"  Issues   → filter by tag  nexus.verification_id:{verification_id}")
    print("  Perf     → search transaction name 'nexus.turn'")
    print(f"  All evts → event ID {exception_event_id}")
    print("=" * 60)


if __name__ == "__main__":
    main()
