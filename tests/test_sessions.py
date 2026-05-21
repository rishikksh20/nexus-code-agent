from __future__ import annotations

from nexus.models import AgentEvent, AgentEventType, Message, ToolCall, ToolResult
from nexus.runtime.runtime_session import resolve_runtime_session
from nexus.runtime.session_checkpoints import (
    checkpoint_snapshots_for_paths,
    create_checkpoint_from_events,
    list_checkpoints,
    rewind_to_checkpoint,
)
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


def test_resolve_runtime_session_does_not_resume_latest_by_default(tmp_path):
    store = SessionStore(tmp_path)
    snapshot = new_snapshot("latest")
    snapshot.messages.append(Message(role="user", content="previous task"))
    store.save(snapshot)

    resolved, resumed = resolve_runtime_session(None, store, persist_sessions=True)

    assert resumed is False
    assert resolved.session_id != "latest"
    assert resolved.messages == []


def test_resolve_runtime_session_resumes_latest_when_opted_in(tmp_path):
    store = SessionStore(tmp_path)
    snapshot = new_snapshot("latest")
    snapshot.messages.append(Message(role="user", content="previous task"))
    store.save(snapshot)

    resolved, resumed = resolve_runtime_session(None, store, persist_sessions=True, resume_latest=True)

    assert resumed is True
    assert resolved.session_id == "latest"
    assert resolved.messages[0].content == "previous task"


def test_session_checkpoint_metadata_round_trips(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "hello.txt"
    target.write_text("hello\n", encoding="utf-8")
    snapshot = new_snapshot("checkpoint")
    snapshot.messages.append(Message(role="user", content="write hello"))

    checkpoint = create_checkpoint_from_events(
        snapshot,
        [
            AgentEvent(
                kind=AgentEventType.TOOL_RESULT,
                payload=ToolResult(
                    call_id="call-1",
                    tool_name="write_file",
                    output="Updated hello.txt",
                    metadata={"affected_paths": ["hello.txt"]},
                ),
            ),
            AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="stop"),
        ],
        workspace=workspace,
        turn_id="turn-1",
    )
    assert checkpoint is not None

    store = SessionStore(tmp_path / "sessions")
    store.save(snapshot)
    loaded = store.load("checkpoint")

    checkpoints = list_checkpoints(loaded)
    assert len(checkpoints) == 1
    assert checkpoints[0]["turn_id"] == "turn-1"
    assert checkpoints[0]["files"][0]["path"] == "hello.txt"
    assert checkpoints[0]["files"][0]["content"] is not None


def test_session_checkpoints_keep_last_ten(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = new_snapshot("checkpoint-retention")
    events = [AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="stop")]

    for index in range(11):
        snapshot.messages.append(Message(role="user", content=f"turn {index}"))
        create_checkpoint_from_events(
            snapshot,
            events,
            workspace=workspace,
            turn_id=f"turn-{index}",
        )

    checkpoints = list_checkpoints(snapshot)
    assert len(checkpoints) == 10
    assert checkpoints[0]["turn_id"] == "turn-1"
    assert checkpoints[-1]["turn_id"] == "turn-10"


def test_session_rewind_restores_file_and_deletes_later_created_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "hello.txt"
    later = workspace / "later.txt"
    snapshot = new_snapshot("checkpoint-rewind")

    target.write_text("one\n", encoding="utf-8")
    snapshot.messages.append(Message(role="user", content="first"))
    create_checkpoint_from_events(
        snapshot,
        [
            AgentEvent(
                kind=AgentEventType.TOOL_RESULT,
                payload=ToolResult(
                    call_id="call-1",
                    tool_name="write_file",
                    output="Updated hello.txt",
                    metadata={"affected_paths": ["hello.txt"]},
                ),
            ),
            AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="stop"),
        ],
        workspace=workspace,
        turn_id="turn-1",
    )
    first_checkpoint = list_checkpoints(snapshot)[0]["id"]

    pre_snapshots = checkpoint_snapshots_for_paths(workspace, {"hello.txt", "later.txt"})
    target.write_text("two\n", encoding="utf-8")
    later.write_text("created later\n", encoding="utf-8")
    snapshot.messages.append(Message(role="user", content="second"))
    create_checkpoint_from_events(
        snapshot,
        [
            AgentEvent(
                kind=AgentEventType.TOOL_RESULT,
                payload=ToolResult(
                    call_id="call-2",
                    tool_name="write_file",
                    output="Updated files",
                    metadata={
                        "affected_paths": ["hello.txt", "later.txt"],
                        "checkpoint_pre_snapshots": pre_snapshots,
                    },
                ),
            ),
            AgentEvent(kind=AgentEventType.TURN_COMPLETED, payload="stop"),
        ],
        workspace=workspace,
        turn_id="turn-2",
    )

    result = rewind_to_checkpoint(snapshot, first_checkpoint, workspace=workspace)

    assert result.errors == []
    assert target.read_text(encoding="utf-8") == "one\n"
    assert not later.exists()
    assert [message.content for message in snapshot.messages] == ["first"]
    assert len(list_checkpoints(snapshot)) == 1
