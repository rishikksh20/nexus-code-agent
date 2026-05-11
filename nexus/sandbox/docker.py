"""Container execution engine for the Nexus sandbox.

:class:`DockerSandbox` runs shell commands inside a hardened Docker
container.  All security constraints (network isolation, read-only workspace
mount, memory limits, dropped capabilities) are applied here so that
:class:`~nexus.sandbox.tool.SandboxedCommandTool` stays focused on the tool
interface.

The module also exposes two lightweight probe functions,
:func:`docker_available` and :func:`docker_image_available`, used by the
registry factory to decide whether to register the sandboxed tool at startup.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from nexus.models import ToolResult


@dataclass(slots=True, frozen=True)
class SandboxConfig:
    """Immutable configuration for a single :class:`DockerSandbox` instance.

    All fields map directly to Docker ``run`` flags so that the relationship
    between config and the subprocess call is transparent.
    """

    image: str = "nexus-sandbox:latest"
    timeout_seconds: float = 30.0
    memory_limit: str = "256m"
    network: str = "none"
    read_only_workspace: bool = True
    tmp_size: str = "64m"


class DockerSandbox:
    """Run a shell command inside a hardened Docker container.

    Security properties guaranteed by the container flags:

    * No network access (``--network=none`` by default).
    * Workspace mounted read-only (``/workspace:ro`` by default).
    * ``/tmp`` is an ephemeral in-memory tmpfs.
    * All Linux capabilities dropped (``--cap-drop=ALL``).
    * ``no-new-privileges`` seccomp option applied.
    * Process count capped at 50 (``--pids-limit=50``).
    * Memory capped at *memory_limit* (default ``256m``).

    Parameters
    ----------
    config:
        :class:`SandboxConfig` controlling resource and security constraints.
        Defaults to safe, minimal settings if omitted.
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()

    async def run(self, command: str, *, cwd: Path) -> ToolResult:
        """Execute *command* inside the sandbox and return a :class:`ToolResult`.

        Parameters
        ----------
        command:
            Shell command string forwarded to ``/bin/sh -lc``.
        cwd:
            Host path mounted at ``/workspace`` inside the container.
        """
        workspace = cwd.resolve()
        if not workspace.exists():
            return ToolResult(
                call_id="sandbox",
                tool_name="run_command",
                output=f"Workspace does not exist: {workspace}",
                is_error=True,
                metadata={"source": "sandbox", "image": self.config.image},
            )

        mount_mode = "ro" if self.config.read_only_workspace else "rw"
        args = [
            "docker", "run", "--rm", "--interactive",
            f"--memory={self.config.memory_limit}",
            f"--network={self.config.network}",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--pids-limit", "50",
            "-w", "/workspace",
            "-v", f"{workspace}:/workspace:{mount_mode}",
            "--tmpfs", f"/tmp:size={self.config.tmp_size}",
            self.config.image,
            "/bin/sh", "-lc", command,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.config.timeout_seconds,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return ToolResult(
                    call_id="sandbox",
                    tool_name="run_command",
                    output=f"Command timed out after {self.config.timeout_seconds}s.",
                    is_error=True,
                    metadata={"source": "sandbox", "image": self.config.image, "timeout": True},
                )
        except FileNotFoundError:
            return ToolResult(
                call_id="sandbox",
                tool_name="run_command",
                output=(
                    "Docker is not available. "
                    "Install Docker and build the sandbox image first."
                ),
                is_error=True,
                metadata={"source": "sandbox", "image": self.config.image},
            )
        except Exception as exc:
            return ToolResult(
                call_id="sandbox",
                tool_name="run_command",
                output=f"Sandbox error: {exc}",
                is_error=True,
                metadata={"source": "sandbox", "image": self.config.image},
            )

        output = stdout.decode("utf-8", errors="replace").strip()
        if process.returncode != 0 and not output:
            output = f"Command exited with code {process.returncode} and no output."

        return ToolResult(
            call_id="sandbox",
            tool_name="run_command",
            output=output,
            is_error=process.returncode != 0,
            metadata={
                "source": "sandbox",
                "image": self.config.image,
                "exit_code": process.returncode,
            },
        )


# ---------------------------------------------------------------------------
# Runtime probes
# ---------------------------------------------------------------------------


def docker_available() -> bool:
    """Return ``True`` if Docker is installed and the daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def docker_image_available(image: str) -> bool:
    """Return ``True`` if *image* exists in the local Docker image store."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0
