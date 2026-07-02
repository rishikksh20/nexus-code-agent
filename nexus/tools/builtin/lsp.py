"""Read-only Python code intelligence helpers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.builtin.python_index import (
    PYTHON_GLOB_SUFFIX,
    IndexedSymbol,
    get_document_index,
    get_python_workspace_index,
    iter_python_files as indexed_python_files,
)
from nexus.tools.utils import allow_hidden_reads, read_path_policy_error, resolve_path

PythonLspOperation = Literal[
    "document_symbol",
    "workspace_symbol",
    "go_to_definition",
    "find_references",
    "hover",
]

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_RESULTS = 200
_MAX_PYTHON_FILES = 2_000


@dataclass(frozen=True)
class SymbolLocation:
    """A Python symbol location inside the current workspace."""

    name: str
    kind: str
    path: Path
    line: int
    character: int
    signature: str = ""
    docstring: str = ""


class PythonLspTool(Tool):
    """Lightweight Python code-intelligence operations."""

    name = "lsp"
    description = (
        "Inspect Python symbols, definitions, references, and hover information "
        "across the current workspace."
    )
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "document_symbol",
                    "workspace_symbol",
                    "go_to_definition",
                    "find_references",
                    "hover",
                ],
                "description": "The code intelligence operation to perform.",
            },
            "file_path": {
                "type": "string",
                "description": "Python source file path, relative to the workspace root.",
            },
            "symbol": {
                "type": "string",
                "description": "Explicit symbol name to look up.",
            },
            "line": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based line number for position-based lookups.",
            },
            "character": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based character offset for position-based lookups.",
            },
            "query": {
                "type": "string",
                "description": "Substring query for workspace_symbol.",
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    }

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        operation = str(arguments.get("operation", "")).strip()
        if operation not in {
            "document_symbol",
            "workspace_symbol",
            "go_to_definition",
            "find_references",
            "hover",
        }:
            return self._error(call_id, "Missing or invalid operation.")

        workspace = context.working_directory.resolve()
        allow_hidden = allow_hidden_reads(context.metadata)

        if operation == "workspace_symbol":
            query = str(arguments.get("query", "")).strip()
            if not query:
                return self._error(call_id, "workspace_symbol requires query.")
            try:
                results = workspace_symbol_search(workspace, query, allow_hidden=allow_hidden)
            except OSError as exc:
                return self._error(call_id, f"Workspace scan failed: {exc}")
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=_format_symbol_locations(results, workspace),
                metadata={"count": len(results)},
            )

        raw_file_path = str(arguments.get("file_path", "")).strip()
        if not raw_file_path:
            return self._error(call_id, f"{operation} requires file_path.")

        file_path = resolve_path(workspace, raw_file_path)
        path_error = _validate_python_file(file_path, workspace, raw_file_path, allow_hidden=allow_hidden)
        if path_error is not None:
            return self._error(call_id, path_error)

        if operation == "document_symbol":
            try:
                results = list_document_symbols(file_path)
            except (OSError, SyntaxError) as exc:
                return self._error(call_id, f"Could not parse Python file: {exc}")
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=_format_symbol_locations(results, workspace),
                metadata={"count": len(results), "path": _display_path(file_path, workspace)},
            )

        symbol = arguments.get("symbol")
        symbol_name = str(symbol).strip() if symbol is not None else None
        line = _optional_int(arguments.get("line"))
        character = _optional_int(arguments.get("character"))
        if not symbol_name and line is None:
            return self._error(call_id, f"{operation} requires symbol or line.")

        if operation == "find_references":
            try:
                references = find_references(
                    root=workspace,
                    file_path=file_path,
                    symbol=symbol_name,
                    line=line,
                    character=character,
                    allow_hidden=allow_hidden,
                )
            except OSError as exc:
                return self._error(call_id, f"Reference search failed: {exc}")
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=_format_references(references, workspace),
                metadata={"count": len(references)},
            )

        try:
            definitions = go_to_definition(
                root=workspace,
                file_path=file_path,
                symbol=symbol_name,
                line=line,
                character=character,
                allow_hidden=allow_hidden,
            )
        except (OSError, SyntaxError) as exc:
            return self._error(call_id, f"Definition search failed: {exc}")

        if operation == "go_to_definition":
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=_format_symbol_locations(definitions, workspace),
                metadata={"count": len(definitions)},
            )

        result = definitions[0] if definitions else None
        if result is None:
            return ToolResult(call_id=call_id, tool_name=self.name, output="(no hover result)")
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=_format_hover(result, workspace),
            metadata={"count": 1},
        )

    def _error(self, call_id: str, output: str) -> ToolResult:
        return ToolResult(call_id=call_id, tool_name=self.name, output=output, is_error=True)


def list_document_symbols(path: Path) -> list[SymbolLocation]:
    """Return top-level and nested Python symbols from one source file."""
    return [_to_symbol_location(symbol) for symbol in get_document_index(path).symbols]


def workspace_symbol_search(root: Path, query: str, *, allow_hidden: bool = False) -> list[SymbolLocation]:
    """Return workspace symbols whose name contains ``query``."""
    needle = query.lower().strip()
    if not needle:
        return []
    matches: list[SymbolLocation] = []
    index = get_python_workspace_index(root, allow_hidden=allow_hidden, max_files=_MAX_PYTHON_FILES)
    for file_index in index.files:
        for symbol in file_index.symbols:
            if needle in symbol.name.lower():
                matches.append(_to_symbol_location(symbol))
                if len(matches) >= _MAX_RESULTS:
                    return matches
    return matches


def go_to_definition(
    *,
    root: Path,
    file_path: Path,
    symbol: str | None = None,
    line: int | None = None,
    character: int | None = None,
    allow_hidden: bool = False,
) -> list[SymbolLocation]:
    """Resolve candidate definitions for a symbol."""
    target = symbol or extract_symbol_at_position(file_path, line=line, character=character)
    if not target:
        return []
    matches: list[SymbolLocation] = []
    index = get_python_workspace_index(root, allow_hidden=allow_hidden, max_files=_MAX_PYTHON_FILES)
    for file_index in index.files:
        for item in file_index.symbols:
            if _symbol_matches(item.name, target):
                matches.append(_to_symbol_location(item))
                if len(matches) >= _MAX_RESULTS:
                    return matches
    return matches


def find_references(
    *,
    root: Path,
    file_path: Path,
    symbol: str | None = None,
    line: int | None = None,
    character: int | None = None,
    allow_hidden: bool = False,
) -> list[tuple[Path, int, str]]:
    """Return line-oriented references for a Python symbol."""
    target = symbol or extract_symbol_at_position(file_path, line=line, character=character)
    if not target:
        return []
    pattern = re.compile(rf"\b{re.escape(target)}\b")
    matches: list[tuple[Path, int, str]] = []
    index = get_python_workspace_index(root, allow_hidden=allow_hidden, max_files=_MAX_PYTHON_FILES)
    for file_index in index.files:
        for lineno, raw_line in enumerate(file_index.lines, start=1):
            if pattern.search(raw_line):
                matches.append((file_index.path, lineno, raw_line.strip()))
                if len(matches) >= _MAX_RESULTS:
                    return matches
    return matches


def extract_symbol_at_position(
    file_path: Path,
    *,
    line: int | None,
    character: int | None,
) -> str | None:
    """Extract a probable identifier from a 1-based line/character position."""
    if line is None:
        return None
    lines = list(get_document_index(file_path).lines)
    if line < 1 or line > len(lines):
        return None
    text = lines[line - 1]
    if not text:
        return None
    index = max(0, min((character or 1) - 1, len(text) - 1))
    first_identifier: str | None = None
    for match in _IDENTIFIER_RE.finditer(text):
        first_identifier = first_identifier or match.group(0)
        if match.start() <= index < match.end():
            return match.group(0)
    return first_identifier


def iter_python_files(root: Path, *, allow_hidden: bool = False) -> list[Path]:
    """Return Python source files in stable workspace order."""
    return list(indexed_python_files(root, allow_hidden=allow_hidden, max_files=_MAX_PYTHON_FILES))


def _validate_python_file(path: Path, workspace: Path, raw_path: str, *, allow_hidden: bool) -> str | None:
    try:
        path.relative_to(workspace)
    except ValueError:
        return "Refusing to access paths outside the current workspace."
    policy_error = read_path_policy_error(path, workspace, allow_hidden=allow_hidden)
    if policy_error is not None:
        return policy_error
    if not path.exists():
        return f"File not found: {raw_path}"
    if not path.is_file():
        return f"Path is not a file: {raw_path}"
    if path.suffix != PYTHON_GLOB_SUFFIX:
        return "The lsp tool currently supports Python files only."
    return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _symbol_matches(candidate: str, target: str) -> bool:
    return candidate == target or candidate.rsplit(".", 1)[-1] == target


def _to_symbol_location(symbol: IndexedSymbol) -> SymbolLocation:
    return SymbolLocation(
        name=symbol.name,
        kind=symbol.kind,
        path=symbol.path,
        line=symbol.line,
        character=symbol.character,
        signature=symbol.signature,
        docstring=symbol.docstring,
    )


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _format_symbol_locations(results: list[SymbolLocation], root: Path) -> str:
    if not results:
        return "(no results)"
    lines: list[str] = []
    for item in results:
        lines.append(f"{item.kind} {item.name} - {_display_path(item.path, root)}:{item.line}:{item.character}")
        if item.signature:
            lines.append(f"  signature: {item.signature}")
        if item.docstring:
            lines.append(f"  docstring: {item.docstring.strip()}")
    if len(results) >= _MAX_RESULTS:
        lines.append(f"... [truncated to {_MAX_RESULTS} results]")
    return "\n".join(lines)


def _format_references(results: list[tuple[Path, int, str]], root: Path) -> str:
    if not results:
        return "(no results)"
    lines = [f"{_display_path(path, root)}:{line}:{text}" for path, line, text in results]
    if len(results) >= _MAX_RESULTS:
        lines.append(f"... [truncated to {_MAX_RESULTS} results]")
    return "\n".join(lines)


def _format_hover(result: SymbolLocation, root: Path) -> str:
    parts = [
        f"{result.kind} {result.name}",
        f"path: {_display_path(result.path, root)}:{result.line}:{result.character}",
    ]
    if result.signature:
        parts.append(f"signature: {result.signature}")
    if result.docstring:
        parts.append(f"docstring: {result.docstring.strip()}")
    return "\n".join(parts)


__all__ = [
    "PythonLspTool",
    "SymbolLocation",
    "extract_symbol_at_position",
    "find_references",
    "go_to_definition",
    "iter_python_files",
    "list_document_symbols",
    "workspace_symbol_search",
]
