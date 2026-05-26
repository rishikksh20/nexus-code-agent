from __future__ import annotations

from contextlib import nullcontext
import os
from pathlib import Path
from uuid import uuid4

import pytest

from nexus.config import load_config
from nexus.observability.logging import _redact_text
from nexus.observability.sentry import (
    SentryMonitor,
    describe_sentry_dsn,
    sentry_settings_from_config,
)


class RecordingSpan:
    def __init__(self, *, op: str | None = None, name: str | None = None) -> None:
        self.op = op
        self.name = name
        self.attributes: dict[str, object] = {}
        self.status: str | None = None

    def __enter__(self) -> RecordingSpan:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_data(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_tag(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, value: str) -> None:
        self.status = value


class RecordingSentryClient:
    def __init__(self) -> None:
        self.exceptions: list[BaseException] = []
        self.messages: list[tuple[str, str]] = []
        self.tags: dict[str, object] = {}
        self.contexts: dict[str, dict[str, object]] = {}
        self.transaction_calls: list[dict[str, object]] = []
        self.span_calls: list[dict[str, object]] = []
        self.current_transaction: RecordingSpan | None = None
        self.current_span: RecordingSpan | None = None
        self.flushed = False

    def init(self, **kwargs) -> None:
        return None

    def capture_exception(self, error: BaseException) -> str | None:
        self.exceptions.append(error)
        return "exception-id"

    def capture_message(self, message: str, level: str = "info") -> str | None:
        self.messages.append((message, level))
        return "message-id"

    def add_breadcrumb(self, **kwargs) -> None:
        return None

    def set_tag(self, key: str, value: object) -> None:
        self.tags[key] = value

    def set_context(self, key: str, value: dict[str, object]) -> None:
        self.contexts[key] = value

    def start_transaction(self, **kwargs) -> RecordingSpan:
        self.transaction_calls.append(dict(kwargs))
        transaction = RecordingSpan(op=kwargs.get("op"), name=kwargs.get("name"))
        self.current_transaction = transaction
        self.current_span = transaction
        return transaction

    def start_span(self, **kwargs) -> RecordingSpan:
        self.span_calls.append(dict(kwargs))
        span = RecordingSpan(op=kwargs.get("op"), name=kwargs.get("name"))
        self.current_span = span
        return span

    def update_current_span(self, **kwargs) -> None:
        span = self.current_span
        if span is None:
            return
        if kwargs.get("op") not in (None, ""):
            span.op = kwargs["op"]
        if kwargs.get("name") not in (None, ""):
            span.name = kwargs["name"]
        if kwargs.get("status") not in (None, ""):
            span.status = kwargs["status"]
        attributes = kwargs.get("attributes")
        if isinstance(attributes, dict):
            span.attributes.update(attributes)

    def flush(self, timeout: float | None = None) -> bool:
        self.flushed = True
        return True


def _set_scope_status(scope: object | None, *, status: str, attributes: dict[str, object] | None = None) -> None:
    if scope is None:
        return

    setter = getattr(scope, "set_status", None)
    if callable(setter):
        try:
            setter(status)
        except Exception:
            pass
    else:
        try:
            setattr(scope, "status", status)
        except Exception:
            pass

    if not attributes:
        return
    for key, value in attributes.items():
        for method_name in ("set_attribute", "set_data", "set_tag"):
            method = getattr(scope, method_name, None)
            if callable(method):
                try:
                    method(key, value)
                    break
                except Exception:
                    continue


def _emit_live_trace_and_span_error(monitor: SentryMonitor, verification_id: str) -> dict[str, str | None]:
    context = {
        "session_id": "pytest-sentry-live-trace",
        "turn_id": verification_id,
        "trace_id": verification_id,
        "mode": "pytest",
        "agent_mode": "manual_verifier",
        "verification_id": verification_id,
        "verification_kind": "live_sentry_trace_test",
    }
    transaction = monitor.start_transaction(
        name="nexus.turn",
        op="nexus.turn",
        attributes={
            **context,
            "nexus.test.name": "manual_sentry_trace_verification",
            "nexus.test.surface": "turn_runner",
        },
    )

    with (transaction if transaction is not None else nullcontext()):
        span = monitor.start_span(
            op="nexus.tool",
            name="manual.sentry.trace.verification",
            attributes={
                "nexus.tool.name": "manual-sentry-trace-verifier",
                "nexus.tool.source": "manual_test",
                "nexus.tool.origin": "tests/test_sentry_live_trace_manual.py",
                "nexus.tool.kind": "manual",
                "nexus.tool.is_mutating": False,
                **context,
            },
        )
        with (span if span is not None else nullcontext()):
            try:
                raise RuntimeError(f"Intentional Sentry trace/span verification failure: {verification_id}")
            except RuntimeError as exc:
                monitor.update_current_span(
                    name="manual.sentry.trace.verification.failed",
                    status="internal_error",
                    attributes={
                        "nexus.test.status": "failed",
                        "nexus.test.error_type": exc.__class__.__name__,
                        "nexus.test.error_message": str(exc),
                        "nexus.test.verification_id": verification_id,
                    },
                )
                _set_scope_status(
                    transaction,
                    status="internal_error",
                    attributes={
                        "nexus.test.status": "failed",
                        "nexus.test.verification_id": verification_id,
                    },
                )
                exception_event_id = monitor.capture_exception(
                    exc,
                    context={
                        **context,
                        "event_type": "manual_trace_span_exception",
                        "error_area": "tool",
                        "span_name": "manual.sentry.trace.verification.failed",
                    },
                )
                message_event_id = monitor.capture_message(
                    f"Nexus live trace/span verification error: {verification_id}",
                    level="error",
                    context={
                        **context,
                        "event_type": "manual_trace_span_error_message",
                        "error_area": "tool",
                        "span_name": "manual.sentry.trace.verification.failed",
                    },
                )
                return {
                    "exception_event_id": exception_event_id,
                    "message_event_id": message_event_id,
                    "span_name": "manual.sentry.trace.verification.failed",
                    "transaction_name": "nexus.turn",
                }

    return {
        "exception_event_id": None,
        "message_event_id": None,
        "span_name": None,
        "transaction_name": "nexus.turn",
    }


def test_emit_live_trace_and_span_error_records_runtime_style_transaction_and_failed_span(tmp_path):
    config = load_config(
        tmp_path,
        global_root=tmp_path / "global",
        cli_overrides={
            "sentry_enabled": True,
            "sentry_dsn": "https://public@example.ingest.sentry.io/123",
            "sentry_traces_sample_rate": 1.0,
        },
    )
    client = RecordingSentryClient()
    monitor = SentryMonitor(sentry_settings_from_config(config), client=client)
    monitor.initialize()

    payload = _emit_live_trace_and_span_error(monitor, "pytest-live-sentry-trace")

    assert payload["exception_event_id"] == "exception-id"
    assert payload["message_event_id"] == "message-id"
    assert client.transaction_calls == [
        {
            "name": "nexus.turn",
            "op": "nexus.turn",
            "attributes": {
                "session_id": "pytest-sentry-live-trace",
                "turn_id": "pytest-live-sentry-trace",
                "trace_id": "pytest-live-sentry-trace",
                "mode": "pytest",
                "agent_mode": "manual_verifier",
                "verification_id": "pytest-live-sentry-trace",
                "verification_kind": "live_sentry_trace_test",
                "nexus.test.name": "manual_sentry_trace_verification",
                "nexus.test.surface": "turn_runner",
            },
        }
    ]
    assert client.span_calls == [
        {
            "op": "nexus.tool",
            "name": "manual.sentry.trace.verification",
            "attributes": {
                "nexus.tool.name": "manual-sentry-trace-verifier",
                "nexus.tool.source": "manual_test",
                "nexus.tool.origin": "tests/test_sentry_live_trace_manual.py",
                "nexus.tool.kind": "manual",
                "nexus.tool.is_mutating": False,
                "session_id": "pytest-sentry-live-trace",
                "turn_id": "pytest-live-sentry-trace",
                "trace_id": "pytest-live-sentry-trace",
                "mode": "pytest",
                "agent_mode": "manual_verifier",
                "verification_id": "pytest-live-sentry-trace",
                "verification_kind": "live_sentry_trace_test",
            },
        }
    ]
    assert len(client.exceptions) == 1
    assert str(client.exceptions[0]) == "Intentional Sentry trace/span verification failure: pytest-live-sentry-trace"
    assert client.messages == [("Nexus live trace/span verification error: pytest-live-sentry-trace", "error")]
    assert client.current_transaction is not None
    assert client.current_transaction.status == "internal_error"
    assert client.current_transaction.attributes["nexus.test.status"] == "failed"
    assert client.current_span is not None
    assert client.current_span.name == "manual.sentry.trace.verification.failed"
    assert client.current_span.status == "internal_error"
    assert client.current_span.attributes["nexus.test.verification_id"] == "pytest-live-sentry-trace"
    assert client.tags["nexus.verification_kind"] == "live_sentry_trace_test"
    assert client.tags["nexus.event_type"] == "manual_trace_span_error_message"


def test_sentry_live_trace_and_span_error_with_dotenv_credentials():
    if os.getenv("NEXUS_RUN_LIVE_SENTRY_TRACE_TEST") != "1":
        pytest.skip(
            "Set NEXUS_RUN_LIVE_SENTRY_TRACE_TEST=1 to send a live Sentry turn trace, failed span, exception, and error message."
        )

    workspace_root = Path(__file__).resolve().parents[1]
    config = load_config(
        workspace_root,
        global_root=workspace_root / ".pytest-global",
        cli_overrides={
            "sentry_enabled": True,
            "sentry_traces_sample_rate": 1.0,
            "sentry_sample_rate": 1.0,
        },
    )
    settings = sentry_settings_from_config(config)

    if not settings.dsn:
        pytest.fail(
            "No live Sentry DSN was loaded. Set SENTRY_DSN or AGENT_SENTRY_DSN in the workspace .env or environment before running this manual verification."
        )

    monitor = SentryMonitor(settings)
    monitor.initialize()
    assert monitor.enabled() is True

    verification_id = os.getenv("NEXUS_SENTRY_TRACE_VERIFICATION_ID", f"pytest-live-sentry-trace-{uuid4().hex[:8]}")
    try:
        payload = _emit_live_trace_and_span_error(monitor, verification_id)
        print(
            "Sentry live trace verification sent:",
            f"target={describe_sentry_dsn(settings.dsn)}",
            f"environment={settings.environment}",
            f"verification_id={verification_id}",
            f"transaction_name={payload['transaction_name']}",
            f"span_name={payload['span_name']}",
            f"exception_event_id={payload['exception_event_id']}",
            f"message_event_id={payload['message_event_id']}",
        )
    finally:
        monitor.flush()


# ---------------------------------------------------------------------------
# Root-cause diagnostic: verify _redact_text corrupts Sentry event fields
# ---------------------------------------------------------------------------

def test_redact_text_corrupts_sentry_event_id_and_abs_path():
    """
    Documents the root cause of events not appearing in Sentry:

    The `_before_send` callback in SentryMonitor runs `_scrub_value` on the
    entire Sentry event dict.  `_scrub_value` calls `redact_payload` which
    calls `_redact_text` on every string value.  `_redact_text` contains a
    base64-pattern regex  ``\\b([A-Za-z0-9+/]{32,}={0,2})\\b``  that matches
    any 32+ char alphanumeric string — including:

      - ``event_id`` (a 32-char hex string) → becomes ``[REDACTED]``
      - stack-trace ``abs_path`` values → get partially redacted

    When Sentry receives an event with ``event_id = "[REDACTED]"`` the event is
    rejected or never indexed, so the dashboard shows nothing.
    """
    # A typical Sentry event_id is a 32-char hex UUID without dashes
    sample_event_id = "bd8356e3b0134ab785b6b8d6d666e4d6"
    assert _redact_text(sample_event_id) == "[REDACTED]", (
        "event_id must be redacted — confirms why Sentry events are invisible"
    )

    # A stack-trace abs_path gets corrupted, breaking source-code links
    abs_path = "/Users/user/dev/build-an-ai-agent/nexus/observability/sentry.py"
    redacted_path = _redact_text(abs_path)
    assert "[REDACTED]" in redacted_path, (
        "abs_path in stack frames must be partially redacted — confirms stacktrace damage"
    )

    # Short strings and normal IDs (< 32 chars) are safe
    assert _redact_text("pytest-live-sentry-trace") == "pytest-live-sentry-trace"


# ---------------------------------------------------------------------------
# Live bypass test: uses sentry_sdk directly, no SentryMonitor wrapper
# ---------------------------------------------------------------------------

def test_sentry_live_push_bypassing_monitor():
    """
    Pushes a real nexus.turn transaction, a failed nexus.tool child span,
    a captured exception, and an error-level message directly to Sentry
    using ``sentry_sdk`` with NO ``before_send`` hooks.

    This bypasses the ``SentryMonitor._before_send`` scrubber that corrupts
    ``event_id`` and stack-trace fields and prevents events appearing on the
    Sentry dashboard.

    Run with:
        NEXUS_RUN_LIVE_SENTRY_DIRECT_TEST=1 uv run pytest \\
            tests/test_sentry_live_trace_manual.py \\
            -k test_sentry_live_push_bypassing_monitor -s

    All emitted events carry the ``nexus.verification_id`` tag so you can find
    them instantly in Sentry with a tag filter.
    """
    if os.getenv("NEXUS_RUN_LIVE_SENTRY_DIRECT_TEST") != "1":
        pytest.skip(
            "Set NEXUS_RUN_LIVE_SENTRY_DIRECT_TEST=1 to send live Sentry events "
            "bypassing the SentryMonitor wrapper (no before_send hooks)."
        )

    try:
        import sentry_sdk as _sdk
    except ImportError:
        pytest.fail("sentry-sdk is not installed. Run: uv sync")

    workspace_root = Path(__file__).resolve().parents[1]

    # Load credentials through the real config system so .env is honoured
    config = load_config(
        workspace_root,
        global_root=workspace_root / ".pytest-global",
        cli_overrides={
            "sentry_enabled": True,
            "sentry_traces_sample_rate": 1.0,
            "sentry_sample_rate": 1.0,
        },
    )
    settings = sentry_settings_from_config(config)

    if not settings.dsn:
        pytest.fail(
            "No live Sentry DSN was loaded. "
            "Set SENTRY_DSN or AGENT_SENTRY_DSN in the workspace .env before running this test."
        )

    print(f"\nsentry-sdk {_sdk.VERSION}")
    print(f"DSN: {describe_sentry_dsn(settings.dsn)}")
    print(f"environment: {settings.environment}")

    # Init sentry_sdk directly — NO before_send hooks so event fields are
    # transmitted intact and events actually appear in the Sentry dashboard.
    _sdk.init(
        dsn=settings.dsn,
        environment=settings.environment,
        release=settings.release or None,
        traces_sample_rate=1.0,
        sample_rate=1.0,
        debug=True,
        send_default_pii=False,
        max_breadcrumbs=50,
        in_app_include=["nexus"],
    )

    verification_id = os.getenv(
        "NEXUS_SENTRY_DIRECT_VERIFICATION_ID",
        f"nexus-direct-{uuid4().hex[:10]}",
    )
    print(f"verification_id: {verification_id}")

    try:
        # ── 1. nexus.turn transaction + nexus.tool failed child span ──────────
        with _sdk.start_transaction(
            op="nexus.turn",
            name="nexus.turn [pytest-direct-push]",
        ) as txn:
            txn.set_tag("nexus.verification_id", verification_id)
            txn.set_tag("nexus.verification_kind", "direct_sdk_push")
            txn.set_tag("nexus.test.surface", "turn_runner")
            txn.set_data("nexus.session_id", "pytest-direct-push")
            txn.set_data("nexus.mode", "pytest_manual")

            _sdk.add_breadcrumb(
                category="nexus.turn",
                message=f"Turn started — direct push {verification_id}",
                level="info",
                data={"verification_id": verification_id},
            )

            with txn.start_child(
                op="nexus.tool",
                name="pytest-direct-push-verifier",
            ) as span:
                span.set_tag("nexus.tool.name", "pytest-direct-push-verifier")
                span.set_tag("nexus.tool.source", "test_sentry_live_trace_manual.py")
                span.set_data("nexus.verification_id", verification_id)

                _sdk.add_breadcrumb(
                    category="nexus.tool",
                    message="Tool started",
                    level="info",
                    data={"tool": "pytest-direct-push-verifier"},
                )

                try:
                    raise RuntimeError(
                        f"[nexus-direct-push] Intentional tool span failure: {verification_id}"
                    )
                except RuntimeError as exc:
                    _sdk.add_breadcrumb(
                        category="nexus.tool",
                        message="Tool raised exception",
                        level="error",
                        data={"error": str(exc)},
                    )
                    span.set_status("internal_error")

            txn.set_status("internal_error")

        # ── 2. Standalone exception event ─────────────────────────────────────
        _sdk.set_tag("nexus.verification_id", verification_id)
        _sdk.set_tag("nexus.verification_kind", "direct_sdk_push")
        _sdk.set_context("nexus", {
            "verification_id": verification_id,
            "verification_kind": "direct_sdk_push",
            "session_id": "pytest-direct-push",
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
            exception_event_id = _sdk.capture_exception(exc)

        # ── 3. Error-level message ─────────────────────────────────────────────
        message_event_id = _sdk.capture_message(
            f"[nexus-direct-push] Verification error message: {verification_id}",
            level="error",
        )

        print(f"\nexception_event_id : {exception_event_id}")
        print(f"message_event_id   : {message_event_id}")
        print(f"\nFind in Sentry → tag filter: nexus.verification_id:{verification_id}")
        print(f"Performance      → transaction 'nexus.turn [pytest-direct-push]'")

        assert exception_event_id is not None, "SDK must return an event_id for the captured exception"
        assert message_event_id is not None, "SDK must return an event_id for the captured message"

    finally:
        flushed = _sdk.flush(timeout=15.0)
        print(f"\nflush OK: {flushed}")