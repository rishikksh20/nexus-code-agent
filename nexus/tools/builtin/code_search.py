"""Repository indexing and concept-search helpers."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.utils import allow_hidden_reads, root_walk


_SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules", ".nexus", "reference_code"})
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class CodeIndexTool(Tool):
    name = "code_index"
    description = "Build a lightweight Python AST index of files, imports, classes, and functions."
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "max_files": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "Maximum Python files to index."},
        },
        "additionalProperties": False,
    }

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        workspace = context.working_directory.resolve()
        allow_hidden = allow_hidden_reads(context.metadata)
        max_files = int(arguments.get("max_files", 1000))
        files: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        imports: dict[str, list[str]] = {}
        for file_path in _iter_python_files(workspace, allow_hidden=allow_hidden):
            if len(files) >= max_files:
                break
            rel = str(file_path.relative_to(workspace))
            try:
                tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            except (OSError, SyntaxError):
                continue
            file_symbols, file_imports = _index_tree(tree, rel)
            files.append({"path": rel, "symbols": len(file_symbols), "imports": len(file_imports)})
            symbols.extend(file_symbols)
            imports[rel] = file_imports
        payload = {
            "files": files,
            "symbols": symbols[:1000],
            "imports": imports,
            "file_count": len(files),
            "symbol_count": len(symbols),
        }
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=json.dumps(payload, indent=2),
            metadata={"file_count": len(files), "symbol_count": len(symbols)},
        )


class SemanticSearchTool(Tool):
    name = "semantic_search"
    description = "Search Python code by concept using symbol names and lexical matches."
    kind = ToolKind.READ
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "description": "Concept or terms to search for."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum results."},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult(call_id=call_id, tool_name=self.name, output="query is required.", is_error=True)
        max_results = int(arguments.get("max_results", 20))
        terms = {term.lower() for term in _IDENTIFIER_RE.findall(query)}
        workspace = context.working_directory.resolve()
        allow_hidden = allow_hidden_reads(context.metadata)
        results: list[dict[str, Any]] = []
        for file_path in _iter_python_files(workspace, allow_hidden=allow_hidden):
            rel = str(file_path.relative_to(workspace))
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines, start=1):
                haystack = line.lower()
                score = sum(1 for term in terms if term in haystack)
                if score == 0:
                    continue
                results.append({"path": rel, "line": index, "score": score, "text": line.strip()[:240]})
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break
        results.sort(key=lambda item: (-int(item["score"]), str(item["path"]), int(item["line"])))
        payload = {"query": query, "results": results[:max_results], "count": min(len(results), max_results)}
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=json.dumps(payload, indent=2),
            metadata={"count": payload["count"]},
        )


def _iter_python_files(root: Path, *, allow_hidden: bool) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in root_walk(root):
        current_dir = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _SKIP_DIRS and (allow_hidden or not name.startswith("."))
        ]
        for filename in filenames:
            path = current_dir / filename
            if path.suffix != ".py":
                continue
            relative_parts = path.relative_to(root).parts
            if any(part in _SKIP_DIRS for part in relative_parts):
                continue
            if not allow_hidden and any(part.startswith(".") for part in relative_parts):
                continue
            files.append(path)
    return sorted(files)


def _index_tree(tree: ast.AST, path: str) -> tuple[list[dict[str, Any]], list[str]]:
    symbols: list[dict[str, Any]] = []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append({"name": node.name, "kind": node.__class__.__name__, "path": path, "line": node.lineno})
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.extend(f"{module}.{alias.name}".strip(".") for alias in node.names)
    return symbols, sorted(set(imports))
