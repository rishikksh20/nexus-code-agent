"""Process-local Python workspace indexing helpers."""
from __future__ import annotations

import ast
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from nexus.tools.utils import read_path_policy_error, root_walk


PYTHON_GLOB_SUFFIX = ".py"
SKIP_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".nexus",
    "reference_code",
    "workspace",
})
MAX_PYTHON_FILES = 2_000
MAX_WORKSPACE_INDEXES = 8
MAX_DOCUMENT_CACHE_ENTRIES = 512


@dataclass(frozen=True)
class IndexedSymbol:
    name: str
    kind: str
    path: Path
    line: int
    character: int
    signature: str = ""
    docstring: str = ""
    syntax_kind: str = ""


@dataclass(frozen=True)
class IndexedPythonFile:
    path: Path
    relative_path: str
    fingerprint: tuple[int, int]
    symbols: tuple[IndexedSymbol, ...]
    imports: tuple[str, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class PythonWorkspaceIndex:
    root: Path
    allow_hidden: bool
    max_files: int
    files: tuple[IndexedPythonFile, ...]
    fingerprint: tuple[tuple[str, int, int], ...]

    @property
    def symbols(self) -> tuple[IndexedSymbol, ...]:
        return tuple(symbol for file_index in self.files for symbol in file_index.symbols)


_WORKSPACE_INDEX_CACHE: OrderedDict[tuple[str, bool, int], PythonWorkspaceIndex] = OrderedDict()
_DOCUMENT_CACHE: OrderedDict[str, IndexedPythonFile] = OrderedDict()
_CACHE_LOCK = RLock()


def get_python_workspace_index(
    root: Path,
    *,
    allow_hidden: bool = False,
    max_files: int = MAX_PYTHON_FILES,
) -> PythonWorkspaceIndex:
    """Return a cached Python index for *root*, invalidated by file mtimes and sizes."""
    resolved_root = root.resolve()
    bounded_max_files = max(1, int(max_files or MAX_PYTHON_FILES))
    paths = iter_python_files(resolved_root, allow_hidden=allow_hidden, max_files=bounded_max_files)
    fingerprint = _workspace_fingerprint(resolved_root, paths)
    cache_key = (str(resolved_root), bool(allow_hidden), bounded_max_files)

    with _CACHE_LOCK:
        cached = _WORKSPACE_INDEX_CACHE.get(cache_key)
        if cached is not None and cached.fingerprint == fingerprint:
            _WORKSPACE_INDEX_CACHE.move_to_end(cache_key)
            return cached
        previous_by_path = {file_index.path: file_index for file_index in cached.files} if cached else {}

    files: list[IndexedPythonFile] = []
    for path in paths:
        stat_fingerprint = _fingerprint_for_path(path)
        if stat_fingerprint is None:
            continue
        previous = previous_by_path.get(path)
        if previous is not None and previous.fingerprint == stat_fingerprint:
            files.append(previous)
            continue
        try:
            files.append(index_python_file(path, root=resolved_root, fingerprint=stat_fingerprint))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue

    index = PythonWorkspaceIndex(
        root=resolved_root,
        allow_hidden=allow_hidden,
        max_files=bounded_max_files,
        files=tuple(files),
        fingerprint=fingerprint,
    )
    with _CACHE_LOCK:
        _WORKSPACE_INDEX_CACHE[cache_key] = index
        _WORKSPACE_INDEX_CACHE.move_to_end(cache_key)
        while len(_WORKSPACE_INDEX_CACHE) > MAX_WORKSPACE_INDEXES:
            _WORKSPACE_INDEX_CACHE.popitem(last=False)
    return index


def get_document_index(path: Path) -> IndexedPythonFile:
    """Return a cached parse for one Python source file."""
    resolved_path = path.resolve()
    fingerprint = _fingerprint_for_path(resolved_path)
    if fingerprint is None:
        raise FileNotFoundError(str(path))
    cache_key = str(resolved_path)
    with _CACHE_LOCK:
        cached = _DOCUMENT_CACHE.get(cache_key)
        if cached is not None and cached.fingerprint == fingerprint:
            _DOCUMENT_CACHE.move_to_end(cache_key)
            return cached
    indexed = index_python_file(resolved_path, root=resolved_path.parent, fingerprint=fingerprint)
    with _CACHE_LOCK:
        _DOCUMENT_CACHE[cache_key] = indexed
        _DOCUMENT_CACHE.move_to_end(cache_key)
        while len(_DOCUMENT_CACHE) > MAX_DOCUMENT_CACHE_ENTRIES:
            _DOCUMENT_CACHE.popitem(last=False)
    return indexed


def index_python_file(
    path: Path,
    *,
    root: Path,
    fingerprint: tuple[int, int] | None = None,
) -> IndexedPythonFile:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    symbols: list[IndexedSymbol] = []
    imports: list[str] = []
    _collect_symbols(tree, path, symbols, parent=None)
    _collect_imports(tree, imports)
    return IndexedPythonFile(
        path=path,
        relative_path=_display_path(path, root),
        fingerprint=fingerprint or _fingerprint_for_path(path) or (0, 0),
        symbols=tuple(symbols),
        imports=tuple(sorted(set(imports))),
        lines=tuple(text.splitlines()),
    )


def iter_python_files(
    root: Path,
    *,
    allow_hidden: bool = False,
    max_files: int = MAX_PYTHON_FILES,
) -> tuple[Path, ...]:
    """Return Python source files in stable workspace order."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in root_walk(root):
        current_dir = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS
            and read_path_policy_error(current_dir / name, root, allow_hidden=allow_hidden) is None
        ]
        for filename in filenames:
            if not filename.endswith(PYTHON_GLOB_SUFFIX):
                continue
            path = current_dir / filename
            if read_path_policy_error(path, root, allow_hidden=allow_hidden) is not None:
                continue
            if path.is_file():
                files.append(path)
                if len(files) >= max_files:
                    return tuple(sorted(files))
    return tuple(sorted(files))


def _workspace_fingerprint(root: Path, paths: tuple[Path, ...]) -> tuple[tuple[str, int, int], ...]:
    fingerprints: list[tuple[str, int, int]] = []
    for path in paths:
        stat_fingerprint = _fingerprint_for_path(path)
        if stat_fingerprint is None:
            continue
        fingerprints.append((_display_path(path, root), *stat_fingerprint))
    return tuple(fingerprints)


def _fingerprint_for_path(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _collect_symbols(
    node: ast.AST,
    path: Path,
    bucket: list[IndexedSymbol],
    *,
    parent: str | None,
) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{parent}.{child.name}" if parent else child.name
            bucket.append(
                IndexedSymbol(
                    name=name,
                    kind="function",
                    path=path,
                    line=child.lineno,
                    character=child.col_offset + 1,
                    signature=_function_signature(child),
                    docstring=ast.get_docstring(child) or "",
                    syntax_kind=child.__class__.__name__,
                )
            )
            _collect_symbols(child, path, bucket, parent=name)
        elif isinstance(child, ast.ClassDef):
            name = f"{parent}.{child.name}" if parent else child.name
            bucket.append(
                IndexedSymbol(
                    name=name,
                    kind="class",
                    path=path,
                    line=child.lineno,
                    character=child.col_offset + 1,
                    signature=f"class {child.name}",
                    docstring=ast.get_docstring(child) or "",
                    syntax_kind="ClassDef",
                )
            )
            _collect_symbols(child, path, bucket, parent=name)
        elif isinstance(child, ast.Assign):
            for target in child.targets:
                _collect_assignment_target(target, path, bucket, parent=parent)
            _collect_symbols(child, path, bucket, parent=parent)
        elif isinstance(child, ast.AnnAssign):
            _collect_assignment_target(child.target, path, bucket, parent=parent)
            _collect_symbols(child, path, bucket, parent=parent)
        else:
            _collect_symbols(child, path, bucket, parent=parent)


def _collect_assignment_target(
    target: ast.AST,
    path: Path,
    bucket: list[IndexedSymbol],
    *,
    parent: str | None,
) -> None:
    if not isinstance(target, ast.Name):
        return
    name = f"{parent}.{target.id}" if parent else target.id
    bucket.append(
        IndexedSymbol(
            name=name,
            kind="variable",
            path=path,
            line=target.lineno,
            character=target.col_offset + 1,
            signature=f"{target.id} = ...",
        )
    )


def _collect_imports(tree: ast.AST, imports: list[str]) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.extend(f"{module}.{alias.name}".strip(".") for alias in node.names)


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [arg.arg for arg in node.args.posonlyargs + node.args.args]
    if node.args.vararg is not None:
        args.append(f"*{node.args.vararg.arg}")
    args.extend(arg.arg for arg in node.args.kwonlyargs)
    if node.args.kwarg is not None:
        args.append(f"**{node.args.kwarg.arg}")
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(args)})"


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def cache_snapshot() -> dict[str, int]:
    """Return cache sizes for diagnostics and tests."""
    with _CACHE_LOCK:
        return {
            "workspace_indexes": len(_WORKSPACE_INDEX_CACHE),
            "documents": len(_DOCUMENT_CACHE),
        }
