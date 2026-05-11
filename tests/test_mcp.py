from __future__ import annotations

import sys

import pytest

from nexus.integrations.mcp import MCPClient, MCPServerConfig, MCPToolAdapter
from nexus.models import ToolExecutionContext
from nexus.tools.base import ToolRegistry


SERVER_SCRIPT = """
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
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": f"echo:{text}"}]},
        }
    else:
        response = {"jsonrpc": "2.0", "id": request_id, "error": {"message": "unknown method"}}
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
        assert registry.record("fs_echo").source == "mcp"
    finally:
        await client.close()