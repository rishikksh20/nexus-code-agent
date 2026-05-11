from __future__ import annotations

from nexus.models import Message
from nexus.runtime.sessions import SessionStore, new_snapshot


def test_session_round_trip(tmp_path):
    store = SessionStore(tmp_path)
    snapshot = new_snapshot("abc123")
    snapshot.messages.append(Message(role="user", content="hello"))
    snapshot.summary = "hello"

    store.save(snapshot)
    loaded = store.load("abc123")

    assert loaded.session_id == "abc123"
    assert loaded.messages[0].content == "hello"
    assert loaded.summary == "hello"


def test_session_store_prunes_older_sessions(tmp_path):
    store = SessionStore(tmp_path, max_sessions_retained=2)

    first = new_snapshot("s1")
    second = new_snapshot("s2")
    third = new_snapshot("s3")

    store.save(first)
    store.save(second)
    store.save(third)

    remaining = [path.stem for path in sorted(tmp_path.glob("*.json"))]

    assert remaining == ["s2", "s3"]


def test_session_store_writes_latest_session_pointer(tmp_path):
    store = SessionStore(tmp_path)
    snapshot = new_snapshot("latest")

    store.save(snapshot)

    assert (tmp_path / "latest_session.txt").read_text(encoding="utf-8") == "latest"
