from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import ToolKind, ToolRegistry


@dataclass(slots=True, frozen=True)
class MCPToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True, frozen=True)
class MCPServerConfig:
    name: str
    command: tuple[str, ...]
    transport: str = "stdio"
    url: str = ""
    prefix: str = ""
    env: dict[str, str] | None = None
    cwd: str | None = None
    startup_timeout_seconds: float = 10.0
    tool_timeout_seconds: float = 60.0
    disabled: bool = False
    disabled_tools: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MCPServerConfig":
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("MCP server entries require a non-empty 'name'.")
        transport = str(payload.get("transport", "stdio")).strip().lower() or "stdio"
        raw_command = payload.get("command")
        command: tuple[str, ...] = ()
        if raw_command is not None:
            if not isinstance(raw_command, list):
                raise ValueError(f"MCP server '{name}' command must be a list.")
            command = tuple(str(part).strip() for part in raw_command if str(part).strip())
        url = str(payload.get("url", "")).strip()
        if transport == "stdio" and not command:
            raise ValueError(f"MCP server '{name}' requires a non-empty command list.")
        if transport in {"http", "streamable_http", "streamable-http"} and not url:
            raise ValueError(f"MCP server '{name}' requires a non-empty url.")
        if transport not in {"stdio", "http", "streamable_http", "streamable-http"}:
            raise ValueError(f"MCP server '{name}' has unsupported transport '{transport}'.")
        prefix = str(payload.get("prefix", "")).strip()
        raw_env = payload.get("env")
        env = None
        if isinstance(raw_env, dict):
            env = {str(key): str(value) for key, value in raw_env.items()}
        raw_disabled_tools = payload.get("disabled_tools", [])
        disabled_tools: tuple[str, ...] = ()
        if isinstance(raw_disabled_tools, list):
            disabled_tools = tuple(str(item).strip() for item in raw_disabled_tools if str(item).strip())
        return cls(
            name=name,
            command=command,
            transport="streamable_http" if transport == "streamable-http" else transport,
            url=url,
            prefix=prefix,
            env=env,
            cwd=str(payload.get("cwd")).strip() if payload.get("cwd") else None,
            startup_timeout_seconds=float(payload.get("startup_timeout_seconds", 10.0)),
            tool_timeout_seconds=float(payload.get("tool_timeout_seconds", 60.0)),
            disabled=bool(payload.get("disabled", False)),
            disabled_tools=disabled_tools,
        )


@dataclass(slots=True, frozen=True)
class MCPCallResult:
    text: str
    is_error: bool = False
    structured_content: dict[str, Any] | None = None
    raw_response: dict[str, Any] | None = None


@dataclass(slots=True, frozen=True)
class MCPRefreshReport:
    server: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    failed: str | None = None


class MCPClient:
    def __init__(self, server: MCPServerConfig) -> None:
        self.server = server
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._stderr_task: asyncio.Task[None] | None = None
        self._message_framing = "headers"
        self._framing_rejected: asyncio.Event | None = None
        self._framing_rejected_detail: str | None = None

    async def connect(self) -> None:
        try:
            await self._connect()
        except RuntimeError as exc:
            message = str(exc)
            if self._message_framing == "headers" and (
                "timed out during initialize" in message
                or "closed the connection during initialize" in message
                or "rejected header framing during initialize" in message
            ):
                await self.close(terminate="rejected header framing during initialize" in message)
                self._message_framing = "lines"
                await self._connect()
                return
            raise

    async def _connect(self) -> None:
        if self.server.transport != "stdio":
            raise RuntimeError(
                f"MCP transport '{self.server.transport}' is configured but only stdio is available."
            )
        self._process = await asyncio.create_subprocess_exec(
            *self.server.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(self.server.env or {})},
            cwd=self.server.cwd,
        )
        self._framing_rejected = asyncio.Event()
        self._framing_rejected_detail = None
        self._stderr_task = asyncio.create_task(
            _drain_stderr(
                self.server.name,
                self._process.stderr,
                on_line=self._handle_stderr_line,
            )
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
        result = _parse_tool_result(response)
        if result.text:
            return result.text
        return json.dumps(response, default=str)

    async def call_tool_result(self, name: str, arguments: dict[str, Any]) -> MCPCallResult:
        response = await self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout=self.server.tool_timeout_seconds)
        return _parse_tool_result(response)

    async def close(self, *, terminate: bool = False) -> None:
        stderr_task = self._stderr_task
        self._stderr_task = None
        if self._process is None:
            if stderr_task is not None:
                stderr_task.cancel()
            return
        process = self._process
        self._process = None
        self._framing_rejected = None
        self._framing_rejected_detail = None
        await _close_stream_writer(process.stdin)
        if terminate:
            _terminate_process(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            _terminate_process(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                _kill_process(process)
                await process.wait()
        if stderr_task is not None:
            try:
                await asyncio.wait_for(stderr_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                stderr_task.cancel()
        await asyncio.sleep(0)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    async def _rpc(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
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
        return await self._read_response(request_id, method, timeout=timeout or self.server.startup_timeout_seconds)

    async def _write_message(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP client is not connected.")
        if self._message_framing == "lines":
            self._process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
            await self._process.stdin.drain()
            return
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + body)
        await self._process.stdin.drain()

    async def _read_response(self, request_id: int, method: str, *, timeout: float) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("MCP client is not connected.")
        framing_task: asyncio.Task[bool] | None = None
        while True:
            response_task = asyncio.create_task(_read_jsonrpc_message(self._process.stdout))
            if self._message_framing == "headers" and self._framing_rejected is not None:
                framing_task = asyncio.create_task(self._framing_rejected.wait())
            try:
                if framing_task is None:
                    response = await asyncio.wait_for(response_task, timeout=timeout)
                else:
                    done, pending = await asyncio.wait(
                        {response_task, framing_task},
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        raise asyncio.TimeoutError
                    if framing_task in done:
                        response_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await response_task
                        detail = f": {self._framing_rejected_detail}" if self._framing_rejected_detail else ""
                        raise RuntimeError(
                            f"MCP server '{self.server.name}' rejected header framing during {method}{detail}"
                        )
                    response = response_task.result()
            except asyncio.TimeoutError as exc:
                response_task.cancel()
                with suppress(asyncio.CancelledError):
                    await response_task
                raise RuntimeError(f"MCP server '{self.server.name}' timed out during {method}.") from exc
            finally:
                if framing_task is not None:
                    framing_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await framing_task
                    framing_task = None
            if response is None:
                raise RuntimeError(f"MCP server '{self.server.name}' closed the connection during {method}.")
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(f"MCP server '{self.server.name}' returned an error: {response['error']}")
            result = response.get("result", {})
            return dict(result) if isinstance(result, dict) else {"result": result}

    def _handle_stderr_line(self, text: str) -> None:
        if self._message_framing != "headers" or self._framing_rejected is None:
            return
        normalized = text.lower()
        if "content-length" not in normalized:
            return
        if "invalid json" not in normalized and "jsonrpcmessage" not in normalized:
            return
        if not self._framing_rejected.is_set():
            self._framing_rejected_detail = text
            self._framing_rejected.set()


def _parse_tool_result(response: dict[str, Any]) -> MCPCallResult:
    structured = response.get("structuredContent")
    if not isinstance(structured, dict):
        structured = None
    content = response.get("content", [])
    texts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                texts.append(str(item["text"]))
    text_output = "\n".join(texts)
    if not text_output and structured is not None:
        text_output = json.dumps(structured, default=str)
    is_error = bool(response.get("isError", response.get("is_error", False)))
    return MCPCallResult(
        text=text_output or json.dumps(response, default=str),
        is_error=is_error,
        structured_content=structured,
        raw_response=response,
    )


async def _read_jsonrpc_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    first = await reader.readline()
    if not first:
        return None
    if first.lstrip().startswith(b"{"):
        return json.loads(first.decode("utf-8"))

    headers: dict[str, str] = {}
    line = first
    while line not in {b"\r\n", b"\n", b""}:
        decoded = line.decode("ascii", errors="replace")
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        line = await reader.readline()
    raw_length = headers.get("content-length")
    if not raw_length:
        raise RuntimeError("MCP server sent a framed message without Content-Length.")
    body = await reader.readexactly(int(raw_length))
    return json.loads(body.decode("utf-8"))


async def _drain_stderr(
    server_name: str,
    stream: asyncio.StreamReader | None,
    *,
    on_line: Callable[[str], None] | None = None,
) -> None:
    if stream is None:
        return
    import logging

    logger = logging.getLogger(__name__)
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            if on_line is not None:
                on_line(text)
            logger.debug("[MCP stderr:%s] %s", server_name, text)


@dataclass(slots=True)
class MCPServerRuntime:
    server: MCPServerConfig
    client: MCPClient | None = None
    connected: bool = False
    registered_tools: tuple[str, ...] = ()
    discovered_tools: tuple[str, ...] = ()
    discovered_specs: tuple[MCPToolSpec, ...] = ()
    last_error: str | None = None
    last_checked_at: str | None = None

    async def refresh(self) -> tuple[str, ...]:
        self.last_checked_at = datetime.now(UTC).isoformat()
        if self.server.disabled:
            self.connected = False
            self.last_error = "disabled"
            self.discovered_specs = ()
            self.discovered_tools = ()
            return self.discovered_tools
        try:
            specs = await self._list_tools()
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc)
            self.discovered_specs = ()
            self.discovered_tools = ()
            return self.discovered_tools

        self.connected = True
        self.last_error = None
        self.discovered_specs = tuple(specs)
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
            try:
                await self.client.connect()
                return await self.client.list_tools()
            except Exception:
                await self.client.close()
                self.client = None
                raise

        try:
            return await self.client.list_tools()
        except Exception:
            await self.client.close()
            self.client = MCPClient(self.server)
            try:
                await self.client.connect()
                return await self.client.list_tools()
            except Exception:
                await self.client.close()
                self.client = None
                raise


class MCPToolAdapter:
    is_mutating = True
    kind = ToolKind.MCP

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
            result = await self._client.call_tool_result(self._remote_name, arguments)
        except Exception as exc:
            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=f"MCP tool '{self.name}' failed: {exc}",
                is_error=True,
                metadata={
                    "source": "mcp",
                    "server": self._client.server.name,
                    "remote_tool": self._remote_name,
                    "transport": self._client.server.transport,
                },
            )
        metadata: dict[str, Any] = {
            "source": "mcp",
            "server": self._client.server.name,
            "remote_tool": self._remote_name,
            "transport": self._client.server.transport,
        }
        if result.structured_content is not None:
            metadata["structured_content"] = result.structured_content
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=result.text,
            is_error=result.is_error,
            metadata=metadata,
        )


async def refresh_mcp_server_tools(runtime: MCPServerRuntime, registry: ToolRegistry, config) -> MCPRefreshReport:
    before = {
        record.name
        for record in registry.records()
        if record.source == "mcp" and record.origin == runtime.server.name
    }
    await runtime.refresh()
    if runtime.last_error:
        return MCPRefreshReport(server=runtime.server.name, failed=runtime.last_error)

    removed = set(registry.unregister_source(source="mcp", origin=runtime.server.name))
    registered = _register_runtime_tools(runtime, registry, config)
    after = set(registered)
    return MCPRefreshReport(
        server=runtime.server.name,
        added=tuple(sorted(after - before)),
        removed=tuple(sorted(removed - after)),
        unchanged=tuple(sorted(after & before)),
    )


def register_discovered_mcp_tools(runtime: MCPServerRuntime, registry: ToolRegistry, config) -> tuple[str, ...]:
    return _register_runtime_tools(runtime, registry, config)


def _register_runtime_tools(runtime: MCPServerRuntime, registry: ToolRegistry, config) -> tuple[str, ...]:
    client = runtime.client
    if client is None or runtime.server.disabled:
        runtime.registered_tools = ()
        return runtime.registered_tools

    disabled_remote_tools = set(runtime.server.disabled_tools)
    registered: list[str] = []
    for spec in runtime.discovered_specs:
        if spec.name in disabled_remote_tools:
            continue
        display_name = runtime.display_name(spec.name)
        try:
            registry.register(
                MCPToolAdapter(client, spec, display_name=display_name),
                source="mcp",
                origin=runtime.server.name,
            )
        except ValueError:
            continue
        registered.append(display_name)
    runtime.registered_tools = tuple(registered)
    return runtime.registered_tools


def mcp_server_example_for_workspace(workspace_root: Path) -> str:
    return (
        '{ name = "filesystem", transport = "stdio", command = ["mcp-server-filesystem", '
        f'"{workspace_root.as_posix()}"], prefix = "mcp_fs_" }}'
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
