"""Tests for nexus/tools/filesystem.py and the extended PermissionChecker."""
from __future__ import annotations

import pytest

from nexus.models import ToolExecutionContext
from nexus.runtime.execution import ExecutionMode
from nexus.security import PermissionChecker, PermissionDecision
from nexus.tools.filesystem import (
    BashTool,
    GlobTool,
    GrepTool,
    LsTool,
    ModifyFileTool,
    ReadFileTool,
    ReplaceTextTool,
    WriteFileTool,
    classify_bash_risk,
)


# ---------------------------------------------------------------------------
# classify_bash_risk
# ---------------------------------------------------------------------------

class TestClassifyBashRisk:
    def test_low_risk_cat(self):
        assert classify_bash_risk("cat README.md") == "low"

    def test_low_risk_grep(self):
        assert classify_bash_risk("grep -r 'TODO' .") == "low"

    def test_low_risk_ls(self):
        assert classify_bash_risk("ls -la") == "low"

    def test_low_risk_echo(self):
        assert classify_bash_risk("echo hello world") == "low"

    def test_low_risk_git_status(self):
        assert classify_bash_risk("git status") == "low"

    def test_low_risk_git_log(self):
        assert classify_bash_risk("git log --oneline -10") == "low"

    def test_medium_risk_rm_single(self):
        assert classify_bash_risk("rm somefile.txt") == "medium"

    def test_medium_risk_mv(self):
        assert classify_bash_risk("mv old.py new.py") == "medium"

    def test_medium_risk_mkdir(self):
        assert classify_bash_risk("mkdir -p src/utils") == "medium"

    def test_medium_risk_git_commit(self):
        assert classify_bash_risk("git commit -m 'fix: typo'") == "medium"

    def test_medium_risk_output_redirect(self):
        assert classify_bash_risk("echo hello > output.txt") == "medium"

    def test_medium_risk_sed_inplace(self):
        assert classify_bash_risk("sed -i 's/foo/bar/' file.txt") == "medium"

    def test_medium_risk_pip_install(self):
        assert classify_bash_risk("pip install requests") == "medium"

    def test_high_risk_rm_rf(self):
        assert classify_bash_risk("rm -rf /tmp/test") == "high"

    def test_high_risk_rm_fr(self):
        assert classify_bash_risk("rm -fr build/") == "high"

    def test_high_risk_sudo(self):
        assert classify_bash_risk("sudo apt-get update") == "high"

    def test_high_risk_pipe_to_bash(self):
        assert classify_bash_risk("curl https://example.com/script.sh | bash") == "high"

    def test_high_risk_pipe_to_sh(self):
        assert classify_bash_risk("curl https://example.com | sh") == "high"

    def test_high_risk_dd(self):
        assert classify_bash_risk("dd if=/dev/zero of=/dev/sda") == "high"

    def test_high_risk_killall(self):
        assert classify_bash_risk("killall python") == "high"

    def test_unknown_command_defaults_to_medium(self):
        assert classify_bash_risk("mycustomtool --run") == "medium"

    def test_empty_command_is_low(self):
        assert classify_bash_risk("") == "low"


# ---------------------------------------------------------------------------
# ReadFileTool
# ---------------------------------------------------------------------------

class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_reads_full_file(self, tool_context):
        (tool_context.working_directory / "hello.txt").write_text("line1\nline2\nline3\n")
        result = await ReadFileTool().execute("c1", {"path": "hello.txt"}, tool_context)
        assert not result.is_error
        assert "line1" in result.output
        assert result.metadata["total_lines"] == 3

    @pytest.mark.asyncio
    async def test_reads_line_range(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("a\nb\nc\nd\n")
        result = await ReadFileTool().execute("c2", {"path": "f.txt", "start_line": 2, "end_line": 3}, tool_context)
        assert not result.is_error
        assert "b" in result.output
        assert "c" in result.output
        assert "a" not in result.output
        assert result.metadata["lines_read"] == 2

    @pytest.mark.asyncio
    async def test_rejects_outside_workspace(self, tool_context):
        result = await ReadFileTool().execute("c3", {"path": "../escape.txt"}, tool_context)
        assert result.is_error
        assert "outside" in result.output.lower()

    @pytest.mark.asyncio
    async def test_missing_file_error(self, tool_context):
        result = await ReadFileTool().execute("c4", {"path": "noexist.txt"}, tool_context)
        assert result.is_error
        assert "not found" in result.output.lower()

    @pytest.mark.asyncio
    async def test_missing_path_argument(self, tool_context):
        result = await ReadFileTool().execute("c5", {}, tool_context)
        assert result.is_error


# ---------------------------------------------------------------------------
# WriteFileTool
# ---------------------------------------------------------------------------

class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_creates_new_file(self, tool_context):
        result = await WriteFileTool().execute(
            "c1", {"path": "new.txt", "content": "hello"}, tool_context
        )
        assert not result.is_error
        assert (tool_context.working_directory / "new.txt").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_overwrites_existing_file(self, tool_context):
        (tool_context.working_directory / "old.txt").write_text("old content")
        result = await WriteFileTool().execute(
            "c2", {"path": "old.txt", "content": "new content"}, tool_context
        )
        assert not result.is_error
        assert (tool_context.working_directory / "old.txt").read_text() == "new content"

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, tool_context):
        result = await WriteFileTool().execute(
            "c3", {"path": "deep/nested/file.txt", "content": "data"}, tool_context
        )
        assert not result.is_error
        assert (tool_context.working_directory / "deep/nested/file.txt").exists()

    @pytest.mark.asyncio
    async def test_rejects_outside_workspace(self, tool_context):
        result = await WriteFileTool().execute(
            "c4", {"path": "../escape.txt", "content": "nope"}, tool_context
        )
        assert result.is_error
        assert "outside" in result.output.lower()

    @pytest.mark.asyncio
    async def test_rejects_nexus_state(self, tool_context):
        (tool_context.working_directory / ".nexus").mkdir(exist_ok=True)
        result = await WriteFileTool().execute(
            "c5", {"path": ".nexus/config.toml", "content": "bad"}, tool_context
        )
        assert result.is_error
        assert ".nexus" in result.output.lower()


# ---------------------------------------------------------------------------
# ModifyFileTool
# ---------------------------------------------------------------------------

class TestModifyFileTool:
    @pytest.mark.asyncio
    async def test_replaces_line_range(self, tool_context):
        (tool_context.working_directory / "src.txt").write_text("line1\nline2\nline3\n")
        result = await ModifyFileTool().execute(
            "c1",
            {"path": "src.txt", "start_line": 2, "end_line": 2, "new_content": "replaced\n"},
            tool_context,
        )
        assert not result.is_error
        content = (tool_context.working_directory / "src.txt").read_text()
        assert "replaced" in content
        assert "line2" not in content
        assert "line1" in content
        assert "line3" in content

    @pytest.mark.asyncio
    async def test_start_gt_end_is_error(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("a\nb\n")
        result = await ModifyFileTool().execute(
            "c2",
            {"path": "f.txt", "start_line": 3, "end_line": 1, "new_content": "x"},
            tool_context,
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_start_beyond_eof_is_error(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("only one line\n")
        result = await ModifyFileTool().execute(
            "c3",
            {"path": "f.txt", "start_line": 99, "end_line": 100, "new_content": "x"},
            tool_context,
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_rejects_outside_workspace(self, tool_context):
        result = await ModifyFileTool().execute(
            "c4",
            {"path": "../escape.txt", "start_line": 1, "end_line": 1, "new_content": "x"},
            tool_context,
        )
        assert result.is_error
        assert "outside" in result.output.lower()


# ---------------------------------------------------------------------------
# ReplaceTextTool
# ---------------------------------------------------------------------------

class TestReplaceTextTool:
    @pytest.mark.asyncio
    async def test_replaces_first_occurrence(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("foo foo foo")
        result = await ReplaceTextTool().execute(
            "c1", {"path": "f.txt", "old_text": "foo", "new_text": "bar"}, tool_context
        )
        assert not result.is_error
        assert (tool_context.working_directory / "f.txt").read_text() == "bar foo foo"
        assert result.metadata["occurrences_replaced"] == 1

    @pytest.mark.asyncio
    async def test_replaces_all_occurrences(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("x x x")
        result = await ReplaceTextTool().execute(
            "c2",
            {"path": "f.txt", "old_text": "x", "new_text": "y", "replace_all": True},
            tool_context,
        )
        assert not result.is_error
        assert (tool_context.working_directory / "f.txt").read_text() == "y y y"
        assert result.metadata["occurrences_replaced"] == 3

    @pytest.mark.asyncio
    async def test_text_not_found_is_error(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("hello")
        result = await ReplaceTextTool().execute(
            "c3", {"path": "f.txt", "old_text": "missing", "new_text": "x"}, tool_context
        )
        assert result.is_error
        assert "not found" in result.output.lower()

    @pytest.mark.asyncio
    async def test_rejects_outside_workspace(self, tool_context):
        result = await ReplaceTextTool().execute(
            "c4", {"path": "../escape.txt", "old_text": "a", "new_text": "b"}, tool_context
        )
        assert result.is_error
        assert "outside" in result.output.lower()


# ---------------------------------------------------------------------------
# GlobTool
# ---------------------------------------------------------------------------

class TestGlobTool:
    @pytest.mark.asyncio
    async def test_finds_matching_files(self, tool_context):
        (tool_context.working_directory / "a.py").write_text("")
        (tool_context.working_directory / "b.py").write_text("")
        (tool_context.working_directory / "c.txt").write_text("")
        result = await GlobTool().execute("c1", {"pattern": "*.py"}, tool_context)
        assert not result.is_error
        assert "a.py" in result.output
        assert "b.py" in result.output
        assert "c.txt" not in result.output
        assert result.metadata["match_count"] == 2

    @pytest.mark.asyncio
    async def test_no_matches(self, tool_context):
        result = await GlobTool().execute("c2", {"pattern": "*.xyz"}, tool_context)
        assert not result.is_error
        assert "no matches" in result.output.lower()
        assert result.metadata["match_count"] == 0

    @pytest.mark.asyncio
    async def test_recursive_glob(self, tool_context):
        sub = tool_context.working_directory / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("")
        result = await GlobTool().execute("c3", {"pattern": "**/*.py"}, tool_context)
        assert not result.is_error
        assert "deep.py" in result.output


# ---------------------------------------------------------------------------
# GrepTool
# ---------------------------------------------------------------------------

class TestGrepTool:
    @pytest.mark.asyncio
    async def test_finds_pattern_in_files(self, tool_context):
        (tool_context.working_directory / "a.txt").write_text("hello world\ngoodbye")
        (tool_context.working_directory / "b.txt").write_text("no match here")
        result = await GrepTool().execute("c1", {"pattern": "hello"}, tool_context)
        assert not result.is_error
        assert "a.txt" in result.output
        assert "hello world" in result.output
        assert "b.txt" not in result.output

    @pytest.mark.asyncio
    async def test_fixed_string_match(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("foo.bar\n")
        # Without fixed_string, "foo.bar" regex matches "fooXbar" too.
        # With fixed_string it matches the literal dot.
        result = await GrepTool().execute(
            "c2", {"pattern": "foo.bar", "fixed_string": True}, tool_context
        )
        assert not result.is_error
        assert "foo.bar" in result.output

    @pytest.mark.asyncio
    async def test_case_insensitive(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("Hello World\n")
        result = await GrepTool().execute(
            "c3", {"pattern": "hello", "case_insensitive": True}, tool_context
        )
        assert not result.is_error
        assert "Hello World" in result.output

    @pytest.mark.asyncio
    async def test_no_matches(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("nothing relevant\n")
        result = await GrepTool().execute("c4", {"pattern": "xyz123"}, tool_context)
        assert not result.is_error
        assert "no matches" in result.output.lower()

    @pytest.mark.asyncio
    async def test_invalid_regex_is_error(self, tool_context):
        result = await GrepTool().execute("c5", {"pattern": "["}, tool_context)
        assert result.is_error
        assert "invalid" in result.output.lower()

    @pytest.mark.asyncio
    async def test_rejects_outside_workspace(self, tool_context):
        result = await GrepTool().execute("c6", {"pattern": "x", "path": "../.."}, tool_context)
        assert result.is_error
        assert "outside" in result.output.lower()


# ---------------------------------------------------------------------------
# LsTool
# ---------------------------------------------------------------------------

class TestLsTool:
    @pytest.mark.asyncio
    async def test_lists_workspace_root(self, tool_context):
        (tool_context.working_directory / "file.txt").write_text("content")
        (tool_context.working_directory / "subdir").mkdir()
        result = await LsTool().execute("c1", {}, tool_context)
        assert not result.is_error
        assert "subdir/" in result.output
        assert "file.txt" in result.output

    @pytest.mark.asyncio
    async def test_hides_dotfiles_by_default(self, tool_context):
        (tool_context.working_directory / ".hidden").write_text("")
        (tool_context.working_directory / "visible.txt").write_text("")
        result = await LsTool().execute("c2", {}, tool_context)
        assert not result.is_error
        assert ".hidden" not in result.output
        assert "visible.txt" in result.output

    @pytest.mark.asyncio
    async def test_shows_hidden_when_requested(self, tool_context):
        (tool_context.working_directory / ".hidden").write_text("")
        result = await LsTool().execute("c3", {"show_hidden": True}, tool_context)
        assert not result.is_error
        assert ".hidden" in result.output

    @pytest.mark.asyncio
    async def test_subdirectory(self, tool_context):
        sub = tool_context.working_directory / "sub"
        sub.mkdir()
        (sub / "child.txt").write_text("")
        result = await LsTool().execute("c4", {"path": "sub"}, tool_context)
        assert not result.is_error
        assert "child.txt" in result.output

    @pytest.mark.asyncio
    async def test_rejects_outside_workspace(self, tool_context):
        result = await LsTool().execute("c5", {"path": "../.."}, tool_context)
        assert result.is_error
        assert "outside" in result.output.lower()

    @pytest.mark.asyncio
    async def test_nonexistent_path_is_error(self, tool_context):
        result = await LsTool().execute("c6", {"path": "no_such_dir"}, tool_context)
        assert result.is_error


# ---------------------------------------------------------------------------
# BashTool
# ---------------------------------------------------------------------------

class TestBashTool:
    @pytest.mark.asyncio
    async def test_runs_simple_command(self, tool_context):
        result = await BashTool().execute("c1", {"command": "echo hello"}, tool_context)
        assert not result.is_error
        assert "hello" in result.output
        assert result.metadata["risk"] == "low"

    @pytest.mark.asyncio
    async def test_captures_stderr(self, tool_context):
        result = await BashTool().execute(
            "c2", {"command": "echo err >&2"}, tool_context
        )
        assert "[stderr]" in result.output or "err" in result.output

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_error(self, tool_context):
        result = await BashTool().execute("c3", {"command": "exit 1"}, tool_context)
        assert result.is_error
        assert result.metadata["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_risk_metadata_attached(self, tool_context):
        result = await BashTool().execute("c4", {"command": "rm file.txt"}, tool_context)
        assert result.metadata["risk"] == "medium"

    @pytest.mark.asyncio
    async def test_runs_in_workspace_root(self, tool_context):
        (tool_context.working_directory / "marker.txt").write_text("found")
        result = await BashTool().execute(
            "c5", {"command": "cat marker.txt"}, tool_context
        )
        assert not result.is_error
        assert "found" in result.output

    @pytest.mark.asyncio
    async def test_missing_command_is_error(self, tool_context):
        result = await BashTool().execute("c6", {}, tool_context)
        assert result.is_error


# ---------------------------------------------------------------------------
# Extended PermissionChecker
# ---------------------------------------------------------------------------

class TestPermissionCheckerFilesystemTools:
    def _ctx(self, tmp_path):
        return ToolExecutionContext(session_id="s", working_directory=tmp_path)

    # --- bash low risk ---

    def test_bash_low_risk_allowed_in_default(self, tmp_path):
        tool = BashTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "cat README.md"}, ExecutionMode.DEFAULT, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    def test_bash_low_risk_allowed_in_plan(self, tmp_path):
        tool = BashTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "ls -la"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    # --- bash medium risk ---

    def test_bash_medium_risk_confirm_in_default(self, tmp_path):
        tool = BashTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "mkdir newdir"}, ExecutionMode.DEFAULT, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_bash_medium_risk_allowed_in_auto(self, tmp_path):
        tool = BashTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "mkdir newdir"}, ExecutionMode.AUTO, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    def test_bash_medium_risk_denied_in_plan(self, tmp_path):
        tool = BashTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "rm file.txt"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.DENY

    # --- bash high risk ---

    def test_bash_high_risk_confirm_in_default(self, tmp_path):
        tool = BashTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "rm -rf /"}, ExecutionMode.DEFAULT, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_bash_high_risk_confirm_even_in_auto(self, tmp_path):
        """High-risk bash always requires confirmation — auto mode does NOT bypass it."""
        tool = BashTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "sudo rm -rf /"}, ExecutionMode.AUTO, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_bash_high_risk_denied_in_plan(self, tmp_path):
        tool = BashTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "rm -rf build/"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.DENY

    # --- write_file always confirms ---

    def test_write_file_confirm_in_default(self, tmp_path):
        tool = WriteFileTool()
        result = PermissionChecker().evaluate(
            tool, {"path": "f.txt", "content": "x"}, ExecutionMode.DEFAULT, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_write_file_confirm_even_in_auto(self, tmp_path):
        """write_file always requires confirmation — auto mode does NOT bypass it."""
        tool = WriteFileTool()
        result = PermissionChecker().evaluate(
            tool, {"path": "f.txt", "content": "x"}, ExecutionMode.AUTO, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_write_file_denied_in_plan(self, tmp_path):
        tool = WriteFileTool()
        result = PermissionChecker().evaluate(
            tool, {"path": "f.txt", "content": "x"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.DENY

    # --- modify_file / replace_text use standard mutating logic ---

    def test_modify_file_confirm_in_default(self, tmp_path):
        tool = ModifyFileTool()
        result = PermissionChecker().evaluate(
            tool,
            {"path": "f.txt", "start_line": 1, "end_line": 1, "new_content": "x"},
            ExecutionMode.DEFAULT,
            context=self._ctx(tmp_path),
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_modify_file_allowed_in_auto(self, tmp_path):
        tool = ModifyFileTool()
        result = PermissionChecker().evaluate(
            tool,
            {"path": "f.txt", "start_line": 1, "end_line": 1, "new_content": "x"},
            ExecutionMode.AUTO,
            context=self._ctx(tmp_path),
        )
        assert result.decision is PermissionDecision.ALLOW

    def test_modify_file_denied_in_plan(self, tmp_path):
        tool = ModifyFileTool()
        result = PermissionChecker().evaluate(
            tool,
            {"path": "f.txt", "start_line": 1, "end_line": 1, "new_content": "x"},
            ExecutionMode.PLAN,
            context=self._ctx(tmp_path),
        )
        assert result.decision is PermissionDecision.DENY

    # --- read / glob / grep / ls are always allowed ---

    def test_read_file_allowed_in_plan(self, tmp_path):
        result = PermissionChecker().evaluate(
            ReadFileTool(), {"path": "f.txt"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    def test_glob_allowed_in_plan(self, tmp_path):
        result = PermissionChecker().evaluate(
            GlobTool(), {"pattern": "**/*.py"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    def test_grep_allowed_in_plan(self, tmp_path):
        result = PermissionChecker().evaluate(
            GrepTool(), {"pattern": "TODO"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    def test_ls_allowed_in_plan(self, tmp_path):
        result = PermissionChecker().evaluate(
            LsTool(), {}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    # --- path-policy hard denials for write tools ---

    def test_write_file_denied_outside_workspace(self, tmp_path):
        tool = WriteFileTool()
        result = PermissionChecker().evaluate(
            tool, {"path": "../escape.txt", "content": "x"}, ExecutionMode.AUTO, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.DENY

    def test_modify_file_denied_into_nexus_state(self, tmp_path):
        tool = ModifyFileTool()
        result = PermissionChecker().evaluate(
            tool,
            {"path": ".nexus/config.toml", "start_line": 1, "end_line": 1, "new_content": "bad"},
            ExecutionMode.AUTO,
            context=self._ctx(tmp_path),
        )
        assert result.decision is PermissionDecision.DENY

    def test_replace_text_denied_outside_workspace(self, tmp_path):
        tool = ReplaceTextTool()
        result = PermissionChecker().evaluate(
            tool, {"path": "../../etc/passwd", "old_text": "a", "new_text": "b"}, ExecutionMode.AUTO, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.DENY
