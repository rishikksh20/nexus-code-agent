from __future__ import annotations

import pytest

from nexus.tools.builtin import GetTimeTool, WriteNoteTool


@pytest.mark.asyncio
async def test_get_time_tool_returns_utc_timestamp(tool_context):
    result = await GetTimeTool().execute("call-1", {}, tool_context)

    assert result.tool_name == "get_time"
    assert "T" in result.output
    assert result.metadata["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_write_note_tool_writes_in_workspace(tool_context):
    result = await WriteNoteTool().execute(
        "call-2",
        {"path": "notes/todo.txt", "content": "ship it"},
        tool_context,
    )

    assert result.is_error is False
    assert (tool_context.working_directory / "notes/todo.txt").read_text(encoding="utf-8") == "ship it"


@pytest.mark.asyncio
async def test_write_note_tool_rejects_outside_workspace(tool_context):
    result = await WriteNoteTool().execute(
        "call-3",
        {"path": "../escape.txt", "content": "nope"},
        tool_context,
    )

    assert result.is_error is True
    assert "outside the current workspace" in result.output.lower()


@pytest.mark.asyncio
async def test_write_note_tool_rejects_large_content(tool_context):
    result = await WriteNoteTool(max_bytes=8).execute(
        "call-4",
        {"path": "notes/large.txt", "content": "this is too large"},
        tool_context,
    )

    assert result.is_error is True
    assert "larger than 8 bytes" in result.output.lower()
