from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nexus.models import ToolExecutionContext
from nexus.tools.builtin import (
    CodeIndexTool,
    GitDiffTool,
    GitStatusTool,
    RunLinterTool,
    RunTestsTool,
    RunTypecheckTool,
    SemanticSearchTool,
)


@pytest.mark.asyncio
async def test_git_status_and_diff_return_structured_data(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (tmp_path / "sample.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=tmp_path, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    context = ToolExecutionContext(session_id="test", working_directory=tmp_path)

    status = await GitStatusTool().execute("status-1", {}, context)
    diff = await GitDiffTool().execute("diff-1", {"target": "staged"}, context)

    assert not status.is_error
    status_payload = json.loads(status.output)
    assert "sample.txt" in status_payload["staged"]
    assert not diff.is_error
    assert "+hello" in diff.output


@pytest.mark.asyncio
async def test_run_typecheck_returns_structured_metadata(tmp_path):
    package = tmp_path / "nexus"
    tests_dir = tmp_path / "tests"
    package.mkdir()
    tests_dir.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "test_placeholder.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    context = ToolExecutionContext(session_id="test", working_directory=tmp_path)

    result = await RunTypecheckTool().execute("type-1", {}, context)

    assert not result.is_error
    assert result.metadata["passed"] is True
    assert result.metadata["exit_code"] == 0


@pytest.mark.asyncio
async def test_run_linter_fails_when_workspace_has_no_python_targets(tmp_path):
    context = ToolExecutionContext(session_id="test", working_directory=tmp_path)

    result = await RunLinterTool().execute("lint-1", {}, context)

    assert result.is_error
    assert "No Python files or packages" in result.output


@pytest.mark.asyncio
async def test_run_typecheck_fails_when_explicit_target_is_missing(tmp_path):
    context = ToolExecutionContext(session_id="test", working_directory=tmp_path)

    result = await RunTypecheckTool().execute("type-2", {"args": ["missing"]}, context)

    assert result.is_error
    assert "does not exist" in result.output


@pytest.mark.asyncio
async def test_run_typecheck_rejects_explicit_target_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    context = ToolExecutionContext(session_id="test", working_directory=tmp_path)

    result = await RunTypecheckTool().execute("type-3", {"args": [str(outside)]}, context)

    assert result.is_error
    assert "outside the workspace" in result.output


@pytest.mark.asyncio
async def test_run_tests_returns_structured_metadata_for_focused_pytest():
    repo_root = Path.cwd()
    context = ToolExecutionContext(session_id="test", working_directory=repo_root)

    result = await RunTestsTool().execute(
        "tests-1",
        {"args": ["-q", "tests/test_config.py::test_init_creates_knowledge_file"], "timeout": 120},
        context,
    )

    assert not result.is_error
    assert result.metadata["passed"] is True
    assert result.metadata["exit_code"] == 0


@pytest.mark.asyncio
async def test_code_index_and_semantic_search_return_counts(tmp_path):
    source = tmp_path / "app.py"
    source.write_text(
        "import json\n\n"
        "class Planner:\n"
        "    def build_plan(self):\n"
        "        return json.dumps({'ok': True})\n",
        encoding="utf-8",
    )
    context = ToolExecutionContext(session_id="test", working_directory=tmp_path)

    index = await CodeIndexTool().execute("index-1", {}, context)
    search = await SemanticSearchTool().execute("search-1", {"query": "build plan"}, context)

    assert not index.is_error
    assert index.metadata["file_count"] == 1
    assert index.metadata["symbol_count"] == 2
    assert not search.is_error
    assert search.metadata["count"] >= 1
