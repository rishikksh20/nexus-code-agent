from __future__ import annotations

import asyncio

import pytest

from nexus.models import ToolExecutionContext
from nexus.runtime.sandbox import DockerSandbox, SandboxConfig, SandboxedCommandTool


class _FakeProcess:
    def __init__(self, *, stdout: bytes = b"ok\n", returncode: int = 0) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self.killed = False

    async def communicate(self):
        return self._stdout, b""

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
async def test_sandboxed_command_tool_uses_docker_boundary(tmp_path, monkeypatch):
    captured: list[str] = []

    async def _fake_create_subprocess_exec(*args, **kwargs):
        del kwargs
        captured.extend(str(arg) for arg in args)
        return _FakeProcess(stdout=b"sandboxed output\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    sandbox = DockerSandbox(SandboxConfig(image="nexus-sandbox:latest", timeout_seconds=5.0))
    tool = SandboxedCommandTool(sandbox)

    result = await tool.execute(
        "call-1",
        {"command": "pwd"},
        ToolExecutionContext(session_id="sandbox", working_directory=tmp_path),
    )

    assert result.output == "sandboxed output"
    assert "docker" in captured[0]
    assert "--network=none" in captured
    assert "--cap-drop" in captured


@pytest.mark.asyncio
async def test_sandboxed_command_tool_reports_missing_command(tmp_path):
    tool = SandboxedCommandTool(DockerSandbox())

    result = await tool.execute(
        "call-1",
        {},
        ToolExecutionContext(session_id="sandbox", working_directory=tmp_path),
    )

    assert result.is_error is True
    assert "Missing required argument" in result.output