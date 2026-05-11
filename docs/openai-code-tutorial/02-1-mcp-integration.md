# 02-1 — MCP Integration: Connecting External Tool Servers

## Prerequisites

Complete [02-tools.md](02-tools.md) first.

Your `ToolRegistry` currently holds only tools you write yourself in Python. The **Model Context Protocol (MCP)** is the 2025–2026 industry standard that lets external programs expose tools to any compatible agent — without you writing adapter code for each one.

This chapter adds an `MCPClient` that connects to any MCP server and automatically registers its tools as `BaseTool` instances in your existing registry.

---

## What you will build

```
agent/
    mcp.py          ← NEW: MCPClient, MCPToolAdapter, discover_mcp_tools()
    tools.py        ← unchanged (MCPToolAdapter conforms to BaseTool)
main.py             ← updated: connect to MCP servers at startup
```

After this chapter your agent will be able to use:
- `mcp-server-filesystem` — read/write/list files via MCP
- `mcp-server-git` — git operations via MCP
- Any future MCP-compatible server without code changes

---

## 1. What MCP actually is

MCP (Model Context Protocol) is a JSON-RPC 2.0 protocol between an **MCP host** (your agent) and an **MCP server** (an external process):

```
Your agent (MCP host)          MCP server process
       │                              │
       │  tools/list  ─────────────────►  returns [{name, description, inputSchema}]
       │                              │
       │  tools/call  ─────────────────►  invokes tool, returns result
       │  {name, arguments}           │
       │              ◄───────────── result text
```

The wire format is the same JSON Schema you already use for `BaseTool.input_schema`. MCP just standardizes the transport (subprocess stdio or HTTP SSE) and discovery protocol.

---

## 2. Create `agent/mcp.py`

```python
# agent/mcp.py

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from agent.models import ToolResult
from agent.tools import BaseTool, ToolExecutionContext, ToolRegistry


# ── Wire types ────────────────────────────────────────────────────────────────

@dataclass
class MCPToolSchema:
    """One tool as returned by the MCP server's tools/list response."""
    name: str
    description: str
    input_schema: dict[str, Any]


# ── MCP client ────────────────────────────────────────────────────────────────

class MCPClient:
    """
    Connects to one MCP server via subprocess stdio transport.

    The server is started as a subprocess. Communication uses JSON-RPC 2.0
    over stdin/stdout.

    Usage:
        async with MCPClient(["uvx", "mcp-server-filesystem", "/tmp"]) as client:
            tools = await client.list_tools()
            result = await client.call_tool("read_file", {"path": "/tmp/test.txt"})
    """

    def __init__(self, command: list[str], timeout: float = 10.0) -> None:
        self.command = command
        self.timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def connect(self) -> None:
        """Start the MCP server subprocess."""
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Send MCP initialize handshake
        await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "agent-harness", "version": "1.0"},
        })
        await self._rpc("notifications/initialized", {})

    async def list_tools(self) -> list[MCPToolSchema]:
        """Query the server for all available tools."""
        response = await self._rpc("tools/list", {})
        tools_raw = response.get("tools", [])
        return [
            MCPToolSchema(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {"type": "object", "properties": {}}),
            )
            for t in tools_raw
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke one tool on the server and return its text output."""
        response = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = response.get("content", [])
        texts = [c["text"] for c in content if c.get("type") == "text"]
        return "\n".join(texts) or "(no output)"

    async def close(self) -> None:
        if self._proc:
            try:
                self._proc.stdin.close()
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:
                self._proc.kill()

    # ── JSON-RPC internals ────────────────────────────────────────────────────

    async def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params,
        }) + "\n"
        self._proc.stdin.write(request.encode())
        await self._proc.stdin.drain()

        try:
            line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"MCP server timed out on method '{method}'")

        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(f"MCP error: {response['error']}")
        return response.get("result", {})


# ── BaseTool adapter ──────────────────────────────────────────────────────────

class MCPToolAdapter(BaseTool):
    """
    Wraps one MCP server tool as a standard BaseTool.

    The agent loop treats it identically to any other tool — no special casing.
    The MCP client reference is kept so calls route back to the right server.
    """
    is_mutating = True   # assume mutating by default; MCP schema doesn't expose this

    def __init__(self, schema: MCPToolSchema, client: MCPClient) -> None:
        self.name = schema.name
        self.description = schema.description
        self.input_schema = schema.input_schema
        self._client = client

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        try:
            output = await self._client.call_tool(self.name, arguments)
            return ToolResult(output=output, metadata={"mcp_tool": self.name})
        except Exception as exc:
            return ToolResult(
                output=(
                    f"MCP tool '{self.name}' failed: {exc}\n"
                    "Check that the MCP server is running and the arguments are correct."
                ),
                is_error=True,
            )


# ── Discovery helper ──────────────────────────────────────────────────────────

async def discover_mcp_tools(
    command: list[str],
    registry: ToolRegistry,
    prefix: str = "",
) -> MCPClient:
    """
    Connect to an MCP server, list its tools, and register them in the registry.

    Returns the MCPClient — keep a reference to close it on shutdown.

    Args:
        command:  Server command, e.g. ["uvx", "mcp-server-filesystem", "/tmp"]
        registry: ToolRegistry to register tools into
        prefix:   Optional name prefix to avoid collisions, e.g. "fs_" → "fs_read_file"

    Example:
        client = await discover_mcp_tools(
            ["uvx", "mcp-server-filesystem", "."],
            registry,
            prefix="fs_",
        )
    """
    client = MCPClient(command)
    await client.connect()

    schemas = await client.list_tools()
    for schema in schemas:
        if prefix:
            schema = MCPToolSchema(
                name=f"{prefix}{schema.name}",
                description=schema.description,
                input_schema=schema.input_schema,
            )
        adapter = MCPToolAdapter(schema, client)
        registry.register(adapter)

    return client
```

---

## 3. Update `main.py` to connect MCP servers

```python
# main.py  — updated build_agent() with MCP support

import asyncio
from agent.mcp import discover_mcp_tools

_mcp_clients: list = []   # keep references for cleanup

async def build_agent_async(project_notes: str = "", mode=ExecutionMode.DEFAULT) -> Agent:
    """Async version of build_agent() — needed because MCP connections are async."""
    registry = default_registry(memory_store=memory_store)

    # ── Connect MCP servers ────────────────────────────────────────────────────
    # Only connect if the server command is available
    import shutil

    if shutil.which("uvx"):
        try:
            # Filesystem MCP server — gives the agent read/write/list tools
            fs_client = await discover_mcp_tools(
                command=["uvx", "mcp-server-filesystem", "."],
                registry=registry,
                prefix="fs_",          # tools become: fs_read_file, fs_write_file, etc.
            )
            _mcp_clients.append(fs_client)
            print(f"  ✓ MCP filesystem server connected ({len(fs_client._proc.pid)} tools)")
        except Exception as e:
            print(f"  ⚠ MCP filesystem server unavailable: {e}")

    return Agent(
        model_client=client,
        tool_registry=registry,
        # ...rest of params unchanged...
    )

async def main() -> None:
    agent = await build_agent_async()
    try:
        await repl(agent, store)
    finally:
        # Clean up MCP server processes on exit
        for client in _mcp_clients:
            await client.close()
```

---

## 4. Try it

```bash
# Install the filesystem MCP server
pip install uvx
uvx mcp-server-filesystem --help

# Run the agent — MCP tools appear alongside your regular tools
python main.py
```

```
  ✓ MCP filesystem server connected
Agent ready. Available tools: get_time, echo, read_file, ..., fs_read_file, fs_write_file, fs_list_directory
```

The model can now use `fs_read_file`, `fs_write_file`, and `fs_list_directory` — all from the external server, all going through your guardrails and permission checks as normal.

---

## 5. How MCP tools interact with your existing safety layers

MCP tools arrive through `MCPToolAdapter`, which is a standard `BaseTool`. This means they automatically go through:

1. `HookExecutor` — `pre_tool_use` fires before any MCP tool call
2. `GuardrailChecker` — path rules still block `~/.ssh` even from MCP tools
3. `PermissionChecker` — `is_mutating = True` means confirmation is required
4. `AuditTrail` — every MCP tool call is logged

No special-casing needed. The adapter pattern is what makes this work.

---

## 6. Connecting multiple MCP servers

```python
# Different servers for different capabilities
servers = [
    (["uvx", "mcp-server-filesystem", "."], "fs_"),
    (["uvx", "mcp-server-git", "."],        "git_"),
    (["uvx", "mcp-server-fetch"],           "web_"),
]

for command, prefix in servers:
    try:
        client = await discover_mcp_tools(command, registry, prefix=prefix)
        _mcp_clients.append(client)
    except Exception as e:
        print(f"Warning: could not connect {command[1]}: {e}")
```

---

## 7. Checklist before moving on

- [ ] `MCPClient` can start a subprocess and perform the initialize handshake
- [ ] `MCPClient.list_tools()` returns a list of `MCPToolSchema` objects
- [ ] `MCPClient.call_tool()` sends `tools/call` and returns the text result
- [ ] `MCPToolAdapter` wraps one schema + client as a standard `BaseTool`
- [ ] `discover_mcp_tools()` connects, lists tools, and registers adapters automatically
- [ ] MCP tools appear in the registry and are advertised in the prompt like any other tool
- [ ] MCP tools pass through guardrails, permissions, and hooks — no bypassing
- [ ] MCP clients are closed cleanly on agent shutdown

---

Next: [02-2-plugins.md](02-2-plugins.md) — extend the registry with third-party plugin packages, then continue to [03-session-manager.md](03-session-manager.md).

