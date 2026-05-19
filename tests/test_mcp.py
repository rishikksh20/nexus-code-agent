from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from nexus.config import load_config
from nexus.tools.mcp import (
    MCPCallResult,
    MCPClient,
    MCPServerConfig,
    MCPServerRuntime,
    MCPToolAdapter,
    MCPToolSpec,
    register_discovered_mcp_tools,
)
from nexus.models import ToolExecutionContext
from nexus.sandbox.agent_tool import SubagentDefinition
from nexus.tools import MCPServerConfig as ExportedMCPServerConfig
from nexus.tools.base import ToolKind, ToolRegistry
from nexus.tools.subagents import register_subagent_tools


SERVER_SCRIPT = """
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    length = int(headers["content-length"])
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii") + body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    if "id" not in message:
        continue
    request_id = message["id"]
    if method == "initialize":
        response = {"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {"tools": {}}}}
    elif method == "tools/list":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo the provided text.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        }
    elif method == "tools/call":
        text = message["params"]["arguments"].get("text", "")
        if text == "structured":
            result = {"structuredContent": {"ok": True, "value": 7}, "content": []}
        elif text == "error":
            result = {"isError": True, "content": [{"type": "text", "text": "remote failed"}]}
        else:
            result = {"content": [{"type": "text", "text": f"echo:{text}"}]}
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
    else:
        response = {"jsonrpc": "2.0", "id": request_id, "error": {"message": "unknown method"}}
    write_message(response)
"""


LINE_SERVER_SCRIPT = """
import json
import sys

for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if "id" not in message:
        continue
    request_id = message["id"]
    if method == "initialize":
        response = {"jsonrpc": "2.0", "id": request_id, "result": {"capabilities": {"tools": {}}}}
    elif method == "tools/list":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object", "properties": {}}}]},
        }
    else:
        response = {"jsonrpc": "2.0", "id": request_id, "result": {"content": []}}
    sys.stdout.write(json.dumps(response) + "\\n")
    sys.stdout.flush()
"""


@pytest.mark.asyncio
async def test_mcp_client_lists_and_calls_tools(tmp_path):
    server_path = tmp_path / "fake_mcp_server.py"
    server_path.write_text(SERVER_SCRIPT, encoding="utf-8")

    client = MCPClient(
        MCPServerConfig(name="fake", command=(sys.executable, str(server_path)), prefix="fs_"),
    )
    await client.connect()
    try:
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["echo"]
        assert await client.call_tool("echo", {"text": "hello"}) == "echo:hello"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_client_falls_back_to_json_lines_stdio(tmp_path):
    server_path = tmp_path / "line_mcp_server.py"
    server_path.write_text(LINE_SERVER_SCRIPT, encoding="utf-8")

    client = MCPClient(
        MCPServerConfig(
            name="line",
            command=(sys.executable, str(server_path)),
            startup_timeout_seconds=0.05,
        ),
    )
    await client.connect()
    try:
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["echo"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_tool_adapter_registers_and_executes(tmp_path):
    server_path = tmp_path / "fake_mcp_server.py"
    server_path.write_text(SERVER_SCRIPT, encoding="utf-8")

    client = MCPClient(MCPServerConfig(name="fake", command=(sys.executable, str(server_path))))
    await client.connect()
    try:
        spec = (await client.list_tools())[0]
        adapter = MCPToolAdapter(client, spec, display_name="fs_echo")
        registry = ToolRegistry()
        registry.register(adapter, source="mcp", origin="fake")

        result = await registry.get("fs_echo").execute(
            "call-1",
            {"text": "world"},
            ToolExecutionContext(session_id="mcp", working_directory=tmp_path),
        )

        assert result.output == "echo:world"
        assert result.metadata["source"] == "mcp"
        assert result.metadata["server"] == "fake"
        assert result.metadata["remote_tool"] == "echo"
        assert result.metadata["transport"] == "stdio"
        assert adapter.kind is ToolKind.MCP
        assert registry.record("fs_echo").source == "mcp"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mcp_tool_adapter_preserves_structured_content_and_remote_errors(tmp_path):
    server_path = tmp_path / "fake_mcp_server.py"
    server_path.write_text(SERVER_SCRIPT, encoding="utf-8")

    client = MCPClient(MCPServerConfig(name="fake", command=(sys.executable, str(server_path))))
    await client.connect()
    try:
        spec = (await client.list_tools())[0]
        adapter = MCPToolAdapter(client, spec, display_name="fs_echo")

        structured = await adapter.execute(
            "call-structured",
            {"text": "structured"},
            ToolExecutionContext(session_id="mcp", working_directory=tmp_path),
        )
        failed = await adapter.execute(
            "call-error",
            {"text": "error"},
            ToolExecutionContext(session_id="mcp", working_directory=tmp_path),
        )

        assert structured.metadata["structured_content"] == {"ok": True, "value": 7}
        assert failed.is_error is True
        assert failed.output == "remote failed"
    finally:
        await client.close()


class _CountingClient:
    def __init__(self, server: MCPServerConfig) -> None:
        self.server = server
        self.list_calls = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_tools(self) -> list[MCPToolSpec]:
        self.list_calls += 1
        return [
            MCPToolSpec(
                name="echo",
                description="Echo.",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    async def call_tool_result(self, name: str, arguments: dict) -> MCPCallResult:
        return MCPCallResult(text=f"{name}:{arguments}")


@pytest.mark.asyncio
async def test_mcp_registration_uses_refresh_snapshot_without_relisting(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    server = MCPServerConfig(name="fake", command=("fake",), prefix="fs_")
    client = _CountingClient(server)
    runtime = MCPServerRuntime(server=server, client=client)
    registry = ToolRegistry()

    await runtime.refresh()
    register_discovered_mcp_tools(runtime, registry, config)

    assert client.list_calls == 1
    assert runtime.discovered_tools == ("fs_echo",)
    assert runtime.registered_tools == ("fs_echo",)
    assert registry.record("fs_echo").source == "mcp"


@pytest.mark.asyncio
async def test_mcp_registration_ignores_global_tool_allowlist(tmp_path):
    config = SimpleNamespace(allowed_tools=["get_time"], denied_tools=[])
    server = MCPServerConfig(name="fake", command=("fake",), prefix="fs_")
    client = _CountingClient(server)
    runtime = MCPServerRuntime(server=server, client=client)
    registry = ToolRegistry()

    await runtime.refresh()
    register_discovered_mcp_tools(runtime, registry, config)

    assert runtime.registered_tools == ("fs_echo",)
    assert registry.record("fs_echo").source == "mcp"


def test_tools_package_exports_mcp_surface():
    assert ExportedMCPServerConfig is MCPServerConfig


def test_builtin_subagent_allowlists_ingest_registered_mcp_tools():
    config = SimpleNamespace(agent_mode="advanced", allowed_tools=[], denied_tools=[])
    server = MCPServerConfig(name="fake", command=("fake",), prefix="fs_")
    client = _CountingClient(server)
    runtime = MCPServerRuntime(
        server=server,
        client=client,
        connected=True,
        discovered_specs=(
            MCPToolSpec(
                name="echo",
                description="Echo.",
                input_schema={"type": "object", "properties": {}},
            ),
        ),
        discovered_tools=("fs_echo",),
    )
    registry = ToolRegistry()
    register_discovered_mcp_tools(runtime, registry, config)

    count = register_subagent_tools(
        registry,
        config,
        definitions=[
            SubagentDefinition(
                name="custom",
                description="Custom restricted agent.",
                goal_prompt="Stay restricted.",
                allowed_tools=["get_time"],
            )
        ],
    )

    assert count == 5
    assert "fs_echo" in registry.record("subagent_execution").tool._definition.allowed_tools
    assert "fs_echo" in registry.record("subagent_planning_analysis").tool._definition.allowed_tools
    assert registry.record("subagent_custom").tool._definition.allowed_tools == ["get_time"]


@pytest.mark.asyncio
async def test_mcp_dead_server_returns_clear_error(tmp_path):
    server = MCPServerConfig(
        name="dead",
        command=(sys.executable, "-c", "import time; time.sleep(2)"),
        startup_timeout_seconds=0.05,
    )
    client = MCPClient(server)
    try:
        with pytest.raises(RuntimeError, match="timed out during initialize"):
            await client.connect()
    finally:
        await client.close()
