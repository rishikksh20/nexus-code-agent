from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult


@dataclass(slots=True, frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True, frozen=True)
class MCPServerConfig:
    name: str
    command: tuple[str, ...]
    prefix: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MCPServerConfig":
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("MCP server entries require a non-empty 'name'.")
        raw_command = payload.get("command")
        if not isinstance(raw_command, list) or not raw_command:
            raise ValueError(f"MCP server '{name}' requires a non-empty command list.")
        command = tuple(str(part).strip() for part in raw_command if str(part).strip())
        if not command:
            raise ValueError(f"MCP server '{name}' requires a non-empty command list.")
        prefix = str(payload.get("prefix", "")).strip()
        return cls(name=name, command=command, prefix=prefix)


class MCPClient:
    def __init__(self, server: MCPServerConfig, *, timeout: float = 10.0) -> None:
        self.server = server
        self.timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def connect(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            *self.server.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "nexus-agent-framework", "version": "0.1.0"},
            },
        )
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[MCPToolSpec]:
        response = await self._rpc("tools/list", {})
        tools = response.get("tools", [])
        specs: list[MCPToolSpec] = []
        for item in tools:
            if not isinstance(item, dict):
                continue
            specs.append(
                MCPToolSpec(
                    name=str(item.get("name", "")).strip(),
                    description=str(item.get("description", "")).strip(),
                    input_schema=dict(item.get("inputSchema", {"type": "object", "properties": {}})),
                )
            )
        return [spec for spec in specs if spec.name]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        response = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = response.get("content", [])
        if isinstance(content, list):
            texts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
            text_output = "\n".join(text for text in texts if text)
            if text_output:
                return text_output
        return json.dumps(response, default=str)

    async def close(self) -> None:
        if self._process is None:
            return
        process = self._process
        self._process = None
        await _close_stream_writer(process.stdin)
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            _terminate_process(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                _kill_process(process)
                await process.wait()
        await asyncio.sleep(0)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return await self._read_response(request_id, method)

    async def _write_message(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP client is not connected.")
        message = json.dumps(payload) + "\n"
        self._process.stdin.write(message.encode("utf-8"))
        await self._process.stdin.drain()

    async def _read_response(self, request_id: int, method: str) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("MCP client is not connected.")
        while True:
            try:
                line = await asyncio.wait_for(self._process.stdout.readline(), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"MCP server '{self.server.name}' timed out during {method}.") from exc
            if not line:
                raise RuntimeError(f"MCP server '{self.server.name}' closed the connection during {method}.")
            response = json.loads(line.decode("utf-8"))
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"MCP server '{self.server.name}' returned an error: {response['error']}")
            result = response.get("result", {})
            return dict(result) if isinstance(result, dict) else {"result": result}


@dataclass(slots=True)
class MCPServerRuntime:
    server: MCPServerConfig
    client: MCPClient | None = None
    connected: bool = False
    registered_tools: tuple[str, ...] = ()
    discovered_tools: tuple[str, ...] = ()
    last_error: str | None = None
    last_checked_at: str | None = None

    async def refresh(self) -> tuple[str, ...]:
        self.last_checked_at = datetime.now(UTC).isoformat()
        try:
            specs = await self._list_tools()
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc)
            self.discovered_tools = ()
            return self.discovered_tools

        self.connected = True
        self.last_error = None
        self.discovered_tools = tuple(self.display_name(spec.name) for spec in specs)
        return self.discovered_tools

    async def close(self) -> None:
        if self.client is None:
            return
        client = self.client
        self.client = None
        await client.close()

    def display_name(self, tool_name: str) -> str:
        return f"{self.server.prefix}{tool_name}" if self.server.prefix else tool_name

    async def _list_tools(self) -> list[MCPToolSpec]:
        if self.client is None:
            self.client = MCPClient(self.server)
            await self.client.connect()
            return await self.client.list_tools()

        try:
            return await self.client.list_tools()
        except Exception:
            await self.client.close()
            self.client = MCPClient(self.server)
            await self.client.connect()
            return await self.client.list_tools()


class MCPToolAdapter:
    is_mutating = True

    def __init__(self, client: MCPClient, spec: MCPToolSpec, *, display_name: str) -> None:
        self._client = client
        self._remote_name = spec.name
        self.name = display_name
        self.description = spec.description or f"MCP tool from {client.server.name}."
        self.input_schema = spec.input_schema

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        del context
        try:
            output = await self._client.call_tool(self._remote_name, arguments)
        except Exception as exc:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"MCP tool '{self.name}' failed: {exc}",
                is_error=True,
                metadata={"source": "mcp", "server": self._client.server.name},
            )
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=output,
            metadata={"source": "mcp", "server": self._client.server.name},
        )


def mcp_server_example_for_workspace(workspace_root: Path) -> str:
    return (
        '{ name = "filesystem", command = ["uvx", "mcp-server-filesystem", '
        f'"{workspace_root.as_posix()}"], prefix = "fs_" }}'
    )


async def _close_stream_writer(writer) -> None:
    if writer is None or writer.is_closing():
        return
    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return
    try:
        await asyncio.wait_for(wait_closed(), timeout=1.0)
    except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
        return


def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return


def _kill_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
