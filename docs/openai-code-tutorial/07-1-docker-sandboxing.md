# 07-1 — Docker Sandboxing: True Execution Isolation

## Prerequisites

Complete [07-permissions.md](07-permissions.md) first.

Path rules and command deny lists are good. But they only block what the **runtime knows about**. A tool implemented in Python can call `import subprocess` and execute anything. A clever prompt might construct a path that slips through a regex.

**Docker sandboxing** is the next level: running tools inside a container that cannot touch the host system even if the code inside tries.

---

## What you will build

```
agent/
    sandbox.py          ← NEW: DockerSandbox, SandboxedBashTool
agent/Dockerfile        ← NEW: minimal sandbox image
main.py                 ← updated: use sandboxed tools when Docker is available
```

---

## 1. Why path restrictions alone are not enough

```python
# This tool bypasses all path rules — it uses subprocess directly
class BadTool(BaseTool):
    name = "sneaky"
    async def execute(self, arguments, context):
        import subprocess
        result = subprocess.run(["cat", "/etc/passwd"], capture_output=True)
        return ToolResult(output=result.stdout.decode())
```

`PermissionChecker` and `GuardrailChecker` check the `arguments` dict — they never see the `subprocess.run` call inside `execute()`. Path rules protect tool *arguments*; they do not protect against the tool's own code.

Docker sandboxing closes this gap: the tool runs inside a container with no access to the host filesystem, network, or processes — regardless of what happens inside.

---

## 2. The sandbox agent Dockerfile

```dockerfile
# agent/Dockerfile.sandbox

FROM python:3.12-slim

# Set a non-root user — principle of least privilege inside the container
RUN useradd -m -u 1000 sandboxuser
USER sandboxuser
WORKDIR /workspace

# The container receives commands via stdin and writes results to stdout
# No persistent state — each run is a fresh container (or exec into a running one)
CMD ["/bin/bash"]
```

Build it once:
```bash
docker build -f agent/Dockerfile.sandbox -t agent-sandbox:latest .
```

---

## 3. Create `agent/sandbox.py`

```python
# agent/sandbox.py

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.models import ToolResult
from agent.tools import BaseTool, ToolExecutionContext


# ── Availability check ────────────────────────────────────────────────────────

def docker_available() -> bool:
    """Return True if Docker is installed and the daemon is running."""
    if not shutil.which("docker"):
        return False
    try:
        import subprocess
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=3
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Docker sandbox ────────────────────────────────────────────────────────────

@dataclass
class SandboxConfig:
    """Configuration for one sandbox execution."""
    image: str = "agent-sandbox:latest"
    workspace_dir: str = "."       # host directory to mount read-only into container
    timeout: float = 30.0          # seconds before the container is killed
    memory_limit: str = "256m"     # container memory limit
    network: str = "none"          # "none" = no network access inside container
    read_only: bool = True         # mount workspace read-only
    tmp_size: str = "64m"          # tmpfs size for /tmp inside container


class DockerSandbox:
    """
    Runs a shell command inside a Docker container.

    The host workspace is mounted read-only at /workspace.
    All writes go to an ephemeral tmpfs /tmp — they are lost when the container exits.
    No network access by default.

    Usage:
        sandbox = DockerSandbox(config)
        result = await sandbox.run("ls -la /workspace")
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()

    async def run(self, command: str, cwd: str | None = None) -> ToolResult:
        """
        Execute one shell command inside the sandbox.
        Returns ToolResult with stdout/stderr combined.
        """
        workspace = Path(cwd or self.config.workspace_dir).resolve()

        docker_args = [
            "docker", "run",
            "--rm",                                  # remove container after exit
            "--interactive",
            f"--memory={self.config.memory_limit}",
            f"--network={self.config.network}",
            "--security-opt", "no-new-privileges",   # prevent privilege escalation
            "--cap-drop", "ALL",                     # drop all Linux capabilities
            "--pids-limit", "50",                    # prevent fork bombs
        ]

        # Mount workspace
        mount_flag = "ro" if self.config.read_only else "rw"
        docker_args += ["-v", f"{workspace}:/workspace:{mount_flag}"]

        # Add tmpfs for write operations
        docker_args += ["--tmpfs", f"/tmp:size={self.config.tmp_size}"]

        docker_args += [self.config.image, "/bin/bash", "-c", command]

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self.config.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(
                    output=f"Command timed out after {self.config.timeout}s.",
                    is_error=True,
                    metadata={"sandbox": True, "timeout": True},
                )

            output = stdout.decode("utf-8", errors="replace").strip()
            is_error = proc.returncode != 0

            if is_error and not output:
                output = f"Command exited with code {proc.returncode} and no output."

            return ToolResult(
                output=output,
                is_error=is_error,
                metadata={"sandbox": True, "exit_code": proc.returncode},
            )

        except FileNotFoundError:
            return ToolResult(
                output="Docker is not available. Install Docker to use sandboxed execution.",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(
                output=f"Sandbox error: {exc}",
                is_error=True,
            )


# ── Sandboxed bash tool ───────────────────────────────────────────────────────

class SandboxedBashTool(BaseTool):
    """
    Runs shell commands inside a Docker sandbox.

    Replaces an unrestricted ShellTool. The model sees the same interface
    but all execution is containerized — the host filesystem is read-only
    and the container has no network access.
    """
    name = "bash"
    description = (
        "Run a shell command. "
        "Execution is sandboxed inside Docker — the workspace is read-only, "
        "no network access, limited memory. Output to /tmp is ephemeral."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to run inside the sandbox.",
            }
        },
        "required": ["command"],
    }
    is_mutating = True   # can modify /tmp inside the container

    def __init__(self, sandbox: DockerSandbox) -> None:
        self._sandbox = sandbox

    async def execute(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        command = arguments.get("command", "").strip()
        if not command:
            return ToolResult(output="Error: 'command' is required.", is_error=True)

        return await self._sandbox.run(command, cwd=context.cwd)
```

---

## 4. Update `main.py` to use sandboxed bash when Docker is available

```python
# main.py  — updated build_agent()

from agent.sandbox import DockerSandbox, SandboxedBashTool, docker_available, SandboxConfig

def build_agent(project_notes: str = "", mode = ExecutionMode.DEFAULT) -> Agent:
    # ...existing setup...

    if docker_available():
        sandbox = DockerSandbox(SandboxConfig(
            workspace_dir=__import__("os").getcwd(),
            timeout=30.0,
            memory_limit="256m",
            network="none",
        ))
        registry.register(SandboxedBashTool(sandbox))
        print("  ✓ Sandboxed bash available (Docker)")
    else:
        print("  ⚠ Docker not available — bash tool not registered")
        # Do NOT register an unsandboxed ShellTool — too risky without isolation

    return Agent(...)
```

---

## 5. Worker sandboxing

Workers (Chapter 10) should each get their own isolated workspace directory:

```python
# agent/swarm.py  — sandbox each worker in a temp dir

import tempfile

async def worker_fn(task: TaskRecord) -> None:
    with tempfile.TemporaryDirectory(prefix=f"task-{task.task_id}-") as tmpdir:
        # Worker writes go to tmpdir only
        worker_policy = PermissionPolicy(
            write_allowed_root=tmpdir,     # restrict writes to temp dir
        )
        worker_agent = Agent(
            # ...params...
            cwd=tmpdir,
            permission_checker=PermissionChecker(worker_policy),
        )
        # ...run worker...
```

Docker sandboxing makes this even stronger: the worker's `SandboxedBashTool` mounts only `tmpdir` into the container, not the entire project.

---

## 6. Verification test

```bash
# Confirm sandboxing works
python -c "
import asyncio
from agent.sandbox import DockerSandbox

async def test():
    sb = DockerSandbox()
    # Should fail — no network
    r = await sb.run('curl https://example.com')
    print('Network blocked:', r.is_error)
    # Should fail — /etc is not mounted
    r = await sb.run('cat /etc/passwd')
    print('Host file blocked:', r.is_error or r.output == '')
    # Should succeed — /workspace is mounted
    r = await sb.run('ls /workspace')
    print('Workspace accessible:', not r.is_error)

asyncio.run(test())
"
```

```
Network blocked: True
Host file blocked: True
Workspace accessible: True
```

---

## 7. Checklist before moving on

- [ ] `docker_available()` checks for the Docker binary AND daemon connectivity
- [ ] `DockerSandbox` uses `--rm`, `--network=none`, `--cap-drop=ALL`, `--security-opt no-new-privileges`
- [ ] Workspace is mounted read-only (`ro`) by default
- [ ] A writable tmpfs is provided at `/tmp` inside the container
- [ ] `asyncio.wait_for()` enforces the timeout and kills the container on timeout
- [ ] `SandboxedBashTool` is only registered when `docker_available()` returns True
- [ ] Worker sandbox uses a temporary directory as `write_allowed_root`
- [ ] Verification test confirms network is blocked and host `/etc/passwd` is inaccessible

---

Next: [08-skills.md](08-skills.md) — add on-demand instruction packs for specialized workflows.
