from __future__ import annotations

from nexus.models import Message, ToolCall
from nexus.runtime.sessions import SessionStore, new_snapshot, sanitize_session_messages


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


def test_session_round_trip_preserves_tool_call_metadata(tmp_path):
    store = SessionStore(tmp_path)
    snapshot = new_snapshot("tool-meta")
    snapshot.messages.extend(
        [
            Message(
                role="assistant",
                content="Creating file.",
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        tool_name="write_file",
                        arguments={"path": "hello.py", "content": "print('hi')\n"},
                    ),
                ),
            ),
            Message(
                role="tool",
                content="Created hello.py",
                name="write_file",
                tool_call_id="call-1",
            ),
        ]
    )

    store.save(snapshot)
    loaded = store.load("tool-meta")

    assert loaded.messages[0].tool_calls[0].call_id == "call-1"
    assert loaded.messages[0].tool_calls[0].tool_name == "write_file"
    assert loaded.messages[0].tool_calls[0].arguments["path"] == "hello.py"
    assert loaded.messages[1].tool_call_id == "call-1"


def test_sanitize_session_messages_drops_invalid_legacy_entries():
    messages = [
        Message(role="user", content="write a file"),
        Message(role="assistant", content="", tool_calls=()),
        Message(role="tool", content="Created hello.py", name="write_file", tool_call_id=None),
        Message(
            role="assistant",
            content="",
            tool_calls=(
                __import__("nexus.models", fromlist=["ToolCall"]).ToolCall(
                    call_id="call-1",
                    tool_name="write_file",
                    arguments={"path": "hello.py"},
                ),
            ),
        ),
        Message(role="tool", content="Created hello.py", name="write_file", tool_call_id="call-1"),
    ]

    sanitized = sanitize_session_messages(messages)

    assert [message.role for message in sanitized] == ["user", "assistant", "tool"]
    assert sanitized[1].tool_calls[0].call_id == "call-1"


def test_session_store_load_sanitizes_invalid_legacy_entries(tmp_path):
    session_path = tmp_path / "legacy.json"
    session_path.write_text(
        """
{
  "session_id": "legacy",
  "created_at": "2026-05-12T00:00:00+00:00",
  "updated_at": "2026-05-12T00:00:00+00:00",
  "messages": [
    {"role": "user", "content": "write file", "name": null, "tool_calls": [], "tool_call_id": null},
    {"role": "assistant", "content": "", "name": null, "tool_calls": [], "tool_call_id": null},
    {"role": "tool", "content": "Created hello.py", "name": "write_file", "tool_calls": [], "tool_call_id": null}
  ],
  "metadata": {},
  "summary": ""
}
""".strip(),
        encoding="utf-8",
    )
    store = SessionStore(tmp_path)

    loaded = store.load("legacy")

    assert len(loaded.messages) == 1
    assert loaded.messages[0].role == "user"


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
