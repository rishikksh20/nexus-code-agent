from __future__ import annotations

import pytest

from nexus.models import ToolExecutionContext
from nexus.tools.builtin.shell import ShellTool


@pytest.mark.asyncio
async def test_bash_tool_streams_live_output_to_ui_callback(tmp_path):
    chunks: list[tuple[str, str, str, str]] = []

    class StreamingUI:
        def stream_tool_output(self, call_id, tool_name, stream_name, chunk):
            chunks.append((call_id, tool_name, stream_name, chunk))

    context = ToolExecutionContext(
        session_id="s",
        working_directory=tmp_path,
        metadata={"ui": StreamingUI()},
    )

    result = await ShellTool().execute(
        "c-stream",
        {"command": "printf hello && printf err >&2"},
        context,
    )

    assert not result.is_error
    assert "hello" in result.output
    assert any(
        call_id == "c-stream" and stream_name == "stdout" and "hello" in chunk
        for call_id, _tool_name, stream_name, chunk in chunks
    )
    assert any(
        call_id == "c-stream" and stream_name == "stderr" and "err" in chunk
        for call_id, _tool_name, stream_name, chunk in chunks
    )


@pytest.mark.asyncio
async def test_bash_tool_scrubs_host_environment_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_TEST_SECRET_TOKEN", "super-secret")
    context = ToolExecutionContext(session_id="s", working_directory=tmp_path)

    result = await ShellTool().execute(
        "c-env",
        {"command": "python -c \"import os; print(os.getenv('NEXUS_TEST_SECRET_TOKEN', 'missing'))\""},
        context,
    )

    assert not result.is_error
    assert result.output.strip() == "missing"


@pytest.mark.asyncio
async def test_bash_tool_keeps_bounded_tail_output(tmp_path):
    class Config:
        tool_output_max_chars = 50
        shell_inherit_environment = False

    context = ToolExecutionContext(
        session_id="s",
        working_directory=tmp_path,
        metadata={"config": Config()},
    )

    result = await ShellTool().execute(
        "c-bound",
        {"command": "python -c \"import sys; sys.stdout.write('a' * 120 + 'TAIL')\""},
        context,
    )

    assert not result.is_error
    assert result.metadata["stdout_truncated"] is True
    assert "TAIL" in result.output
    assert len(result.output) < 140
