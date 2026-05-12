"""WebFetchTool — fetch the content of a URL.

Requires ``httpx`` (listed as a project dependency).  Returns the response
body as text.  Responses over 100 KB are truncated to protect the context
window.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind

_MAX_RESPONSE_BYTES = 100 * 1024   # 100 KB


class WebFetchTool(Tool):
    """Fetch content from a URL and return it as text.

    Only ``http://`` and ``https://`` URLs are accepted.
    """

    name = "web_fetch"
    description = (
        "Fetch content from a URL. Returns the response body as text. "
        "Only http:// and https:// URLs are supported."
    )
    kind = ToolKind.NETWORK
    is_mutating = False
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "minLength": 1,
                "description": "URL to fetch (must be http:// or https://).",
            },
            "timeout": {
                "type": "integer",
                "minimum": 5,
                "maximum": 120,
                "description": "Request timeout in seconds (default: 30).",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    }

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            import httpx
        except ImportError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="httpx is not installed. Run: pip install httpx",
                is_error=True,
            )

        url = str(arguments.get("url", "")).strip()
        if not url:
            return ToolResult(call_id=call_id, tool_name=self.name, output="Missing required argument: url", is_error=True)

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult(call_id=call_id, tool_name=self.name, output="URL must start with http:// or https://", is_error=True)

        timeout = int(arguments.get("timeout", 30))

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                text = response.text
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"HTTP {exc.response.status_code}: {exc.response.reason_phrase}",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Request failed: {exc}", is_error=True)

        if len(text) > _MAX_RESPONSE_BYTES:
            text = text[:_MAX_RESPONSE_BYTES] + "\n... [content truncated]"

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=text,
            metadata={"status_code": response.status_code, "url": url},
        )
