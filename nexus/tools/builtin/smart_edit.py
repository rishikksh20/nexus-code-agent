"""InsertEditIntoFileTool — semantic context-anchor file editor.

This tool allows the model to edit a file by providing a code snippet that
contains ``...existing code...`` markers.  The markers act as anchors that tell
the tool "keep what's here unchanged"; the non-marker blocks are the *new*
content to apply at those positions.

Unlike :class:`~nexus.tools.builtin.edit_file.EditTool` (which requires an
exact ``old_string`` → ``new_string`` pair), this tool understands surrounding
context and can apply edits using fuzzy anchor matching — making it suitable for
refactoring, adding methods, and updating logic where the exact surrounding text
is not critical.

Algorithm
---------
1. Parse ``code`` into segments separated by ``...existing code...`` lines.
2. The first non-empty segment is the **top anchor** and the last non-empty
   segment is the **bottom anchor**; everything in between is **new content**.
3. Locate the top anchor in the file (exact first, then fuzzy ≥ 65% similarity).
4. Locate the bottom anchor after the top anchor.
5. Replace the span between the end of the top anchor and the start of the
   bottom anchor with the new content.
6. If there is only one segment (no markers) perform a fuzzy find-and-replace of
   the closest matching region.
"""
from __future__ import annotations

import difflib
import re
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import FileDiff, Tool, ToolConfirmation, ToolKind
from nexus.tools.utils import ensure_parent, resolve_path

# ---------------------------------------------------------------------------
# Marker detection
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(
    r"""^\s*(?://|#|/\*|<!--|--|;|"{3}|'{3})?\s*\.{3}\s*existing\s+code\s*\.{3}\s*(?:\*/|-->|"{3}|'{3})?\s*$""",
    re.IGNORECASE,
)


def _split_on_markers(code: str) -> list[str]:
    """Split *code* on ``...existing code...`` marker lines."""
    lines = code.splitlines(keepends=True)
    segments: list[str] = []
    current: list[str] = []
    for line in lines:
        if _MARKER_RE.match(line):
            segments.append("".join(current))
            current = []
        else:
            current.append(line)
    segments.append("".join(current))
    return segments


# ---------------------------------------------------------------------------
# Fuzzy-search helpers
# ---------------------------------------------------------------------------

_MIN_RATIO = 0.65  # minimum similarity to accept a fuzzy match


def _find_block_in_lines(
    file_lines: list[str],
    search_lines: list[str],
    *,
    start_from: int = 0,
) -> int:
    """Return the first index in *file_lines* (≥ *start_from*) where
    *search_lines* best matches, or ``-1`` if no match exceeds the threshold.

    Tries exact substring match first, then a sliding-window fuzzy match.
    """
    if not search_lines:
        return start_from

    n = len(search_lines)
    search_text = "".join(search_lines)

    # 1. Exact match
    for i in range(start_from, len(file_lines) - n + 1):
        if "".join(file_lines[i : i + n]) == search_text:
            return i

    # 2. Fuzzy sliding-window match
    best_ratio = 0.0
    best_idx = -1
    for i in range(start_from, max(start_from + 1, len(file_lines) - n + 1)):
        window = "".join(file_lines[i : i + n])
        ratio = difflib.SequenceMatcher(None, search_text, window, autojunk=False).ratio()
        if ratio > best_ratio and ratio >= _MIN_RATIO:
            best_ratio = ratio
            best_idx = i

    return best_idx


def _fuzzy_replace_no_anchors(
    file_lines: list[str],
    new_lines: list[str],
) -> list[str] | None:
    """Find the closest-matching block of *len(new_lines)* lines in *file_lines*
    and replace it with *new_lines*.  Returns ``None`` if no match is found.
    """
    idx = _find_block_in_lines(file_lines, new_lines)
    if idx == -1:
        return None
    return file_lines[:idx] + new_lines + file_lines[idx + len(new_lines) :]


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class InsertEditIntoFileTool(Tool):
    """Edit a file using contextual anchor blocks and ``...existing code...`` markers.

    The ``code`` parameter contains the code as it should look *after* the edit,
    with ``// ...existing code...`` (or ``# ...``) markers for regions that are
    not being changed.  The tool uses the surrounding blocks as anchors to
    locate the edit position in the file.

    Use this tool for:
    - Adding new methods or classes
    - Refactoring logic with surrounding context
    - Large contextual edits where exact strings are impractical

    For precise single-string replacements use ``edit`` (``old_string`` →
    ``new_string``).  For full rewrites use ``write_file``.
    """

    name = "insert_edit_into_file"
    description = (
        "Edit a file using semantic context anchors. "
        "Supply 'code' containing the new content with '// ...existing code...' "
        "(or '# ...existing code...') markers for unchanged regions. "
        "The first and last non-marker blocks are used as positional anchors; "
        "the content between them replaces the original span. "
        "Best for refactoring, adding methods, and large contextual edits where "
        "exact text matching is impractical. "
        "For precise replacements use 'edit'; for full rewrites use 'write_file'."
    )
    kind = ToolKind.WRITE
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "File path (relative to workspace or absolute).",
            },
            "code": {
                "type": "string",
                "description": (
                    "The code to apply. Use '// ...existing code...' (or '# ...existing code...') "
                    "as markers for unchanged regions. The surrounding blocks serve as positional "
                    "anchors. With no markers, performs a fuzzy find-and-replace of the "
                    "best-matching region."
                ),
            },
            "explanation": {
                "type": "string",
                "description": "Brief description of what this edit does (logged, not applied).",
            },
        },
        "required": ["path", "code"],
        "additionalProperties": False,
    }

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        raw_path = str(arguments.get("path", "")).strip()
        code = str(arguments.get("code", ""))
        explanation = str(arguments.get("explanation", ""))
        path = resolve_path(context.working_directory, raw_path)
        old_content = path.read_text(encoding="utf-8") if path.exists() else ""
        diff = FileDiff(path=path, old_content=old_content, new_content=code)
        desc = explanation or f"Smart edit: {path}"
        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=desc,
            diff=diff,
            affected_paths=[path],
        )

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Missing required argument: path", is_error=True,
            )

        workspace = context.working_directory.resolve()
        path = resolve_path(workspace, raw_path)

        # Workspace boundary
        try:
            path.relative_to(workspace)
        except ValueError:
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to write outside the current workspace.", is_error=True,
            )

        # Refuse writes into .nexus managed state
        nexus_root = (workspace / ".nexus").resolve()
        try:
            path.relative_to(nexus_root)
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output="Refusing to write into .nexus managed state directory.", is_error=True,
            )
        except ValueError:
            pass

        code = str(arguments.get("code", ""))
        explanation = str(arguments.get("explanation", ""))

        # --- New file ---
        if not path.exists():
            ensure_parent(path)
            path.write_text(code, encoding="utf-8")
            lines = len(code.splitlines())
            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"Created {path.relative_to(workspace)} — {lines} lines",
                metadata={"path": str(path.relative_to(workspace)), "is_new_file": True},
            )

        # --- Read existing file ---
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Read error: {exc}", is_error=True)

        file_lines = original.splitlines(keepends=True)
        segments = _split_on_markers(code)

        # ----------------------------------------------------------------
        # No markers — fuzzy find-and-replace of best-matching block
        # ----------------------------------------------------------------
        if len(segments) == 1:
            new_lines = code.splitlines(keepends=True)
            result = _fuzzy_replace_no_anchors(file_lines, new_lines)
            if result is None:
                # Append to end of file as fallback
                if not original.endswith("\n"):
                    file_lines.append("\n")
                result = file_lines + new_lines

            new_content = "".join(result)
            try:
                path.write_text(new_content, encoding="utf-8")
            except OSError as exc:
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

            return ToolResult(
                call_id=call_id, tool_name=self.name,
                output=f"Applied smart edit to {path.relative_to(workspace)} ({explanation or 'no description'})",
                metadata={"path": str(path.relative_to(workspace)), "strategy": "fuzzy_replace"},
            )

        # ----------------------------------------------------------------
        # Markers present — anchor-based replacement
        #
        # segments[0]  = top anchor (existing code shown for context)
        # segments[1:-1] = new content to insert between anchors
        # segments[-1] = bottom anchor (existing code shown for context)
        # ----------------------------------------------------------------
        top_seg = segments[0]
        bottom_seg = segments[-1]
        middle_segs = segments[1:-1]  # the actual NEW content

        top_lines = top_seg.splitlines(keepends=True) if top_seg.strip() else []
        bottom_lines = bottom_seg.splitlines(keepends=True) if bottom_seg.strip() else []
        new_middle_lines: list[str] = []
        for seg in middle_segs:
            new_middle_lines.extend(seg.splitlines(keepends=True))

        # --- Find top anchor ---
        if top_lines:
            top_idx = _find_block_in_lines(file_lines, top_lines)
            if top_idx == -1:
                return ToolResult(
                    call_id=call_id, tool_name=self.name,
                    output=(
                        "Top anchor block not found in file. "
                        "Ensure the first block in 'code' matches existing content closely."
                    ),
                    is_error=True,
                )
            end_of_top = top_idx + len(top_lines)
        else:
            # No top anchor — insert at the very beginning
            top_idx = 0
            end_of_top = 0

        # --- Find bottom anchor ---
        if bottom_lines:
            bottom_idx = _find_block_in_lines(file_lines, bottom_lines, start_from=end_of_top)
            if bottom_idx == -1:
                return ToolResult(
                    call_id=call_id, tool_name=self.name,
                    output=(
                        "Bottom anchor block not found in file after the top anchor. "
                        "Ensure the last block in 'code' matches existing content closely."
                    ),
                    is_error=True,
                )
            end_of_bottom = bottom_idx + len(bottom_lines)
        else:
            # No bottom anchor — insert directly after top anchor
            bottom_idx = end_of_top
            end_of_bottom = end_of_top

        # Build new file: preserve_before + top_anchor + new_middle + bottom_anchor + preserve_after
        new_file_lines = (
            file_lines[:top_idx]
            + top_lines
            + new_middle_lines
            + bottom_lines
            + file_lines[end_of_bottom:]
        )
        new_content = "".join(new_file_lines)

        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

        rel = path.relative_to(workspace)
        lines_before = len(file_lines)
        lines_after = len(new_file_lines)
        return ToolResult(
            call_id=call_id,
            tool_name=self.name,
            output=(
                f"Applied edit to {rel} "
                f"({lines_before} → {lines_after} lines). "
                f"{explanation or ''}"
            ).strip(),
            metadata={
                "path": str(rel),
                "strategy": "anchor_replace",
                "lines_before": lines_before,
                "lines_after": lines_after,
                "top_anchor_line": top_idx + 1 if top_lines else None,
                "bottom_anchor_line": bottom_idx + 1 if bottom_lines else None,
            },
        )

