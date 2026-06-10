"""Repository indexing and concept-search helpers."""
from __future__ import annotations

import json
import re
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind
from nexus.tools.builtin.python_index import get_python_workspace_index
from nexus.tools.utils import allow_hidden_reads


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
        index = get_python_workspace_index(workspace, allow_hidden=allow_hidden, max_files=max_files)
        for file_index in index.files:
            file_symbols = [
                {
                    "name": symbol.name,
                    "kind": symbol.syntax_kind or _legacy_symbol_kind(symbol.kind),
                    "path": file_index.relative_path,
                    "line": symbol.line,
                }
                for symbol in file_index.symbols
                if symbol.kind in {"class", "function"}
            ]
            files.append({
                "path": file_index.relative_path,
                "symbols": len(file_symbols),
                "imports": len(file_index.imports),
            })
            symbols.extend(file_symbols)
            imports[file_index.relative_path] = list(file_index.imports)
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
        index = get_python_workspace_index(workspace, allow_hidden=allow_hidden)
        for file_index in index.files:
            for line_index, line in enumerate(file_index.lines, start=1):
                haystack = line.lower()
                score = sum(1 for term in terms if term in haystack)
                if score == 0:
                    continue
                results.append({
                    "path": file_index.relative_path,
                    "line": line_index,
                    "score": score,
                    "text": line.strip()[:240],
                })
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


def _legacy_symbol_kind(kind: str) -> str:
    return {
        "class": "ClassDef",
        "function": "FunctionDef",
    }.get(kind, kind)
