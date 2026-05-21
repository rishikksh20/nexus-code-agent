"""Compatibility write_note tool.

The legacy tool remains importable for tests and external callers that register
it explicitly, but it is intentionally not part of the default core registry.
"""
from __future__ import annotations

from nexus.tools.builtin.write_file import WriteFileTool


class WriteNoteTool(WriteFileTool):
    name = "write_note"
    description = "Compatibility alias for writing a note file. Prefer write_file in new code."
