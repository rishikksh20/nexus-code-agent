from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True, frozen=True)
class MemoryEntry:
    key: str
    content: str
    keywords: tuple[str, ...] = field(default_factory=tuple)


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, entry: MemoryEntry) -> None:
        path = self.root / f"{entry.key}.md"
        keywords = ", ".join(entry.keywords)
        body = f"# {entry.key}\n\n{entry.content.strip()}\n\nKeywords: {keywords}\n"
        path.write_text(body, encoding="utf-8")

    def load(self, key: str) -> MemoryEntry | None:
        path = self.root / f"{key}.md"
        if not path.exists():
            return None
        return self._parse(path)

    def list_keys(self) -> list[str]:
        return sorted(path.stem for path in self.root.glob("*.md"))

    def search(self, query: str) -> list[MemoryEntry]:
        lowered = query.lower()
        matches: list[MemoryEntry] = []
        for path in self.root.glob("*.md"):
            entry = self._parse(path)
            searchable = f"{entry.key} {entry.content} {' '.join(entry.keywords)}".lower()
            if lowered in searchable:
                matches.append(entry)
        return matches

    def delete(self, key: str) -> bool:
        path = self.root / f"{key}.md"
        if not path.exists():
            return False
        path.unlink()
        return True

    def _parse(self, path: Path) -> MemoryEntry:
        lines = path.read_text(encoding="utf-8").splitlines()
        keywords: tuple[str, ...] = ()
        content_lines: list[str] = []
        for line in lines[1:]:
            if line.startswith("Keywords:"):
                raw = line.removeprefix("Keywords:").strip()
                keywords = tuple(part.strip() for part in raw.split(",") if part.strip())
                continue
            if line:
                content_lines.append(line)
        return MemoryEntry(key=path.stem, content="\n".join(content_lines).strip(), keywords=keywords)
