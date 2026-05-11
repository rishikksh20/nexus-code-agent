from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from nexus.models import Message


@dataclass(slots=True)
class SessionSnapshot:
    session_id: str
    created_at: str
    updated_at: str
    messages: list[Message] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [
                {"role": message.role, "content": message.content, "name": message.name}
                for message in self.messages
            ],
            "metadata": self.metadata,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SessionSnapshot":
        raw_messages = data.get("messages", [])
        messages = [Message(**item) for item in raw_messages]  # type: ignore[arg-type]
        return cls(
            session_id=str(data["session_id"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            messages=messages,
            metadata=dict(data.get("metadata", {})),  # type: ignore[arg-type]
            summary=str(data.get("summary", "")),
        )


def new_snapshot(session_id: str | None = None) -> SessionSnapshot:
    now = datetime.now(UTC).isoformat()
    return SessionSnapshot(
        session_id=session_id or uuid4().hex[:12],
        created_at=now,
        updated_at=now,
    )


class SessionStore:
    def __init__(self, root: Path, *, max_sessions_retained: int | None = None) -> None:
        self.root = root
        self.max_sessions_retained = max_sessions_retained
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def save(self, snapshot: SessionSnapshot) -> None:
        snapshot.updated_at = datetime.now(UTC).isoformat()
        target = self._path(snapshot.session_id)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(target)
        (self.root / "latest_session.txt").write_text(snapshot.session_id, encoding="utf-8")
        self._prune_if_needed()

    def load(self, session_id: str) -> SessionSnapshot:
        payload = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        return SessionSnapshot.from_dict(payload)

    def list_sessions(self) -> list[SessionSnapshot]:
        snapshots = [
            SessionSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))
            for path in self.root.glob("*.json")
        ]
        return sorted(snapshots, key=lambda item: item.updated_at, reverse=True)

    def load_latest(self) -> SessionSnapshot | None:
        """Return the most recently saved session, or None if no sessions exist."""
        latest_file = self.root / "latest_session.txt"
        if not latest_file.exists():
            return None
        session_id = latest_file.read_text(encoding="utf-8").strip()
        try:
            return self.load(session_id)
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            return None

    def _prune_if_needed(self) -> None:
        if self.max_sessions_retained is None:
            return
        sessions = self.list_sessions()
        for snapshot in sessions[self.max_sessions_retained :]:
            path = self._path(snapshot.session_id)
            if path.exists():
                path.unlink()


class EphemeralSessionStore(SessionStore):
    def __init__(self) -> None:
        self.root = Path(".")
        self.max_sessions_retained = None

    def save(self, snapshot: SessionSnapshot) -> None:
        snapshot.updated_at = datetime.now(UTC).isoformat()

    def load(self, session_id: str) -> SessionSnapshot:
        return new_snapshot(session_id=session_id)

    def list_sessions(self) -> list[SessionSnapshot]:
        return []
