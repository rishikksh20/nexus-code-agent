"""Shared path, binary-detection, and text utilities for builtin tools.

These helpers are intentionally dependency-free (stdlib only) so they can be
imported by any tool without pulling in optional packages.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git", ".venv", "venv", ".hg", ".svn", ".nexus"})
_READABLE_HIDDEN_SUBTREES = ((".agents", "skills"), (".agents", "tools"))

BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".ico",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a",
    ".pyc", ".pyo", ".whl", ".egg",
    ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mov", ".mkv",
    ".db", ".sqlite", ".sqlite3",
})


def resolve_path(cwd: Path, raw: str) -> Path:
    """Resolve *raw* relative to *cwd*, returning an absolute :class:`Path`."""
    p = Path(raw)
    return (cwd / p).resolve() if not p.is_absolute() else p.resolve()


def is_binary_file(path: Path) -> bool:
    """Return ``True`` if *path* is almost certainly a binary file.

    Uses a two-stage heuristic: extension lookup first, then a byte-scan of
    the first 8 KB.
    """
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return True
    try:
        sample = path.read_bytes()[:8192]
    except OSError:
        return False
    # Null bytes are a reliable binary indicator
    return b"\x00" in sample


def ensure_parent(path: Path) -> None:
    """Create all parent directories of *path* if they do not exist."""
    path.parent.mkdir(parents=True, exist_ok=True)


def allow_hidden_reads(metadata: dict[str, object] | None) -> bool:
    """Return ``True`` when hidden/private read access was explicitly enabled."""
    if not metadata:
        return False
    return bool(metadata.get("allow_hidden_paths", False))


def read_path_policy_error(
    target: Path,
    workspace_root: Path,
    *,
    allow_hidden: bool = False,
) -> str | None:
    """Return a human-readable denial reason for blocked read targets, if any."""
    try:
        relative = target.relative_to(workspace_root)
    except ValueError:
        return None
    restricted_part = next(
        _restricted_path_part(_visible_read_parts(relative.parts), allow_hidden=allow_hidden),
        None,
    )
    if restricted_part is None:
        return None
    if restricted_part == ".nexus":
        return "Refusing to read Nexus-managed .nexus state."
    return (
        "Refusing to read hidden/private paths unless allow_hidden_paths is enabled. "
        f"Blocked path component: {restricted_part}"
    )


def include_directory_entry(name: str, *, show_hidden: bool, allow_hidden: bool) -> bool:
    """Return whether a directory entry should be shown in list-style output."""
    if name == ".nexus":
        return False
    if not is_hidden_or_private_name(name):
        return True
    return show_hidden and allow_hidden


def can_read_match(path: Path, workspace_root: Path, *, allow_hidden: bool = False) -> bool:
    """Return whether *path* is visible to read-only discovery tools."""
    return read_path_policy_error(path, workspace_root, allow_hidden=allow_hidden) is None


def walk_text_files(root: Path, *, allow_hidden: bool = False, max_files: int = 500) -> list[Path]:
    """Walk *root* recursively, returning up to *max_files* non-binary files."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in root_walk(root):
        current_dir = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if _include_walk_entry(current_dir / d, root, allow_hidden=allow_hidden, is_dir=True)
        ]
        for name in filenames:
            fp = current_dir / name
            if not _include_walk_entry(fp, root, allow_hidden=allow_hidden, is_dir=False):
                continue
            if not is_binary_file(fp):
                found.append(fp)
                if len(found) >= max_files:
                    return found
    return found


def root_walk(root: Path):
    """Thin wrapper around ``os.walk`` that yields ``(dirpath, dirnames, filenames)``."""
    import os
    yield from os.walk(root)


def is_hidden_or_private_name(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    return name.startswith(".") or name.casefold().startswith("private")


def _restricted_path_part(parts: Iterable[str], *, allow_hidden: bool) -> Iterable[str]:
    for part in parts:
        if part in {"", "."}:
            continue
        if part == ".nexus":
            yield part
            continue
        if not allow_hidden and is_hidden_or_private_name(part):
            yield part


def _visible_read_parts(parts: tuple[str, ...]) -> tuple[str, ...]:
    for subtree in _READABLE_HIDDEN_SUBTREES:
        if parts[: len(subtree)] == subtree:
            return parts[len(subtree) :]
    return parts


def _is_readable_hidden_subtree_parent(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(subtree[: len(parts)] == parts for subtree in _READABLE_HIDDEN_SUBTREES)


def _include_walk_entry(path: Path, root: Path, *, allow_hidden: bool, is_dir: bool) -> bool:
    name = path.name
    if name in _SKIP_DIRS:
        return False
    if is_dir and _is_readable_hidden_subtree_parent(path, root):
        return True
    if not can_read_match(path, root, allow_hidden=allow_hidden):
        return False
    if is_dir:
        return True
    return True


# ---------------------------------------------------------------------------
# Token / text helpers
# ---------------------------------------------------------------------------

# Rough approximation: 1 token ≈ 4 characters (adequate for budget checks)
_CHARS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    """Estimate the number of tokens in *text* (rough heuristic)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def truncate_text(text: str, max_tokens: int, *, suffix: str = "\n... [truncated]") -> str:
    """Truncate *text* to approximately *max_tokens* tokens."""
    max_chars = max_tokens * _CHARS_PER_TOKEN
    if len(text) <= max_chars:
        return text
    cut = max_chars - len(suffix)
    return text[:cut] + suffix
