"""Shared path, binary-detection, and text utilities for builtin tools.

These helpers are intentionally dependency-free (stdlib only) so they can be
imported by any tool without pulling in optional packages.
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git", ".venv", "venv", ".hg", ".svn"})

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


def walk_text_files(root: Path, max_files: int = 500) -> list[Path]:
    """Walk *root* recursively, returning up to *max_files* non-binary files."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in root_walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if name.startswith("."):
                continue
            fp = Path(dirpath) / name
            if not is_binary_file(fp):
                found.append(fp)
                if len(found) >= max_files:
                    return found
    return found


def root_walk(root: Path):
    """Thin wrapper around ``os.walk`` that yields ``(dirpath, dirnames, filenames)``."""
    import os
    yield from os.walk(root)


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
