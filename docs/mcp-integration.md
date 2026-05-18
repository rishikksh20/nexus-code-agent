# MCP Integration

Nexus can expose tools from configured MCP servers through the normal tool registry. MCP tools use the same runtime path as built-in tools: model tool calls are checked by the permission system, approvals stay centralized in `run_agent_turn()`, and approved calls resume through the existing deterministic tool-call flow.

The live MCP implementation lives in `nexus/tools/mcp.py` and is exported from the `nexus.tools` package.

## Configuration

Add MCP servers to `mcp_servers` in config:

```toml
mcp_servers = [
  { name = "filesystem", transport = "stdio", command = ["mcp-server-filesystem", "."], prefix = "mcp_fs_" }
]
```

`transport` defaults to `stdio`, so older entries that only specify `name`, `command`, and `prefix` still work.

Use a distinct prefix such as `mcp_fs_` for filesystem MCP tools. That keeps published MCP tool names like `mcp_fs_read_file` from colliding with Nexus built-ins like `read_file`.

Install the official filesystem server with:

```bash
npm install -g @modelcontextprotocol/server-filesystem
```

Supported fields:

| Field | Description |
|---|---|
| `name` | Unique server name shown in `/mcp` output and tool metadata |
| `transport` | `stdio` today; `http` and `streamable_http` config validates for future support |
| `command` | Stdio command as a list, required for `stdio` |
| `url` | Remote MCP URL, required for HTTP-style transports |
| `prefix` | Prefix added to published Nexus tool names |
| `env` | Extra environment variables merged into the server process environment |
| `cwd` | Working directory for the server process |
| `startup_timeout_seconds` | Timeout for initialize and discovery calls |
| `tool_timeout_seconds` | Timeout for `tools/call` execution |
| `disabled` | Skip discovery and registration for the server |
| `disabled_tools` | Remote MCP tool names to hide from Nexus |

Global `allowed_tools` and `denied_tools` still apply to the final Nexus tool name after prefixing.

## Slash Commands

Use `/mcp status` to inspect configured servers. It shows transport, connection state, registered and discovered counts, last check time, and last error.

Use `/mcp tools` to inspect discovered tools. It shows the server, published Nexus name, remote MCP name, enabled/disabled/filtered state, and a short description.

Use `/mcp refresh` to rediscover every configured server and hot-register the current tool set into the active `ToolRegistry`. Use `/mcp refresh <server>` to refresh one server. The refresh report lists added, removed, unchanged, and failed tools.

Use `/mcp reload` after editing config in an already-running REPL. It reloads config, closes currently loaded MCP servers, removes old MCP tools from the active registry, starts the configured MCP servers, and registers their current tools.

## Safety Behavior

MCP tools are marked `ToolKind.MCP` and mutating by default. In plan mode, mutating MCP calls are denied. In default mode, they require confirmation. In auto mode or auto approval policy, they follow the same rules as other mutating tools.

When MCP tools are registered, the system prompt includes a compact MCP tool contract and lists the published MCP tool names by server. Built-in cognitive sub-agents also inherit registered MCP tool names in their default allowlists, so specialist agents can use configured MCP capabilities through the same approval path. Custom sub-agents with explicit `allowed_tools` keep their configured restrictions.

Tool results include MCP provenance in metadata:

- `source = "mcp"`
- `server`
- `remote_tool`
- `transport`
- `structured_content` when the MCP server returns structured content

Remote MCP `isError` results become `ToolResult.is_error = true`.

## Refresh Semantics

Discovery stores a snapshot of `MCPToolSpec` objects on the server runtime. Registration consumes that snapshot, so startup and refresh do not call `tools/list` once per tool.

Refresh removes previous MCP tools for the refreshed server, then registers the newly discovered enabled tools. Non-MCP tools and tools from other MCP servers are left untouched.

If discovery fails, Nexus keeps the failure visible in `/mcp status` and does not replace the active tools for that server during that failed refresh.

If you add MCP config to an existing project while Nexus is already running, use `/mcp reload`. If Nexus is not running yet, starting a new REPL or headless run loads `mcp_servers` during startup.

## Troubleshooting

- If a server does not appear, check `/mcp status` for `last_error`.
- If a tool appears as `filtered`, check `allowed_tools`, `denied_tools`, and `disabled_tools`.
- If a server hangs during startup, lower or inspect `startup_timeout_seconds`.
- If a tool call hangs, set `tool_timeout_seconds`.
- Stdio server stderr is captured to Nexus debug logs as MCP stderr lines.
