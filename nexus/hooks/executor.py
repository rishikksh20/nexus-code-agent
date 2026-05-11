"""Hook executor — registration and dispatch for :class:`HookEvent` handlers.

The :class:`HookExecutor` is the central object that ties the hooks system
together.  Components register handlers against specific events; the runtime
emits events by calling :meth:`HookExecutor.emit` or
:meth:`HookExecutor.emit_payload`.

Design decisions
----------------
* **Async handlers only** — all handlers are ``async`` coroutines.  This lets
  handlers perform I/O (e.g. writing audit logs) without blocking the event
  loop.
* **Fire-and-continue** — exceptions in individual handlers are caught and
  logged; a failing handler never prevents other handlers or the main runtime
  from proceeding.
* **Dict-based payload wire format** — handlers receive ``dict[str, Any]``
  payloads so that they work with any serialisation format and remain easy to
  test.  Typed :mod:`~nexus.hooks.payloads` dataclasses are accepted by
  :meth:`emit_payload` and converted automatically.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, fields as dataclass_fields
from collections.abc import Awaitable, Callable
from typing import Any

from nexus.hooks.events import HookEvent


HookHandler = Callable[[dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)


def _is_dataclass_instance(obj: object) -> bool:
    """Return True if *obj* is an instance of a dataclass (not a class itself)."""
    import dataclasses
    return dataclasses.is_dataclass(obj) and not isinstance(obj, type)


class HookExecutor:
    """Register and dispatch async hook handlers.

    Usage::

        hooks = HookExecutor()

        async def on_pre_tool(payload: dict) -> None:
            print("About to run", payload["tool_name"])

        hooks.register(HookEvent.PRE_TOOL_USE, on_pre_tool)

        # Later, in the runtime:
        await hooks.emit(HookEvent.PRE_TOOL_USE, {"tool_name": "bash", ...})
        # or with a typed payload:
        await hooks.emit_payload(HookEvent.PRE_TOOL_USE, PreToolPayload(...))
    """

    def __init__(self) -> None:
        self._handlers: dict[HookEvent, list[HookHandler]] = defaultdict(list)

    def register(self, event: HookEvent, handler: HookHandler) -> None:
        """Register *handler* to be called whenever *event* is emitted.

        Handlers are called in registration order.  The same handler may be
        registered for multiple events.

        Parameters
        ----------
        event:
            The :class:`~nexus.hooks.events.HookEvent` to listen for.
        handler:
            An async callable that accepts a ``dict[str, Any]`` payload.
        """
        self._handlers[event].append(handler)

    def unregister(self, event: HookEvent, handler: HookHandler) -> None:
        """Remove a previously registered *handler* for *event*.

        A no-op if *handler* is not registered for *event*.
        """
        try:
            self._handlers[event].remove(handler)
        except ValueError:
            pass

    async def emit(self, event: HookEvent, payload: dict[str, Any]) -> None:
        """Dispatch *payload* to all handlers registered for *event*.

        Exceptions raised by individual handlers are caught, logged at
        WARNING level, and do not interrupt other handlers.

        Parameters
        ----------
        event:
            The event to emit.
        payload:
            A ``dict`` that will be passed verbatim to each handler.
        """
        for handler in self._handlers.get(event, []):
            try:
                await handler(payload)
            except Exception:
                logger.exception(
                    "Hook handler %r failed for event %s",
                    getattr(handler, "__qualname__", handler),
                    event.value,
                )

    async def emit_payload(self, event: HookEvent, payload: object) -> None:
        """Dispatch a **typed payload** dataclass to all handlers for *event*.

        The dataclass is converted to a ``dict`` via :func:`dataclasses.asdict`
        before being forwarded to handlers, preserving full backward
        compatibility with handlers that expect raw dicts.

        Parameters
        ----------
        event:
            The event to emit.
        payload:
            An instance of one of the :mod:`~nexus.hooks.payloads` dataclasses
            **or** a plain ``dict`` (which is passed through unchanged).
        """
        if _is_dataclass_instance(payload):
            await self.emit(event, asdict(payload))  # type: ignore[arg-type]
        elif isinstance(payload, dict):
            await self.emit(event, payload)
        else:
            raise TypeError(
                f"emit_payload expects a dataclass instance or dict, got {type(payload).__name__!r}"
            )

    def handler_count(self, event: HookEvent) -> int:
        """Return the number of handlers registered for *event*."""
        return len(self._handlers.get(event, []))

    def registered_events(self) -> list[HookEvent]:
        """Return a sorted list of events that have at least one handler."""
        return sorted(
            (event for event, handlers in self._handlers.items() if handlers),
            key=lambda e: e.value,
        )
