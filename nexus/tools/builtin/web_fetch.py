"""WebFetchTool — fetch the content of a URL.

Requires ``httpx`` (listed as a project dependency).  Returns the response
body as text.  Responses over 100 KB are truncated to protect the context
window.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind

_MAX_RESPONSE_BYTES = 100 * 1024   # 100 KB
_MAX_REDIRECTS = 10


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
        host_error = _validate_public_http_url(url)
        if host_error is not None:
            return ToolResult(call_id=call_id, tool_name=self.name, output=host_error, is_error=True)

        timeout = int(arguments.get("timeout", 30))

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False) as client:
                response = await _get_with_safe_redirects(client, url)
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


async def _get_with_safe_redirects(client: Any, url: str) -> Any:
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        host_error = _validate_public_http_url(current_url)
        if host_error is not None:
            raise ValueError(host_error)
        response = await client.get(current_url)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current_url = urljoin(str(response.url), location)
    raise ValueError(f"Too many redirects; stopped after {_MAX_REDIRECTS} redirects.")


def _validate_public_http_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "URL must start with http:// or https://"
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if not hostname:
        return "URL must include a hostname."
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return "Refusing to fetch localhost or private network URL."
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            resolved = {
                item[4][0]
                for item in socket.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            return f"Could not resolve host: {exc}"
        for address in resolved:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                return "Refusing to fetch URL with an invalid resolved address."
            if _is_blocked_ip(ip):
                return "Refusing to fetch localhost or private network URL."
        return None
    if _is_blocked_ip(ip):
        return "Refusing to fetch localhost or private network URL."
    return None


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )
