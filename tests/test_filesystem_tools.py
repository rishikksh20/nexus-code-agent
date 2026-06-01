"""Tests for nexus/tools/filesystem.py and the extended PermissionChecker."""
from __future__ import annotations

import pytest

from nexus.models import ToolExecutionContext
from nexus.runtime.execution import ExecutionMode
from nexus.security import PermissionChecker, PermissionDecision
from nexus.tools.builtin.edit_file import EditTool
from nexus.tools.filesystem import (
    ShellTool,
    GlobTool,
    GrepTool,
    ListDirTool,
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

    def test_medium_risk_env_dump(self):
        assert classify_bash_risk("env") == "medium"
        assert classify_bash_risk("printenv") == "medium"

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

    @pytest.mark.asyncio
    async def test_rejects_hidden_file_reads_by_default(self, tool_context):
        (tool_context.working_directory / ".env").write_text("API_KEY=test\n")

        result = await ReadFileTool().execute("c6", {"path": ".env"}, tool_context)

        assert result.is_error
        assert "hidden/private" in result.output.lower()

    @pytest.mark.asyncio
    async def test_reads_standard_agent_resource_files_by_default(self, tool_context):
        skill_dir = tool_context.working_directory / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        tools_dir = tool_context.working_directory / ".agents" / "tools"
        tools_dir.mkdir()
        (tools_dir / "demo.py").write_text("# Demo tool\n", encoding="utf-8")

        skill_result = await ReadFileTool().execute(
            "c7", {"path": ".agents/skills/review/SKILL.md"}, tool_context
        )
        tool_result = await ReadFileTool().execute("c8", {"path": ".agents/tools/demo.py"}, tool_context)

        assert not skill_result.is_error
        assert skill_result.output == "# Review"
        assert not tool_result.is_error
        assert tool_result.output == "# Demo tool"

    @pytest.mark.asyncio
    async def test_allows_hidden_file_reads_when_enabled_except_nexus(self, tool_context):
        (tool_context.working_directory / ".env").write_text("API_KEY=test\n")
        (tool_context.working_directory / ".nexus").mkdir(exist_ok=True)
        (tool_context.working_directory / ".nexus" / "config.toml").write_text("provider = 'fake'\n")
        allow_hidden_context = ToolExecutionContext(
            session_id=tool_context.session_id,
            working_directory=tool_context.working_directory,
            metadata={"allow_hidden_paths": True},
        )

        hidden_result = await ReadFileTool().execute("c7", {"path": ".env"}, allow_hidden_context)
        nexus_result = await ReadFileTool().execute("c8", {"path": ".nexus/config.toml"}, allow_hidden_context)

        assert not hidden_result.is_error
        assert hidden_result.output == "API_KEY=test"
        assert nexus_result.is_error
        assert ".nexus" in nexus_result.output


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
# EditTool
# ---------------------------------------------------------------------------

class TestEditTool:
    @pytest.mark.asyncio
    async def test_recovers_from_indentation_drift(self, tool_context):
        target = tool_context.working_directory / "nested.py"
        target.write_text(
            "def outer():\n"
            "    if True:\n"
            '        print("before")\n'
            "        return 1\n\n"
            'print("done")\n',
            encoding="utf-8",
        )

        result = await EditTool().execute(
            "edit-1",
            {
                "path": "nested.py",
                "old_string": 'if True:\n    print("before")\n    return 1\n',
                "new_string": 'if True:\n    print("after")\n    return 2\n',
            },
            tool_context,
        )

        assert not result.is_error
        assert "matching" in result.output
        assert target.read_text(encoding="utf-8") == (
            "def outer():\n"
            "    if True:\n"
            '        print("after")\n'
            "        return 2\n\n"
            'print("done")\n'
        )

    @pytest.mark.asyncio
    async def test_recovers_from_escaped_newlines(self, tool_context):
        target = tool_context.working_directory / "escaped.txt"
        target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

        result = await EditTool().execute(
            "edit-2",
            {
                "path": "escaped.txt",
                "old_string": "beta\\ngamma\\n",
                "new_string": "beta\\ndelta\\n",
            },
            tool_context,
        )

        assert not result.is_error
        assert target.read_text(encoding="utf-8") == "alpha\nbeta\ndelta\n"

    @pytest.mark.asyncio
    async def test_rejects_fuzzy_replace_all_without_more_context(self, tool_context):
        target = tool_context.working_directory / "ambiguous.py"
        target.write_text(
            "def one():\n"
            "    total = old_value\n\n"
            "def two():\n"
            "    total = old_value\n",
            encoding="utf-8",
        )

        result = await EditTool().execute(
            "edit-3",
            {
                "path": "ambiguous.py",
                "old_string": "total=old_value",
                "new_string": "total=new_value",
                "replace_all": True,
            },
            tool_context,
        )

        assert result.is_error
        assert "Provide more surrounding context" in result.output
        assert target.read_text(encoding="utf-8") == (
            "def one():\n"
            "    total = old_value\n\n"
            "def two():\n"
            "    total = old_value\n"
        )


# ---------------------------------------------------------------------------
# ModifyFileTool
# ---------------------------------------------------------------------------

class TestModifyFileTool:
    @pytest.mark.asyncio
    async def test_builds_confirmation_diff(self, tool_context):
        (tool_context.working_directory / "src.txt").write_text("line1\nline2\nline3\n")

        confirmation = await ModifyFileTool().get_confirmation(
            "confirm-1",
            {"path": "src.txt", "start_line": 2, "end_line": 2, "new_content": "updated\n"},
            tool_context,
        )

        assert confirmation is not None
        assert confirmation.diff is not None
        assert "-line2" in confirmation.diff.to_diff()
        assert "+updated" in confirmation.diff.to_diff()

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
    async def test_builds_confirmation_diff(self, tool_context):
        (tool_context.working_directory / "f.txt").write_text("foo foo foo")

        confirmation = await ReplaceTextTool().get_confirmation(
            "confirm-2",
            {"path": "f.txt", "old_text": "foo", "new_text": "bar"},
            tool_context,
        )

        assert confirmation is not None
        assert confirmation.diff is not None
        assert "-foo foo foo" in confirmation.diff.to_diff()
        assert "+bar foo foo" in confirmation.diff.to_diff()

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

    @pytest.mark.asyncio
    async def test_rejects_nexus_state_via_edit_wrapper(self, tool_context):
        (tool_context.working_directory / ".nexus").mkdir(exist_ok=True)
        (tool_context.working_directory / ".nexus" / "state.txt").write_text("old")

        result = await ReplaceTextTool().execute(
            "c5",
            {"path": ".nexus/state.txt", "old_text": "old", "new_text": "new"},
            tool_context,
        )

        assert result.is_error
        assert ".nexus" in result.output


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

    @pytest.mark.asyncio
    async def test_hidden_and_private_matches_require_override(self, tool_context):
        (tool_context.working_directory / ".hidden.py").write_text("")
        private_dir = tool_context.working_directory / "private_docs"
        private_dir.mkdir()
        (private_dir / "secret.py").write_text("")
        allow_hidden_context = ToolExecutionContext(
            session_id=tool_context.session_id,
            working_directory=tool_context.working_directory,
            metadata={"allow_hidden_paths": True},
        )

        default_result = await GlobTool().execute("c4", {"pattern": "**/*.py"}, tool_context)
        override_result = await GlobTool().execute("c5", {"pattern": "**/*.py"}, allow_hidden_context)

        assert ".hidden.py" not in default_result.output
        assert "private_docs/secret.py" not in default_result.output
        assert ".hidden.py" in override_result.output
        assert "private_docs/secret.py" in override_result.output

    @pytest.mark.asyncio
    async def test_finds_standard_agent_resource_files_by_default(self, tool_context):
        skill_dir = tool_context.working_directory / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Review\n", encoding="utf-8")
        tools_dir = tool_context.working_directory / ".agents" / "tools"
        tools_dir.mkdir()
        (tools_dir / "demo.py").write_text("# Demo tool\n", encoding="utf-8")

        skill_result = await GlobTool().execute(
            "c6", {"pattern": ".agents/skills/**/SKILL.md"}, tool_context
        )
        tool_result = await GlobTool().execute("c7", {"pattern": ".agents/tools/**/*.py"}, tool_context)

        assert not skill_result.is_error
        assert ".agents/skills/review/SKILL.md" in skill_result.output
        assert not tool_result.is_error
        assert ".agents/tools/demo.py" in tool_result.output


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

    @pytest.mark.asyncio
    async def test_grep_permanently_blocks_nexus_even_with_hidden_override(self, tool_context):
        (tool_context.working_directory / ".nexus").mkdir(exist_ok=True)
        allow_hidden_context = ToolExecutionContext(
            session_id=tool_context.session_id,
            working_directory=tool_context.working_directory,
            metadata={"allow_hidden_paths": True},
        )

        result = await GrepTool().execute("c7", {"pattern": "provider", "path": ".nexus"}, allow_hidden_context)

        assert result.is_error
        assert ".nexus" in result.output

    @pytest.mark.asyncio
    async def test_searches_standard_agent_resources_but_not_other_agents_paths(self, tool_context):
        skill_dir = tool_context.working_directory / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("workspace-skill-marker\n", encoding="utf-8")
        tools_dir = tool_context.working_directory / ".agents" / "tools"
        tools_dir.mkdir()
        (tools_dir / "demo.py").write_text("workspace-skill-marker\n", encoding="utf-8")
        (tool_context.working_directory / ".agents" / "private.txt").write_text(
            "workspace-skill-marker\n", encoding="utf-8"
        )

        result = await GrepTool().execute("c8", {"pattern": "workspace-skill-marker"}, tool_context)

        assert not result.is_error
        assert ".agents/skills/review/SKILL.md" in result.output
        assert ".agents/tools/demo.py" in result.output
        assert ".agents/private.txt" not in result.output


# ---------------------------------------------------------------------------
# ListDirTool
# ---------------------------------------------------------------------------

class TestListDirTool:
    @pytest.mark.asyncio
    async def test_lists_workspace_root(self, tool_context):
        (tool_context.working_directory / "file.txt").write_text("content")
        (tool_context.working_directory / "subdir").mkdir()
        result = await ListDirTool().execute("c1", {}, tool_context)
        assert not result.is_error
        assert "subdir/" in result.output
        assert "file.txt" in result.output

    @pytest.mark.asyncio
    async def test_hides_dotfiles_by_default(self, tool_context):
        (tool_context.working_directory / ".hidden").write_text("")
        (tool_context.working_directory / "visible.txt").write_text("")
        result = await ListDirTool().execute("c2", {}, tool_context)
        assert not result.is_error
        assert ".hidden" not in result.output
        assert "visible.txt" in result.output

    @pytest.mark.asyncio
    async def test_shows_hidden_when_requested(self, tool_context):
        (tool_context.working_directory / ".hidden").write_text("")
        allow_hidden_context = ToolExecutionContext(
            session_id=tool_context.session_id,
            working_directory=tool_context.working_directory,
            metadata={"allow_hidden_paths": True},
        )
        result = await ListDirTool().execute("c3", {"show_hidden": True}, allow_hidden_context)
        assert not result.is_error
        assert ".hidden" in result.output

    @pytest.mark.asyncio
    async def test_show_hidden_requires_hidden_override_and_never_shows_nexus(self, tool_context):
        (tool_context.working_directory / ".hidden").write_text("")
        (tool_context.working_directory / ".nexus").mkdir(exist_ok=True)
        allow_hidden_context = ToolExecutionContext(
            session_id=tool_context.session_id,
            working_directory=tool_context.working_directory,
            metadata={"allow_hidden_paths": True},
        )

        default_result = await ListDirTool().execute("c7", {"show_hidden": True}, tool_context)
        override_result = await ListDirTool().execute("c8", {"show_hidden": True}, allow_hidden_context)

        assert ".hidden" not in default_result.output
        assert ".hidden" in override_result.output
        assert ".nexus" not in override_result.output

    @pytest.mark.asyncio
    async def test_subdirectory(self, tool_context):
        sub = tool_context.working_directory / "sub"
        sub.mkdir()
        (sub / "child.txt").write_text("")
        result = await ListDirTool().execute("c4", {"path": "sub"}, tool_context)
        assert not result.is_error
        assert "child.txt" in result.output

    @pytest.mark.asyncio
    async def test_lists_standard_agent_resource_directories_by_default(self, tool_context):
        skill_dir = tool_context.working_directory / ".agents" / "skills" / "review"
        skill_dir.mkdir(parents=True)
        tools_dir = tool_context.working_directory / ".agents" / "tools"
        tools_dir.mkdir()
        (tools_dir / "demo.py").write_text("# Demo tool\n", encoding="utf-8")

        skill_result = await ListDirTool().execute("c9", {"path": ".agents/skills"}, tool_context)
        tool_result = await ListDirTool().execute("c10", {"path": ".agents/tools"}, tool_context)

        assert not skill_result.is_error
        assert "review/" in skill_result.output
        assert not tool_result.is_error
        assert "demo.py" in tool_result.output

    @pytest.mark.asyncio
    async def test_rejects_outside_workspace(self, tool_context):
        result = await ListDirTool().execute("c5", {"path": "../.."}, tool_context)
        assert result.is_error
        assert "outside" in result.output.lower()

    @pytest.mark.asyncio
    async def test_nonexistent_path_is_error(self, tool_context):
        result = await ListDirTool().execute("c6", {"path": "no_such_dir"}, tool_context)
        assert result.is_error


# ---------------------------------------------------------------------------
# ShellTool
# ---------------------------------------------------------------------------

class TestShellTool:
    @pytest.mark.asyncio
    async def test_runs_simple_command(self, tool_context):
        result = await ShellTool().execute("c1", {"command": "echo hello"}, tool_context)
        assert not result.is_error
        assert "hello" in result.output
        assert result.metadata["risk"] == "low"

    @pytest.mark.asyncio
    async def test_captures_stderr(self, tool_context):
        result = await ShellTool().execute(
            "c2", {"command": "echo err >&2"}, tool_context
        )
        assert "[stderr]" in result.output or "err" in result.output

    @pytest.mark.asyncio
    async def test_nonzero_exit_is_error(self, tool_context):
        result = await ShellTool().execute("c3", {"command": "exit 1"}, tool_context)
        assert result.is_error
        assert result.metadata["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_risk_metadata_attached(self, tool_context):
        result = await ShellTool().execute("c4", {"command": "rm file.txt"}, tool_context)
        assert result.metadata["risk"] == "medium"

    @pytest.mark.asyncio
    async def test_risk_metadata_uses_shared_classifier(self, tool_context):
        command = "echo ok | sh"
        result = await ShellTool().execute("c7", {"command": command}, tool_context)
        assert result.metadata["risk"] == classify_bash_risk(command)

    @pytest.mark.asyncio
    async def test_runs_in_workspace_root(self, tool_context):
        (tool_context.working_directory / "marker.txt").write_text("found")
        result = await ShellTool().execute(
            "c5", {"command": "cat marker.txt"}, tool_context
        )
        assert not result.is_error
        assert "found" in result.output

    @pytest.mark.asyncio
    async def test_rejects_cwd_outside_workspace(self, tool_context, tmp_path):
        outside = tmp_path.parent / "outside-workspace"
        outside.mkdir()

        result = await ShellTool().execute(
            "c8", {"command": "pwd", "cwd": str(outside)}, tool_context
        )

        assert result.is_error
        assert "outside the workspace" in result.output

    @pytest.mark.asyncio
    async def test_missing_command_is_error(self, tool_context):
        result = await ShellTool().execute("c6", {}, tool_context)
        assert result.is_error


# ---------------------------------------------------------------------------
# Extended PermissionChecker
# ---------------------------------------------------------------------------

class TestPermissionCheckerFilesystemTools:
    def _ctx(self, tmp_path):
        return ToolExecutionContext(session_id="s", working_directory=tmp_path)

    # --- bash low risk ---

    def test_bash_low_risk_allowed_in_default(self, tmp_path):
        tool = ShellTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "cat README.md"}, ExecutionMode.DEFAULT, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    def test_bash_low_risk_allowed_in_plan(self, tmp_path):
        tool = ShellTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "ls -la"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    # --- bash medium risk ---

    def test_bash_medium_risk_confirm_in_default(self, tmp_path):
        tool = ShellTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "mkdir newdir"}, ExecutionMode.DEFAULT, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_bash_env_dump_confirms_in_default(self, tmp_path):
        tool = ShellTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "env"}, ExecutionMode.DEFAULT, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_bash_medium_risk_allowed_in_auto(self, tmp_path):
        tool = ShellTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "mkdir newdir"}, ExecutionMode.AUTO, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.ALLOW

    def test_bash_medium_risk_denied_in_plan(self, tmp_path):
        tool = ShellTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "rm file.txt"}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.DENY

    # --- bash high risk ---

    def test_bash_high_risk_confirm_in_default(self, tmp_path):
        tool = ShellTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "rm -rf /"}, ExecutionMode.DEFAULT, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_bash_high_risk_confirm_even_in_auto(self, tmp_path):
        """High-risk bash always requires confirmation — auto mode does NOT bypass it."""
        tool = ShellTool()
        result = PermissionChecker().evaluate(
            tool, {"command": "sudo rm -rf /"}, ExecutionMode.AUTO, context=self._ctx(tmp_path)
        )
        assert result.decision is PermissionDecision.CONFIRM

    def test_bash_high_risk_denied_in_plan(self, tmp_path):
        tool = ShellTool()
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
            ListDirTool(), {}, ExecutionMode.PLAN, context=self._ctx(tmp_path)
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
