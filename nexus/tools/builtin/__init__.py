"""nexus.tools.builtin — all first-party Nexus tools.

Each tool lives in its own module following the reference code's organisation.
This ``__init__.py`` re-exports every tool class so that callers can do either:

    from nexus.tools.builtin import ReadFileTool, EditTool, MemoryTool
    from nexus.tools.builtin.edit_file import EditTool

The :func:`get_all_builtin_tools` factory returns a list of pre-constructed
tool instances ready for registration.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.tools.builtin.create_file import CreateFileTool
from nexus.tools.builtin.edit_file import EditTool
from nexus.tools.builtin.glob import GlobTool
from nexus.tools.builtin.grep import GrepTool
from nexus.tools.builtin.list_dir import ListDirTool, LsTool
from nexus.tools.builtin.memory import MemoryTool
from nexus.tools.builtin.note import WriteNoteTool
from nexus.tools.builtin.patch import ApplyPatchTool
from nexus.tools.builtin.read_file import ReadFileTool
from nexus.tools.builtin.shell import BashTool, ShellTool
from nexus.tools.builtin.smart_edit import InsertEditIntoFileTool
from nexus.tools.builtin.time import GetTimeTool
from nexus.tools.builtin.todo import TodoTool
from nexus.tools.builtin.web_fetch import WebFetchTool
from nexus.tools.builtin.web_search import WebSearchTool
from nexus.tools.builtin.write_file import WriteFileTool

__all__ = [
    # Core / time
    "GetTimeTool",
    # File I/O
    "ReadFileTool",
    "WriteFileTool",
    "CreateFileTool",          # create-only (refuse to overwrite)
    "EditTool",
    "InsertEditIntoFileTool",  # semantic context-anchor editor
    # Patching
    "ApplyPatchTool",
    # Search
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "LsTool",          # alias
    # Execution
    "ShellTool",
    "BashTool",        # alias (name = "bash", backward compat)
    # Memory & tasks
    "MemoryTool",
    "TodoTool",
    "WriteNoteTool",
    # Network
    "WebFetchTool",
    "WebSearchTool",
]


def get_all_builtin_tools(
    *,
    write_note_max_bytes: int = 65_536,
    memory_dir: Path | None = None,
) -> list[Any]:
    """Return a list of pre-constructed builtin tool instances.

    Parameters
    ----------
    write_note_max_bytes:
        Size cap for :class:`WriteNoteTool` (default: 64 KB).
    memory_dir:
        Override the directory where :class:`MemoryTool` persists data.
        Defaults to ``~/.nexus/memory/``.
    """
    return [
        GetTimeTool(),
        ReadFileTool(),
        WriteFileTool(),
        CreateFileTool(),
        EditTool(),
        InsertEditIntoFileTool(),
        ApplyPatchTool(),
        GlobTool(),
        GrepTool(),
        ListDirTool(),
        ShellTool(),
        MemoryTool(memory_dir=memory_dir),
        TodoTool(),
        WriteNoteTool(max_bytes=write_note_max_bytes),
        WebFetchTool(),
        WebSearchTool(),
    ]
