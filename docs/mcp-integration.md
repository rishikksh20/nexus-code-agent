# MCP Integration

Nexus can expose tools from configured MCP servers through the normal tool registry. MCP tools use the same runtime path as built-in tools: model tool calls are checked by the permission system, approvals stay centralized in `run_agent_turn()`, and approved calls resume through the existing deterministic tool-call flow.

The live MCP implementation lives in `nexus/tools/mcp.py` and is exported from the `nexus.tools` package.

---

## Quick Start: Git + Filesystem MCP Servers

This is the most common MCP setup — a filesystem server for file access and a git server for repository operations. Both use the `stdio` transport and are configured in `.nexus/config.toml`.

### Step 1 — Install the servers

**Filesystem MCP server** (Node.js npm package):

```bash
npm install -g @modelcontextprotocol/server-filesystem
```

Verify:

```bash
mcp-server-filesystem --help
```

**Git MCP server** (Python, via `uvx` — no permanent install needed):

```bash
uvx mcp-server-git --help
```

Or install permanently:

```bash
pip install mcp-server-git
```

> **Note:** `mcp-server-filesystem` is an npm package — do **not** use `uvx` to invoke it.
> `mcp-server-git` is a Python package — use `uvx` or `pip install`.

### Step 2 — Configure MCP Servers

Define reusable MCP servers globally in `~/.nexus/config.toml`, or define
workspace-only servers in `.nexus/config.toml`.

MCP definitions intentionally stay in protected config instead of a readable
`.agents/` directory: an MCP entry may contain environment variables,
credentials, or remote URLs. Nexus does not currently ship packaged MCP server
definitions to copy into a workspace.

```toml
mcp_servers = [
  {
    name      = "filesystem",
    transport = "stdio",
    command   = ["mcp-server-filesystem", "/path/to/your/workspace"],
    prefix    = "fs_",
    startup_timeout_seconds = 10,
    tool_timeout_seconds    = 60
  },
  {
    name      = "git",
    transport = "stdio",
    command   = ["uvx", "mcp-server-git", "--repository", "/path/to/your/git-repo-root"],
    prefix    = "git_",
    startup_timeout_seconds = 15,
    tool_timeout_seconds    = 60
  }
]
```

Replace `/path/to/your/workspace` and `/path/to/your/git-repo-root` with absolute paths.

> **Important:** The git server requires the **git repository root** (the directory that contains `.git/`), not a subdirectory inside the repo.
> Run `git rev-parse --show-toplevel` to find the correct path.

### Step 3 — Activate Servers for a Workspace

Local and global `mcp_servers` entries form a catalog. Activate servers for a
workspace by name:

```toml
enabled_mcp_servers = ["filesystem", "git"]
disabled_mcp_servers = []
```

You can also manage this from the REPL:

```
/mcp available
/mcp activate filesystem
/mcp deactivate filesystem
```

Do not add MCP tool names to `allowed_tools`. When an MCP server is active,
Nexus discovers its tools during initialization and registers all discovered
tools except any remote names listed in that server's `disabled_tools`.

> **Prefix doubling explained:** `mcp-server-git` names its tools internally with a `git_` prefix (e.g. `git_status`). Adding `prefix = "git_"` in config makes the Nexus name `git_git_status`. To avoid doubling, use `prefix = "mcp_git_"` instead (giving names like `mcp_git_git_status`), or omit the prefix entirely — but then the MCP tool `git_status` collides with the Nexus built-in `git_status`.

### Step 4 — Start the REPL and verify

```bash
cd /your/workspace
uv run nexus
```

Inside the REPL, run:

```
/mcp status
```

Expected output (both servers connected):

```
┏━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ Name       ┃ Transport ┃ Status    ┃ Prefix ┃ Registered ┃ Discovered ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━┛╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ filesystem │ stdio     │ connected │ fs_    │ 9          │ 9          │
│ git        │ stdio     │ connected │ git_   │ 12         │ 12         │
└────────────┴───────────┴───────────┴────────┴────────────┴────────────┘
```

List registered tools:

```
/mcp tools
```

Then ask the agent naturally:

```
> Show me the last 5 commits
> What files have been changed but not staged?
> List all Python files in the workspace
> Commit the staged changes with message "fix: update config"
```

---

## Configuration Reference

Add MCP servers to `mcp_servers` in `.nexus/config.toml` (workspace-level) or `~/.nexus/config.toml` (global catalog):

```toml
mcp_servers = [
  { name = "filesystem", transport = "stdio", command = ["mcp-server-filesystem", "."], prefix = "mcp_fs_" }
]
```

`transport` defaults to `stdio`, so older entries that only specify `name`, `command`, and `prefix` still work.

### All Supported Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Unique label shown in `/mcp status` and tool metadata |
| `transport` | string | `"stdio"` | `stdio`, `http`, or `streamable_http` |
| `command` | list | required for stdio | Process command as a list of strings |
| `url` | string | — | Remote MCP URL, required for HTTP transports |
| `prefix` | string | `""` | Prefix prepended to every published Nexus tool name |
| `env` | dict | — | Extra env vars merged into the subprocess environment |
| `cwd` | string | — | Working directory for the subprocess |
| `startup_timeout_seconds` | float | `10.0` | Timeout for `initialize` + `tools/list` handshake |
| `tool_timeout_seconds` | float | `60.0` | Timeout for `tools/call` execution |
| `disabled` | bool | `false` | Skip discovery and registration for this server |
| `disabled_tools` | list | `[]` | Remote MCP tool names to hide from Nexus |

`allowed_tools` and `denied_tools` apply to built-in, plugin, sandbox, and
sub-agent tools. MCP tools are controlled at the server level:

- `enabled_mcp_servers` activates global catalog servers for the workspace.
- `disabled_mcp_servers` disables local or global servers by name.
- `disabled_tools` on one server hides specific remote MCP tool names.

### Full Multi-Server Example

```toml
mcp_servers = [
  # Filesystem — npm package; prefix avoids collision with built-in read_file/write_file
  {
    name      = "filesystem",
    transport = "stdio",
    command   = ["mcp-server-filesystem", "/home/user/projects/myapp"],
    prefix    = "fs_",
    startup_timeout_seconds = 10,
    tool_timeout_seconds    = 60
  },

  # Git — Python package via uvx; must point at the git repo root (.git parent)
  {
    name      = "git",
    transport = "stdio",
    command   = ["uvx", "mcp-server-git", "--repository", "/home/user/projects/myapp"],
    prefix    = "git_",
    startup_timeout_seconds = 15,
    tool_timeout_seconds    = 60
  },

  # Specific git tools suppressed — e.g. disable commit via MCP, keep read-only tools
  {
    name           = "git-readonly",
    transport      = "stdio",
    command        = ["uvx", "mcp-server-git", "--repository", "/home/user/projects/myapp"],
    prefix         = "gro_",
    disabled_tools = ["git_commit", "git_add", "git_reset", "git_create_branch", "git_checkout"]
  },

  # Remote HTTP server — disabled for now
  {
    name      = "remote-api",
    transport = "streamable_http",
    url       = "http://localhost:3333/mcp",
    disabled  = true
  }
]
```

---

## Slash Commands

All `/mcp` commands are available inside the interactive REPL. Run `/mcp help` to see the full list.

### `/mcp status`

Inspect every configured server. Shows transport, connection state, prefix, registered/discovered tool counts, last check time, and last error.

```
/mcp status
```

Use this first after startup to confirm servers connected. If `Status` is `disconnected`, check the `Last Error` column for the exact failure reason.

### `/mcp tools`

List all discovered tools across all connected servers. Shows server name, Nexus published name, original remote name, and description.

```
/mcp tools
```

Use this to confirm the actual published names and to see which tools are enabled or disabled.

### `/mcp available`

List MCP servers defined globally or locally, plus whether each one is active,
available, disabled, or referenced but missing.

```
/mcp available
```

### `/mcp activate <server>` and `/mcp deactivate <server>`

Enable or disable an MCP server for the current workspace by name. These update
the workspace config and reload MCP servers immediately.

```
/mcp activate filesystem
/mcp deactivate filesystem
```

### `/mcp refresh [server]`

Rediscover tools from a running server and hot-register into the active registry — **without** restarting the server process.

```
/mcp refresh              # refresh all servers
/mcp refresh git          # refresh only the "git" server
/mcp refresh filesystem   # refresh only the "filesystem" server
```

The refresh report lists added, removed, unchanged, and failed tools. Use this when the MCP server's tool list may have changed while Nexus is running.

### `/mcp reload`

Full reload: re-reads `.nexus/config.toml`, closes all MCP server processes, removes old MCP tools from the active registry, restarts every configured server, and registers their tools.

```
/mcp reload
```

Use this after **editing `mcp_servers` in config** while the REPL is already running. This is the recommended workflow:

1. Edit `.nexus/config.toml`
2. Run `/mcp reload` in the REPL
3. Confirm with `/mcp status`

---

## Tool Name Reference

### Filesystem MCP (`mcp-server-filesystem`) with `prefix = "fs_"`

| Remote tool name | Nexus name | Description |
|---|---|---|
| `read_file` | `fs_read_file` | Read file contents |
| `write_file` | `fs_write_file` | Create or overwrite a file |
| `list_directory` | `fs_list_directory` | List directory contents |
| `create_directory` | `fs_create_directory` | Create a directory |
| `delete_file` | `fs_delete_file` | Delete a file |
| `move_file` | `fs_move_file` | Move or rename a file |
| `get_file_info` | `fs_get_file_info` | File metadata (size, modified time, type) |
| `search_files` | `fs_search_files` | Search files by name pattern |
| `list_allowed_directories` | `fs_list_allowed_directories` | Show configured allowed root paths |

### Git MCP (`mcp-server-git`) with `prefix = "git_"`

The git MCP server ships tools with an internal `git_` prefix, so with `prefix = "git_"` the Nexus names are doubled:

| Remote tool name | Nexus name (prefix=`git_`) | Description |
|---|---|---|
| `git_status` | `git_git_status` | Working tree status |
| `git_diff_unstaged` | `git_git_diff_unstaged` | Unstaged changes diff |
| `git_diff_staged` | `git_git_diff_staged` | Staged changes diff |
| `git_diff` | `git_git_diff` | Diff between two branches or commits |
| `git_log` | `git_git_log` | Commit history |
| `git_show` | `git_git_show` | Show a specific commit's content |
| `git_add` | `git_git_add` | Stage files |
| `git_reset` | `git_git_reset` | Unstage files |
| `git_commit` | `git_git_commit` | Create a commit |
| `git_create_branch` | `git_git_create_branch` | Create a new branch |
| `git_checkout` | `git_git_checkout` | Switch branches |
| `git_read_file` | `git_git_read_file` | Read a file at a specific commit |

---

## Safety Behavior

MCP tools are marked `ToolKind.MCP` and mutating by default.

| Mode | Mutating MCP call behavior |
|---|---|
| `plan` | Denied — no mutations allowed in plan mode |
| `default` | Requires user confirmation before executing |
| `auto` | Executes immediately (use with care) |

When MCP tools are registered, the system prompt includes a compact MCP tool contract and lists the published MCP tool names by server. Built-in cognitive sub-agents in `advanced` mode also inherit registered MCP tool names in their default allowlists, so specialist agents can use MCP capabilities through the same approval path. Custom sub-agents with explicit `allowed_tools` keep their configured restrictions.

Tool results include MCP provenance in metadata:

- `source = "mcp"`
- `server` — the server `name` from config
- `remote_tool` — the original tool name before prefixing
- `transport` — `stdio`, `http`, or `streamable_http`
- `structured_content` — when the MCP server returns structured content alongside text

Remote MCP `isError` results become `ToolResult.is_error = true`.

---

## Refresh Semantics

Discovery stores a snapshot of `MCPToolSpec` objects on the server runtime. Registration consumes that snapshot, so startup and refresh do not call `tools/list` once per tool.

`/mcp refresh` removes previous MCP tools for the refreshed server, then registers the newly discovered enabled tools. Non-MCP tools and tools from other MCP servers are left untouched.

If discovery fails, Nexus keeps the failure visible in `/mcp status` and does not replace the active tools for that server during that failed refresh.

If you add MCP config to an existing project while Nexus is already running, use `/mcp reload`. If Nexus is not running yet, starting a new REPL or headless run loads `mcp_servers` during startup.

---

## Troubleshooting

### Server shows `disconnected` in `/mcp status`

1. Run `/mcp status` — the `Last Error` column shows the exact exception.
2. Test the binary manually in your terminal:
   ```bash
   mcp-server-filesystem /your/workspace --help
   uvx mcp-server-git --repository /your/repo-root --help
   ```
3. Verify path types:
   - `mcp-server-filesystem` → any readable directory
   - `mcp-server-git` → must be a **git repo root** (contains `.git/`)
   ```bash
   git rev-parse --show-toplevel   # prints the correct path
   ```
4. If using `uvx`, the first cold run downloads the package. Increase `startup_timeout_seconds = 30` for first-time runs.

### `mcp-server-filesystem` fails with package not found via `uvx`

`mcp-server-filesystem` is an **npm** package, not a Python package. Do not prefix with `uvx`:

```toml
# WRONG — uvx cannot resolve npm packages
command = ["uvx", "mcp-server-filesystem", "/workspace"]

# CORRECT — use the npm-installed global binary
command = ["mcp-server-filesystem", "/workspace"]
```

Install it once:

```bash
npm install -g @modelcontextprotocol/server-filesystem
```

### Tools appear as `unregistered` in `/mcp tools`

The MCP server is active and discovery saw the remote tool, but Nexus could not
publish that tool name. The usual cause is a name collision with an existing
tool. Set a unique server `prefix`, then run `/mcp reload`.

### `mcp-server-git` error: "not a valid Git repository"

The `--repository` path must contain a `.git/` directory:

```bash
git rev-parse --show-toplevel   # use this output as the --repository value
```

Update config with the correct path, then `/mcp reload`.

### A tool call hangs or times out

Increase `tool_timeout_seconds` for that server:

```toml
{ name = "git", ..., tool_timeout_seconds = 120 }
```

Reload with `/mcp reload`.

### Stdio server stderr is not visible

Nexus captures MCP server stderr to debug logs. Enable verbose logging to see it:

```bash
uv run nexus --log-level debug
```

Or inspect `~/.nexus/logs/runtime.jsonl` for lines tagged `[MCP stderr:<server-name>]`.
