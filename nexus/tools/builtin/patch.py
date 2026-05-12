"""ApplyPatchTool — apply a unified diff patch to files in the workspace.

Parses a **unified diff** (``diff -u`` / ``git diff`` format) and applies
each hunk to the corresponding file.  The tool is a pure-Python implementation
— no external ``patch`` binary required.

Unified diff format recap
-------------------------
::

    --- a/path/to/file.py
    +++ b/path/to/file.py
    @@ -start,count +start,count @@
     context line
    -removed line
    +added line
     context line

``---`` / ``+++`` headers may use ``a/`` / ``b/`` prefixes (as produced by
``git diff``), plain paths, or ``/dev/null`` for new/deleted files.

Errors
------
The tool returns an error (per-file) if:

* A context line in a hunk does not match the actual file content.
* A removed line in a hunk does not match the actual file content.
* The patch would write outside the workspace or into ``.nexus/``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import Tool, ToolKind

# ---------------------------------------------------------------------------
# Patch parser
# ---------------------------------------------------------------------------

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


class _Hunk:
    __slots__ = ("old_start", "old_count", "new_start", "new_count", "lines")

    def __init__(
        self,
        old_start: int,
        old_count: int,
        new_start: int,
        new_count: int,
    ) -> None:
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.lines: list[str] = []  # raw diff lines (include leading +/-/ )


class _FilePatch:
    __slots__ = ("old_path", "new_path", "hunks", "is_new_file", "is_deleted_file")

    def __init__(self, old_path: str, new_path: str) -> None:
        self.old_path = old_path
        self.new_path = new_path
        self.hunks: list[_Hunk] = []
        self.is_new_file = old_path == "/dev/null"
        self.is_deleted_file = new_path == "/dev/null"


def _strip_prefix(path_str: str) -> str:
    """Remove ``a/`` or ``b/`` prefix that git diff inserts."""
    for prefix in ("a/", "b/"):
        if path_str.startswith(prefix):
            return path_str[len(prefix):]
    return path_str


def _parse_patch(patch_text: str) -> list[_FilePatch]:
    """Parse *patch_text* into a list of :class:`_FilePatch` objects."""
    file_patches: list[_FilePatch] = []
    current_file: _FilePatch | None = None
    current_hunk: _Hunk | None = None
    pending_old_path: str | None = None

    for raw_line in patch_text.splitlines(keepends=True):
        line = raw_line.rstrip("\n").rstrip("\r")

        if line.startswith("--- "):
            pending_old_path = _strip_prefix(line[4:].split("\t")[0].strip())
            current_file = None
            continue

        if line.startswith("+++ ") and pending_old_path is not None:
            new_path = _strip_prefix(line[4:].split("\t")[0].strip())
            current_file = _FilePatch(pending_old_path, new_path)
            assert current_file is not None
            file_patches.append(current_file)
            current_hunk = None
            pending_old_path = None
            continue

        if current_file is None:
            continue

        m = _HUNK_HEADER_RE.match(line)
        if m:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            current_hunk = _Hunk(old_start, old_count, new_start, new_count)
            assert current_hunk is not None
            current_file.hunks.append(current_hunk)
            continue

        if current_hunk is None:
            continue

        if line.startswith(("+", "-", " ")):
            current_hunk.lines.append(raw_line if raw_line.endswith("\n") else raw_line + "\n")

    return file_patches


# ---------------------------------------------------------------------------
# Patch applier
# ---------------------------------------------------------------------------

def _apply_hunk(file_lines: list[str], hunk: _Hunk) -> list[str] | str:
    """Apply a single *hunk* to *file_lines*.

    Returns the updated lines list, or a human-readable error string on
    failure.
    """
    # old_start is 1-indexed; 0 means the file is being created (hunk before BOF)
    pos = max(0, hunk.old_start - 1)
    result: list[str] = list(file_lines[:pos])

    file_pos = pos
    for raw in hunk.lines:
        if not raw:
            continue
        op = raw[0]
        content = raw[1:]

        if op == " ":
            # Context line — must match file
            if file_pos >= len(file_lines):
                return (
                    f"Hunk context line not found at file line {file_pos + 1}: "
                    f"{content.rstrip()!r}"
                )
            actual = file_lines[file_pos]
            if actual.rstrip("\n\r") != content.rstrip("\n\r"):
                return (
                    f"Context mismatch at line {file_pos + 1}: "
                    f"expected {content.rstrip()!r}, got {actual.rstrip()!r}"
                )
            result.append(actual)
            file_pos += 1
        elif op == "-":
            # Removed line — must match file, skip in output
            if file_pos >= len(file_lines):
                return (
                    f"Removed line not found at file line {file_pos + 1}: "
                    f"{content.rstrip()!r}"
                )
            actual = file_lines[file_pos]
            if actual.rstrip("\n\r") != content.rstrip("\n\r"):
                return (
                    f"Remove mismatch at line {file_pos + 1}: "
                    f"expected {content.rstrip()!r}, got {actual.rstrip()!r}"
                )
            file_pos += 1  # consume without outputting
        elif op == "+":
            result.append(content if content.endswith("\n") else content + "\n")
        # ignore other op chars (e.g. '\')

    result.extend(file_lines[file_pos:])
    return result


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------

def _resolve(workspace: Path, raw: str) -> Path:
    p = Path(raw)
    return (workspace / p).resolve() if not p.is_absolute() else p.resolve()


def _workspace_check(path: Path, workspace: Path) -> str | None:
    try:
        path.relative_to(workspace)
    except ValueError:
        return "Refusing to write outside the current workspace."
    nexus_root = (workspace / ".nexus").resolve()
    try:
        path.relative_to(nexus_root)
        return "Refusing to write into .nexus managed state."
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class ApplyPatchTool(Tool):
    """Apply a unified diff patch to one or more files in the workspace.

    Accepts standard unified diff output (``diff -u``, ``git diff``).  Each
    ``---``/``+++`` file header selects the target file; ``@@`` hunks describe
    the changes.  Context and removed lines are verified against the actual file
    content before writing.

    Use this tool when you have a patch string ready (e.g. from a diff command
    or from preparing changes ahead of time).  For interactive edits prefer
    :class:`~nexus.tools.builtin.edit_file.EditTool` or
    :class:`~nexus.tools.builtin.smart_edit.InsertEditIntoFileTool`.
    """

    name = "apply_patch"
    description = (
        "Apply a unified diff patch (diff -u / git diff format) to files in the workspace. "
        "Verifies context and removed lines against actual file content before writing. "
        "Supports multi-file patches, new-file creation (/dev/null old path), and "
        "file deletion (/dev/null new path). "
        "Cannot patch files outside the workspace or in .nexus/ managed state."
    )
    kind = ToolKind.WRITE
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Unified diff patch text (output of diff -u or git diff). "
                    "May contain changes for multiple files."
                ),
            },
            "strip": {
                "type": "integer",
                "minimum": 0,
                "description": (
                    "Number of leading path components to strip from filenames "
                    "(like patch -p). Defaults to 1 to strip the a/ b/ git diff prefix."
                ),
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    }

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        patch_text = str(arguments.get("patch", "")).strip()
        if not patch_text:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Missing required argument: patch", is_error=True,
            )

        strip = int(arguments.get("strip", 1))
        workspace = context.working_directory.resolve()

        file_patches = _parse_patch(patch_text)
        if not file_patches:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="No file patches found in the supplied patch text.", is_error=True,
            )

        results: list[str] = []
        errors: list[str] = []

        for fp in file_patches:
            # Determine target path (prefer new_path unless deletion)
            raw_path = fp.new_path if not fp.is_deleted_file else fp.old_path

            # Apply strip (like patch -pN)
            parts = Path(raw_path).parts
            if strip > 0 and len(parts) > strip:
                raw_path = str(Path(*parts[strip:]))

            target = _resolve(workspace, raw_path)
            if err := _workspace_check(target, workspace):
                errors.append(f"{raw_path}: {err}")
                continue

            # --- New file ---
            if fp.is_new_file:
                if target.exists():
                    errors.append(f"{raw_path}: File already exists (patch creates new file).")
                    continue
                # Collect all '+' lines from all hunks
                new_lines: list[str] = []
                for hunk in fp.hunks:
                    for raw in hunk.lines:
                        if raw.startswith("+"):
                            content = raw[1:]
                            new_lines.append(content if content.endswith("\n") else content + "\n")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("".join(new_lines), encoding="utf-8")
                results.append(f"Created {target.relative_to(workspace)}")
                continue

            # --- Deleted file ---
            if fp.is_deleted_file:
                if not target.exists():
                    errors.append(f"{raw_path}: File not found (patch deletes file).")
                    continue
                target.unlink()
                results.append(f"Deleted {target.relative_to(workspace)}")
                continue

            # --- Modify existing file ---
            if not target.exists():
                errors.append(f"{raw_path}: File not found.")
                continue

            try:
                original = target.read_text(encoding="utf-8")
            except OSError as exc:
                errors.append(f"{raw_path}: Read error — {exc}")
                continue

            file_lines = original.splitlines(keepends=True)

            # Apply hunks in order (they come sorted by line number)
            ok = True
            for hunk in fp.hunks:
                result = _apply_hunk(file_lines, hunk)
                if isinstance(result, str):
                    errors.append(f"{raw_path}: {result}")
                    ok = False
                    break
                file_lines = result

            if not ok:
                continue

            try:
                target.write_text("".join(file_lines), encoding="utf-8")
            except OSError as exc:
                errors.append(f"{raw_path}: Write error — {exc}")
                continue

            hunks_applied = len(fp.hunks)
            results.append(
                f"Patched {target.relative_to(workspace)} — {hunks_applied} hunk(s) applied"
            )

        if errors and not results:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="\n".join(errors), is_error=True,
            )

        summary_parts: list[str] = []
        if results:
            summary_parts.append("\n".join(results))
        if errors:
            summary_parts.append("Errors:\n" + "\n".join(errors))

        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output="\n".join(summary_parts),
            is_error=bool(errors),
            metadata={
                "files_patched": len(results),
                "files_errored": len(errors),
            },
        )

