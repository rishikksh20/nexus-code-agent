"""WebSearchTool — DuckDuckGo web search.

Requires the ``ddgs`` package (optional).  Returns titles, URLs, and snippets
for the top results.  Gracefully reports when the package is not installed.
"""
from __future__ import annotations

from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind


class WebSearchTool(Tool):
    """Search the web via DuckDuckGo and return titles, URLs, and snippets.

    Requires ``ddgs`` (``pip install ddgs``).  The tool reports a clear error
    if the package is absent rather than crashing.
    """

    name = "web_search"
    description = (
        "Search the web for information. "
        "Returns search results with titles, URLs and snippets."
    )
    kind = ToolKind.NETWORK
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Search query.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum number of results to return (default: 10).",
            },
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
        try:
            from ddgs import DDGS
        except ImportError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="ddgs is not installed. Run: pip install ddgs",
                is_error=True,
            )

        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: query", is_error=True)

        max_results = int(arguments.get("max_results", 10))

        try:
            results = list(DDGS().text(
                query,
                region="us-en",
                safesearch="off",
                timelimit="y",
                max_results=max_results,
            ))
        except Exception as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Search failed: {exc}", is_error=True)

        if not results:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"No results found for: {query}",
                metadata={"results": 0},
            )

        lines = [f"Search results for: {query}", ""]
        for i, result in enumerate(results, start=1):
            lines.append(f"{i}. {result.get('title', '(no title)')}")
            lines.append(f"   URL: {result.get('href', '')}")
            if result.get("body"):
                lines.append(f"   {result['body']}")
            lines.append("")

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output="\n".join(lines),
            metadata={"query": query, "results": len(results)},
        )
