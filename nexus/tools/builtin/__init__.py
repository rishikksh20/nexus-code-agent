"""nexus.tools.builtin — all first-party Nexus tools.

Each tool lives in its own module following the reference code's organisation.
This ``__init__.py`` re-exports every tool class so that callers can do either:

    from nexus.tools.builtin import ReadFileTool, EditTool, MemoryTool
    from nexus.tools.builtin.edit_file import EditTool
"""
from __future__ import annotations

from nexus.tools.builtin.edit_file import EditTool
from nexus.tools.builtin.code_search import CodeIndexTool, FindReferencesTool, SemanticSearchTool
from nexus.tools.builtin.git import GitDiffTool, GitStatusTool
from nexus.tools.builtin.glob import GlobTool
from nexus.tools.builtin.grep import GrepTool
from nexus.tools.builtin.list_dir import ListDirTool, LsTool
from nexus.tools.builtin.lsp import PythonLspTool
from nexus.tools.builtin.memory import MemoryTool
from nexus.tools.builtin.patch import ApplyPatchTool
from nexus.tools.builtin.read_file import ReadFileTool
from nexus.tools.builtin.shell import BashTool, ShellTool
from nexus.tools.builtin.smart_edit import InsertEditIntoFileTool
from nexus.tools.builtin.time import GetTimeTool
from nexus.tools.builtin.todo import TodoTool
from nexus.tools.builtin.web_fetch import WebFetchTool
from nexus.tools.builtin.web_search import WebSearchTool
from nexus.tools.builtin.write_file import WriteFileTool
from nexus.tools.builtin.verification import RunFormatterTool, RunLinterTool, RunTestsTool, RunTypecheckTool

__all__ = [
    "GetTimeTool",
    # File I/O
    "ReadFileTool",
    "WriteFileTool",
    "EditTool",
    "InsertEditIntoFileTool",
    # Patching
    "ApplyPatchTool",
    # Search
    "GlobTool",
    "GrepTool",
    "ListDirTool",
    "LsTool",
    "PythonLspTool",
    "FindReferencesTool",
    "CodeIndexTool",
    "SemanticSearchTool",
    "GitStatusTool",
    "GitDiffTool",
    "RunTestsTool",
    "RunLinterTool",
    "RunTypecheckTool",
    "RunFormatterTool",
    # Execution
    "ShellTool",
    "BashTool",
    # Memory & tasks
    "MemoryTool",
    "TodoTool",
    # Network
    "WebFetchTool",
    "WebSearchTool",
]
