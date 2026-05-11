"""Backward-compatibility shim for ``nexus.runtime.hooks``.

All hook types have moved to :mod:`nexus.hooks`.
This module re-exports them so that existing imports continue to work.
"""
# ruff: noqa: F401
from nexus.hooks.events import HookEvent
from nexus.hooks.executor import HookExecutor, HookHandler

__all__ = ["HookEvent", "HookExecutor", "HookHandler"]
