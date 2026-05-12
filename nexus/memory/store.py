from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


_MEMORY_FILE = "user_memory.json"
_LEGACY_MEMORY_FILE = "agent_memory.json"


@dataclass(slots=True, frozen=True)
class MemoryEntry:
    key: str
    content: str
    keywords: tuple[str, ...] = field(default_factory=tuple)


class MemoryStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._memory_path = self.root / _MEMORY_FILE
        self._legacy_memory_path = self.root / _LEGACY_MEMORY_FILE

    def save(self, entry: MemoryEntry) -> None:
        data = self._load_data()
        data["entries"][entry.key] = entry.content.strip()
        self._save_data(data)

    def load(self, key: str) -> MemoryEntry | None:
        entries = self._load_data()["entries"]
        if key not in entries:
            return None
        return MemoryEntry(key=key, content=entries[key], keywords=(key,))

    def list_keys(self) -> list[str]:
        return sorted(self._load_data()["entries"])

    def search(self, query: str) -> list[MemoryEntry]:
        lowered = query.lower()
        matches: list[MemoryEntry] = []
        for key, content in self._load_data()["entries"].items():
            entry = MemoryEntry(key=key, content=content, keywords=(key,))
            searchable = f"{entry.key} {entry.content} {' '.join(entry.keywords)}".lower()
            if lowered in searchable:
                matches.append(entry)
        return matches

    def delete(self, key: str) -> bool:
        data = self._load_data()
        entries = data["entries"]
        if key not in entries:
            return False
        del entries[key]
        self._save_data(data)
        return True

    def clear(self) -> int:
        data = self._load_data()
        count = len(data["entries"])
        data["entries"] = {}
        self._save_data(data)
        return count

    def _load_data(self) -> dict[str, dict[str, str]]:
        for path in (self._memory_path, self._legacy_memory_path):
            data = self._load_json(path)
            if data is not None:
                if path != self._memory_path:
                    self._save_data(data)
                return data

        imported_entries = self._load_markdown_entries()
        if imported_entries:
            data = {"entries": imported_entries}
            self._save_data(data)
            return data
        return {"entries": {}}

    def _save_data(self, data: dict[str, dict[str, str]]) -> None:
        normalised = {
            "entries": {
                str(key): str(value)
                for key, value in data.get("entries", {}).items()
            }
        }
        self._memory_path.write_text(
            json.dumps(normalised, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load_json(self, path: Path) -> dict[str, dict[str, str]] | None:
        if not path.exists():
            return None
        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"entries": {}}
        if not isinstance(raw_data, dict):
            return {"entries": {}}
        entries = raw_data.get("entries") if isinstance(raw_data.get("entries"), dict) else raw_data
        if not isinstance(entries, dict):
            return {"entries": {}}
        return {
            "entries": {
                str(key): str(value)
                for key, value in entries.items()
            }
        }

    def _load_markdown_entries(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        for path in sorted(self.root.glob("*.md")):
            entry = self._parse(path)
            entries[entry.key] = entry.content
        return entries

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
