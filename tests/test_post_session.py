from __future__ import annotations

import json

from nexus.config import load_config
from nexus.models import Message
from nexus.runtime.post_session import extract_facts, run_post_session_updates
from nexus.runtime.sessions import new_snapshot


def test_extract_facts_finds_commands_and_envs():
    facts = extract_facts("Use ./venv/bin/activate then pytest and npm run build")

    assert {item["type"] for item in facts} >= {"venv_path", "test_command", "build_command"}


def test_post_session_updates_workspace_and_profile_files(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    snapshot = new_snapshot("session-1")
    snapshot.messages = [
        Message(role="user", content="Run pytest and summarize the repo."),
        Message(role="assistant", content="I will verify the repo."),
        Message(role="tool", content="ok", name="get_time"),
    ]
    snapshot.summary = "Run pytest and summarize the repo."

    run_post_session_updates(config, snapshot, active_skills=["review"])

    facts_payload = json.loads((config.local_root / "facts.json").read_text(encoding="utf-8"))
    workspaces_payload = json.loads((config.global_root / "workspaces.json").read_text(encoding="utf-8"))
    profile_text = (config.global_root / "profile.md").read_text(encoding="utf-8")
    knowledge_text = config.knowledge_file.read_text(encoding="utf-8")

    assert facts_payload["session_count"] == 1
    assert facts_payload["source_of_truth"] == []
    assert workspaces_payload["workspaces"][str(config.workspace_root)]["project_name"] == config.project_name
    assert "Preferred tool: get_time" in profile_text
    assert "Recent Tasks" in knowledge_text


def test_post_session_preserves_existing_source_of_truth_without_inventing_defaults(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.local_root.mkdir(parents=True, exist_ok=True)
    (config.local_root / "facts.json").write_text(
        json.dumps({"source_of_truth": ["README.md"]}),
        encoding="utf-8",
    )
    snapshot = new_snapshot("session-2")
    snapshot.messages = [Message(role="user", content="Summarize this workspace.")]

    run_post_session_updates(config, snapshot)

    facts_payload = json.loads((config.local_root / "facts.json").read_text(encoding="utf-8"))
    assert facts_payload["source_of_truth"] == ["README.md"]
