from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from nexus.models import AgentEvent, AgentEventType, Message, ToolResult
from nexus.runtime.sessions import SessionSnapshot


CHECKPOINTS_KEY = "checkpoints"
MAX_CHECKPOINTS = 10


@dataclass(slots=True)
class RewindResult:
    checkpoint_id: str
    message_count: int
    restored_files: int
    errors: list[str]


def create_checkpoint_from_events(
    snapshot: SessionSnapshot,
    events: list[AgentEvent],
    *,
    workspace: Path,
    turn_id: str,
    limit: int = MAX_CHECKPOINTS,
) -> dict[str, Any] | None:
    if not any(event.kind == AgentEventType.TURN_COMPLETED for event in events):
        return None

    checkpoints = _checkpoint_list(snapshot.metadata)
    if checkpoints and checkpoints[-1].get("turn_id") == turn_id:
        return None

    affected_paths = _affected_paths_from_events(events)
    pre_snapshots = _pre_snapshots_from_events(events)
    _backfill_missing_snapshots(checkpoints, pre_snapshots)

    tracked_paths = _tracked_paths(checkpoints) | set(affected_paths) | set(pre_snapshots)
    files = [_read_file_snapshot(workspace, path) for path in sorted(tracked_paths)]
    checkpoint = {
        "id": f"chk-{uuid4().hex[:8]}",
        "created_at": datetime.now(UTC).isoformat(),
        "turn_id": turn_id,
        "message_count": len(snapshot.messages),
        "summary": _checkpoint_summary(snapshot.messages),
        "files": files,
    }
    checkpoints.append(checkpoint)
    if len(checkpoints) > limit:
        del checkpoints[:-limit]
    snapshot.metadata[CHECKPOINTS_KEY] = checkpoints
    return checkpoint


def list_checkpoints(snapshot: SessionSnapshot) -> list[dict[str, Any]]:
    return list(_checkpoint_list(snapshot.metadata))


def rewind_to_checkpoint(
    snapshot: SessionSnapshot,
    checkpoint_id: str,
    *,
    workspace: Path,
    restore_files: bool = True,
) -> RewindResult:
    checkpoints = _checkpoint_list(snapshot.metadata)
    index = next(
        (idx for idx, checkpoint in enumerate(checkpoints) if checkpoint.get("id") == checkpoint_id),
        -1,
    )
    if index < 0:
        raise ValueError(f"Checkpoint not found: {checkpoint_id}")

    checkpoint = checkpoints[index]
    message_count = int(checkpoint.get("message_count", 0) or 0)
    errors: list[str] = []
    restored_files = 0
    if restore_files:
        restored_files, errors = _restore_files(workspace, checkpoint)

    snapshot.messages = list(snapshot.messages[:message_count])
    snapshot.metadata[CHECKPOINTS_KEY] = checkpoints[: index + 1]
    snapshot.summary = _session_summary(snapshot.messages)
    return RewindResult(
        checkpoint_id=checkpoint_id,
        message_count=message_count,
        restored_files=restored_files,
        errors=errors,
    )


def checkpoint_snapshots_for_paths(workspace: Path, paths: set[str]) -> list[dict[str, Any]]:
    return [_read_file_snapshot(workspace, path) for path in sorted(paths)]


def _checkpoint_list(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    value = metadata.get(CHECKPOINTS_KEY)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _tracked_paths(checkpoints: list[dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for checkpoint in checkpoints:
        for file_snapshot in _file_snapshots(checkpoint):
            path = str(file_snapshot.get("path", "")).strip()
            if path:
                paths.add(path)
    return paths


def _affected_paths_from_events(events: list[AgentEvent]) -> list[str]:
    paths: list[str] = []
    for result in _tool_results(events):
        affected = result.metadata.get("affected_paths")
        if isinstance(affected, list):
            paths.extend(str(item) for item in affected if str(item).strip())
    return list(dict.fromkeys(paths))


def _pre_snapshots_from_events(events: list[AgentEvent]) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for result in _tool_results(events):
        raw_snapshots = result.metadata.get("checkpoint_pre_snapshots")
        if not isinstance(raw_snapshots, list):
            continue
        for item in raw_snapshots:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if path:
                snapshots.setdefault(path, dict(item))
    return snapshots


def _tool_results(events: list[AgentEvent]) -> list[ToolResult]:
    return [
        event.payload
        for event in events
        if event.kind == AgentEventType.TOOL_RESULT and isinstance(event.payload, ToolResult)
    ]


def _backfill_missing_snapshots(
    checkpoints: list[dict[str, Any]],
    pre_snapshots: dict[str, dict[str, Any]],
) -> None:
    if not pre_snapshots:
        return
    for checkpoint in checkpoints:
        files = checkpoint.setdefault("files", [])
        if not isinstance(files, list):
            checkpoint["files"] = files = []
        existing = {
            str(item.get("path", "")).strip()
            for item in files
            if isinstance(item, dict)
        }
        for path, file_snapshot in pre_snapshots.items():
            if path not in existing:
                files.append(dict(file_snapshot))


def _read_file_snapshot(workspace: Path, path_text: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    path = _resolve_workspace_path(workspace, path_text)
    rel_path = _relative_path(workspace, path)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        encoded: str | None = None
    except OSError:
        encoded = None
    else:
        encoded = base64.b64encode(content).decode("ascii")
    return {"path": rel_path, "content": encoded}


def _restore_files(workspace: Path, checkpoint: dict[str, Any]) -> tuple[int, list[str]]:
    workspace = workspace.resolve()
    errors: list[str] = []
    restored = 0
    for file_snapshot in _file_snapshots(checkpoint):
        path_text = str(file_snapshot.get("path", "")).strip()
        if not path_text:
            continue
        path = _resolve_workspace_path(workspace, path_text)
        try:
            path.relative_to(workspace)
        except ValueError:
            errors.append(f"Refusing to restore outside workspace: {path_text}")
            continue
        try:
            path.relative_to((workspace / ".nexus").resolve())
            errors.append(f"Refusing to restore managed state path: {path_text}")
            continue
        except ValueError:
            pass
        content = file_snapshot.get("content")
        try:
            if content is None:
                if path.exists():
                    path.unlink()
                    restored += 1
                continue
            if not isinstance(content, str):
                errors.append(f"Invalid checkpoint content for: {path_text}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(content.encode("ascii")))
            restored += 1
        except OSError as exc:
            errors.append(f"Failed to restore {path_text}: {exc}")
        except ValueError as exc:
            errors.append(f"Invalid checkpoint content for {path_text}: {exc}")
    return restored, errors


def _file_snapshots(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    files = checkpoint.get("files", [])
    if not isinstance(files, list):
        return []
    return [item for item in files if isinstance(item, dict)]


def _resolve_workspace_path(workspace: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path.resolve() if path.is_absolute() else (workspace / path).resolve()


def _relative_path(workspace: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace))
    except ValueError:
        return str(path.resolve())


def _checkpoint_summary(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user" and message.content.strip():
            return message.content.strip()[:80]
    return ""


def _session_summary(messages: list[Message]) -> str:
    for message in messages:
        if message.role == "user" and message.content.strip():
            return message.content.strip()
    return ""
