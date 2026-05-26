"""EditTool — resilient surgical editor for existing files.

The tool is designed for agent use: it tries strict matching first, then a
small set of recovery strategies for common model drift such as indentation,
whitespace, and escaped newlines. It still applies edits conservatively using
precise byte ranges rather than full-file rewrites.
"""
from __future__ import annotations

import asyncio
import ast
import codecs
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from nexus.models import ToolExecutionContext, ToolResult
from nexus.tools.base import FileDiff, Tool, ToolConfirmation, ToolKind
from nexus.tools.utils import ensure_parent, resolve_path


@dataclass(frozen=True)
class EditRequest:
    path: Path
    old_string: str
    new_string: str
    replace_all: bool = False
    replace_first: bool = False


@dataclass(frozen=True)
class MatchResult:
    matched_text: str
    start: int
    end: int
    strategy: str
    confidence: float


@dataclass(frozen=True)
class FileTextState:
    content: str
    newline: str
    has_utf8_bom: bool = False


@dataclass
class EditTransaction:
    original_content: str
    modified_content: str
    matches: list[MatchResult]
    diagnostics: list[str] = field(default_factory=list)


FILE_LOCKS: dict[str, asyncio.Lock] = {}
FILE_LOCKS_GUARD = asyncio.Lock()


async def _lock_for_path(path: Path) -> asyncio.Lock:
    key = str(path.resolve())
    async with FILE_LOCKS_GUARD:
        return FILE_LOCKS.setdefault(key, asyncio.Lock())


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _write_path_error(path: Path, workspace: Path) -> str | None:
    try:
        path.relative_to(workspace)
    except ValueError:
        return "Refusing to write outside the current workspace."

    nexus_root = (workspace / ".nexus").resolve()
    try:
        path.relative_to(nexus_root)
    except ValueError:
        return None
    return "Refusing to write into .nexus managed state directory."


def _parse_request(arguments: dict[str, Any], workspace: Path) -> tuple[EditRequest | None, str | None]:
    raw_path = str(arguments.get("path", "")).strip()
    if not raw_path:
        return None, "Missing required argument: path"

    path = resolve_path(workspace, raw_path)
    path_error = _write_path_error(path, workspace)
    if path_error is not None:
        return None, path_error

    old_string = _normalize_newlines(str(arguments.get("old_string", "")))
    new_string = _normalize_newlines(str(arguments.get("new_string", "")))
    replace_all = bool(arguments.get("replace_all", False))
    replace_first = bool(arguments.get("_replace_first", False))

    if old_string and old_string == new_string:
        return None, "old_string and new_string are identical. Use edit only when content changes."

    return (
        EditRequest(
            path=path,
            old_string=old_string,
            new_string=new_string,
            replace_all=replace_all,
            replace_first=replace_first,
        ),
        None,
    )


def _load_file_state(path: Path) -> FileTextState:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("Binary files are not supported by the edit tool.")
    has_utf8_bom = raw.startswith(codecs.BOM_UTF8)
    decoded = raw.decode("utf-8-sig")
    return FileTextState(
        content=_normalize_newlines(decoded),
        newline=_detect_newline(decoded),
        has_utf8_bom=has_utf8_bom,
    )


def _serialize_content(state: FileTextState, content: str) -> bytes:
    rendered = content if state.newline == "\n" else content.replace("\n", state.newline)
    payload = rendered.encode("utf-8")
    if state.has_utf8_bom:
        return codecs.BOM_UTF8 + payload
    return payload


def _line_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        spans.append((offset, offset + len(line), line))
        offset += len(line)
    if not spans and text:
        spans.append((0, len(text), text))
    return spans


def _iter_windows(text: str, minimum_lines: int, maximum_lines: int) -> list[tuple[int, int, str]]:
    spans = _line_spans(text)
    if not spans:
        return []

    windows: list[tuple[int, int, str]] = []
    for window_size in range(max(1, minimum_lines), max(1, maximum_lines) + 1):
        if window_size > len(spans):
            continue
        for start_index in range(0, len(spans) - window_size + 1):
            start = spans[start_index][0]
            end = spans[start_index + window_size - 1][1]
            windows.append((start, end, text[start:end]))
    return windows


def _dedupe_matches(matches: list[MatchResult]) -> list[MatchResult]:
    deduped: dict[tuple[int, int], MatchResult] = {}
    for match in matches:
        key = (match.start, match.end)
        existing = deduped.get(key)
        if existing is None or match.confidence > existing.confidence:
            deduped[key] = match
    return sorted(deduped.values(), key=lambda match: (match.start, match.end))


def _common_indent(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    indents = [re.match(r"[ \t]*", line).group(0) for line in lines]
    return min(indents, key=len) if indents else ""


def _strip_common_indent(text: str) -> str:
    indent = _common_indent(text)
    if not indent:
        return text
    stripped: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.strip() and line.startswith(indent):
            stripped.append(line[len(indent) :])
        else:
            stripped.append(line)
    return "".join(stripped)


def _indent_block(text: str, indent: str) -> str:
    indented: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.strip():
            indented.append(f"{indent}{line}")
        else:
            indented.append(line)
    return "".join(indented)


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _unescape_common(text: str) -> str:
    if "\\" not in text:
        return text
    unescaped = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    return _normalize_newlines(unescaped)


def _exact_matches(content: str, old_string: str) -> list[MatchResult]:
    matches: list[MatchResult] = []
    start = 0
    while True:
        index = content.find(old_string, start)
        if index < 0:
            break
        matches.append(
            MatchResult(
                matched_text=content[index : index + len(old_string)],
                start=index,
                end=index + len(old_string),
                strategy="exact",
                confidence=1.0,
            )
        )
        start = index + max(1, len(old_string))
    return matches


def _trimmed_line_matches(content: str, old_string: str) -> list[MatchResult]:
    old_lines = old_string.splitlines(keepends=True) or [old_string]
    windows = _iter_windows(content, len(old_lines), len(old_lines))
    matches: list[MatchResult] = []
    for start, end, candidate in windows:
        candidate_lines = candidate.splitlines(keepends=True) or [candidate]
        if len(candidate_lines) != len(old_lines):
            continue
        if all(left.strip() == right.strip() for left, right in zip(candidate_lines, old_lines, strict=False)):
            matches.append(
                MatchResult(candidate, start, end, strategy="trimmed_lines", confidence=0.96)
            )
    return matches


def _whitespace_normalized_matches(content: str, old_string: str) -> list[MatchResult]:
    old_lines = old_string.splitlines(keepends=True) or [old_string]
    normalized_old = _collapse_whitespace(old_string)
    windows = _iter_windows(content, max(1, len(old_lines) - 1), len(old_lines) + 1)
    matches: list[MatchResult] = []
    for start, end, candidate in windows:
        if _collapse_whitespace(candidate) == normalized_old:
            matches.append(
                MatchResult(candidate, start, end, strategy="whitespace_normalized", confidence=0.93)
            )
    return matches


def _indentation_flexible_matches(content: str, old_string: str) -> list[MatchResult]:
    old_lines = old_string.splitlines(keepends=True) or [old_string]
    normalized_old = _strip_common_indent(old_string).strip("\n")
    windows = _iter_windows(content, len(old_lines), len(old_lines))
    matches: list[MatchResult] = []
    for start, end, candidate in windows:
        if _strip_common_indent(candidate).strip("\n") == normalized_old:
            matches.append(
                MatchResult(candidate, start, end, strategy="indentation_flexible", confidence=0.9)
            )
    return matches


def _escape_normalized_matches(content: str, old_string: str) -> list[MatchResult]:
    unescaped = _unescape_common(old_string)
    if unescaped == old_string:
        return []
    return [
        MatchResult(match.matched_text, match.start, match.end, "escape_normalized", 0.88)
        for match in _exact_matches(content, unescaped)
    ]


def _context_anchor_matches(content: str, old_string: str) -> list[MatchResult]:
    old_lines = old_string.splitlines()
    anchor_lines = [line.strip() for line in old_lines if line.strip()]
    if len(anchor_lines) < 2:
        return []

    first_anchor = anchor_lines[0]
    last_anchor = anchor_lines[-1]
    target_line_count = max(1, len(old_lines))
    matches: list[MatchResult] = []
    for start, end, candidate in _iter_windows(content, max(1, target_line_count - 1), target_line_count + 2):
        candidate_lines = candidate.splitlines()
        if not candidate_lines:
            continue
        if candidate_lines[0].strip() != first_anchor or candidate_lines[-1].strip() != last_anchor:
            continue
        score = SequenceMatcher(
            None,
            _collapse_whitespace(candidate),
            _collapse_whitespace(old_string),
        ).ratio()
        if score >= 0.72:
            matches.append(
                MatchResult(candidate, start, end, strategy="context_anchors", confidence=min(0.85, 0.74 + score / 10))
            )
    return matches


def _similarity_matches(content: str, old_string: str) -> list[MatchResult]:
    old_lines = old_string.splitlines(keepends=True) or [old_string]
    normalized_old = _collapse_whitespace(_strip_common_indent(old_string))
    matches: list[MatchResult] = []
    for start, end, candidate in _iter_windows(content, max(1, len(old_lines) - 1), len(old_lines) + 2):
        score = SequenceMatcher(
            None,
            _collapse_whitespace(_strip_common_indent(candidate)),
            normalized_old,
        ).ratio()
        if score >= 0.84:
            matches.append(
                MatchResult(candidate, start, end, strategy="similarity", confidence=min(0.79, score))
            )
    return matches


MATCHERS = (
    _exact_matches,
    _trimmed_line_matches,
    _whitespace_normalized_matches,
    _indentation_flexible_matches,
    _escape_normalized_matches,
    _context_anchor_matches,
    _similarity_matches,
)


def _resolve_matches(content: str, request: EditRequest) -> tuple[list[MatchResult] | None, str | None]:
    for matcher in MATCHERS:
        matches = _dedupe_matches(matcher(content, request.old_string))
        if not matches:
            continue

        if request.replace_all:
            strategy = matches[0].strategy
            if len(matches) > 1 and matches[0].confidence < 0.95:
                return (
                    None,
                    f"old_string matched {len(matches)} regions in {request.path} using {strategy}. "
                    "Provide more surrounding context instead of using replace_all for fuzzy matches.",
                )
            return matches, None

        if len(matches) == 1:
            return matches, None

        if request.replace_first:
            return [matches[0]], None

        return (
            None,
            f"old_string matched {len(matches)} regions in {request.path} using {matches[0].strategy}. "
            "Provide 3-8 surrounding lines so the target is unique, or set replace_all=true when every occurrence should change.",
        )

    return (
        None,
        f"old_string not found in {request.path}. The edit tool patches existing snippets, not whole files. "
        "Provide the current text from disk with a few surrounding lines and use write_file only for full rewrites.",
    )


def _prepare_replacement(request: EditRequest, match: MatchResult) -> str:
    replacement = request.new_string
    if match.strategy == "escape_normalized" and "\\n" in replacement and "\n" not in replacement:
        replacement = _unescape_common(replacement)
    if match.strategy in {"exact"}:
        return replacement

    requested_indent = _common_indent(request.old_string)
    matched_indent = _common_indent(match.matched_text)
    if matched_indent and matched_indent != requested_indent:
        replacement = _indent_block(_strip_common_indent(replacement), matched_indent)
    return replacement


def _python_diagnostics(path: Path, content: str) -> list[str]:
    if path.suffix != ".py":
        return []
    try:
        ast.parse(content)
    except SyntaxError as exc:
        location = f"line {exc.lineno}" if exc.lineno is not None else "unknown location"
        return [f"Python syntax check failed at {location}: {exc.msg}"]
    return []


def _build_transaction(state: FileTextState, request: EditRequest) -> tuple[EditTransaction | None, str | None]:
    if not request.old_string:
        return (
            None,
            "old_string is empty but the file already exists. Provide the current snippet to edit, or use write_file to overwrite the file.",
        )

    matches, error = _resolve_matches(state.content, request)
    if error is not None or matches is None:
        return None, error

    modified_content = state.content
    for match in sorted(matches, key=lambda item: item.start, reverse=True):
        replacement = _prepare_replacement(request, match)
        modified_content = modified_content[: match.start] + replacement + modified_content[match.end :]

    diagnostics = _python_diagnostics(request.path, modified_content)
    return (
        EditTransaction(
            original_content=state.content,
            modified_content=modified_content,
            matches=matches,
            diagnostics=diagnostics,
        ),
        None,
    )


class EditTool(Tool):
    """Patch an existing file snippet without rewriting the whole file.

    Required inputs are ``path``, ``old_string``, and ``new_string``.
    ``old_string`` should be copied from the current file contents and include
    enough surrounding context to identify one region. ``new_string`` replaces
    only that matched span. Use ``replace_all`` only when every occurrence of
    the same snippet should change. Use ``write_file`` for full-file rewrites.
    """

    name = "edit"
    description = (
        "Precisely patch part of an existing file. Required args: path, old_string, new_string. "
        "Copy old_string from the current file on disk and include 3-8 surrounding lines when needed so the match is unique. "
        "new_string replaces only the matched span, not the whole file. "
        "Use replace_all only when every occurrence of the same snippet should change. "
        "Use write_file for new files or intentional full-file rewrites."
    )
    kind = ToolKind.WRITE
    is_mutating = True
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "Target file path, relative to the workspace or absolute inside the workspace boundary.",
            },
            "old_string": {
                "type": "string",
                "description": "Current snippet exactly as it appears on disk. Include nearby context so it resolves to one region. Leave empty only when creating a brand-new file.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text for the matched region only, not the full file. Use an empty string to delete old_string.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Optional. Replace every safely resolved occurrence of old_string. Default false; prefer adding more context before enabling this.",
            },
        },
        "required": ["path", "old_string", "new_string"],
        "additionalProperties": False,
    }

    async def get_confirmation(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolConfirmation | None:
        workspace = context.working_directory.resolve()
        request, error = _parse_request(arguments, workspace)
        del call_id
        if error is not None or request is None:
            return None

        path = request.path
        is_new = not path.exists()

        if is_new:
            diff = FileDiff(path=path, old_content="", new_content=request.new_string, is_new_file=True)
            return ToolConfirmation(
                tool_name=self.name,
                params=arguments,
                description=f"Create new file: {path}",
                diff=diff,
                affected_paths=[path],
            )

        try:
            state = _load_file_state(path)
        except (OSError, UnicodeDecodeError, ValueError):
            return None

        transaction, build_error = _build_transaction(state, request)
        if build_error is not None or transaction is None:
            return None

        diff = FileDiff(path=path, old_content=state.content, new_content=transaction.modified_content)
        strategy = transaction.matches[0].strategy if transaction.matches else "unknown"
        return ToolConfirmation(
            tool_name=self.name,
            params=arguments,
            description=f"Edit file: {path} ({strategy})",
            diff=diff,
            affected_paths=[path],
        )

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        workspace = context.working_directory.resolve()
        request, error = _parse_request(arguments, workspace)
        if error is not None or request is None:
            return ToolResult(call_id=call_id, tool_name=self.name, output=error or "Invalid edit request.", is_error=True)

        path = request.path
        lock = await _lock_for_path(path)
        async with lock:
            # --- Create new file ---
            if not path.exists():
                if request.old_string:
                    return ToolResult(
                        call_id=call_id,
                        tool_name=self.name,
                        output=f"File does not exist: {path}. To create a new file, leave old_string empty.",
                        is_error=True,
                    )
                ensure_parent(path)
                try:
                    path.write_text(request.new_string, encoding="utf-8")
                except OSError as exc:
                    return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)
                return ToolResult(
                    call_id=call_id,
                    tool_name=self.name,
                    output=f"Created {path} — {len(request.new_string.splitlines())} lines",
                    metadata={"path": str(path), "is_new_file": True},
                )

            # --- Edit existing file ---
            try:
                state = _load_file_state(path)
            except OSError as exc:
                return ToolResult(
                    call_id=call_id,
                    tool_name=self.name,
                    output=f"Read error: {exc}",
                    is_error=True,
                )
            except UnicodeDecodeError:
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Read error: {path} is not valid UTF-8.", is_error=True)
            except ValueError as exc:
                return ToolResult(call_id=call_id, tool_name=self.name, output=str(exc), is_error=True)

            transaction, build_error = _build_transaction(state, request)
            if build_error is not None or transaction is None:
                return ToolResult(call_id=call_id, tool_name=self.name, output=build_error or "Unable to apply edit.", is_error=True)

            try:
                ensure_parent(path)
                path.write_bytes(_serialize_content(state, transaction.modified_content))
            except OSError as exc:
                return ToolResult(call_id=call_id, tool_name=self.name, output=f"Write error: {exc}", is_error=True)

            lead_match = transaction.matches[0]
            output = (
                f"Patched {len(transaction.matches)} region(s) in {path} "
                f"using {lead_match.strategy} matching (confidence {lead_match.confidence:.2f})."
            )
            if transaction.diagnostics:
                output = f"{output} Diagnostics: {'; '.join(transaction.diagnostics)}"

            return ToolResult(
                call_id=call_id,
                tool_name=self.name,
                output=output,
                metadata={
                    "path": str(path),
                    "occurrences_found": len(transaction.matches),
                    "occurrences_replaced": len(transaction.matches),
                    "match_strategy": lead_match.strategy,
                    "match_confidence": lead_match.confidence,
                    "diagnostics": transaction.diagnostics,
                },
            )
