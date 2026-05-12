from __future__ import annotations

from pathlib import Path

from nexus.config import load_config
from nexus.prompts import build_context_sections
from nexus.context import CarryOverState
from nexus.skills import load_skill_registry
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, WriteFileTool


def test_build_context_uses_live_execution_mode(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())

    sections = build_context_sections(
        config,
        registry,
        task_input="check time",
        execution_mode="plan",
    )

    assert "Mode: plan" in sections.environment


def test_build_context_includes_current_time_and_working_directory(tmp_path, monkeypatch):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    current_cwd = tmp_path / "cwd"
    current_cwd.mkdir()

    import nexus.prompts

    monkeypatch.setattr(nexus.prompts, "_current_utc_time", lambda: "2026-04-27T12:34:56+00:00")

    sections = build_context_sections(
        config,
        registry,
        task_input="check environment",
        current_working_directory=current_cwd,
    )

    assert "Current UTC time: 2026-04-27T12:34:56+00:00" in sections.environment
    assert f"Current working directory: {current_cwd.resolve()}" in sections.environment


def test_build_context_ignores_unreadable_knowledge_file(tmp_path, monkeypatch):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    config.knowledge_file.parent.mkdir(parents=True, exist_ok=True)
    config.knowledge_file.write_text("hello", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(GetTimeTool())

    original_read_text = Path.read_text

    def _raise_read_error(path: Path, *args, **kwargs):
        if path == config.knowledge_file:
            raise OSError("boom")
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _raise_read_error)

    sections = build_context_sections(config, registry, task_input="check time")

    assert sections.project_notes == [f"Project: {config.project_name}"]


def test_build_context_includes_active_skill_and_carry_over(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    skill_root = tmp_path / "global" / "skills" / "review"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Review skill\n\nAlways review carefully.", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    skill_registry = load_skill_registry(config.skills_dir)

    sections = build_context_sections(
        config,
        registry,
        task_input="review this change",
        skill_registry=skill_registry,
        active_skills=["review"],
        carry_over=CarryOverState(summarized_history=["Earlier context compacted."], active_constraints=["Stay concise."]),
    )

    assert any("Active Skill: review" in item for item in sections.skills)
    assert "Earlier context compacted." in sections.carry_over


def test_base_instruction_prefers_read_only_tools_for_repo_explanations(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    registry.register(WriteFileTool())

    sections = build_context_sections(
        config,
        registry,
        task_input="scan this repo and explain the structure",
    )

    assert "treat the task as read-only by default" in sections.base_instruction
    assert "Do **not** call mutating tools unless the user explicitly asks" in sections.base_instruction


def test_base_instruction_mentions_hidden_path_read_restrictions(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())

    sections = build_context_sections(
        config,
        registry,
        task_input="inspect config files",
    )

    assert "Hidden/private dot-path reads are blocked by default" in sections.base_instruction
    assert "Never rely on reading `.nexus`" in sections.base_instruction


def test_context_tools_label_read_only_vs_mutating(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    registry = ToolRegistry()
    registry.register(GetTimeTool())
    registry.register(WriteFileTool())

    sections = build_context_sections(config, registry, task_input="describe tools")

    assert any("[core] [read-only] get_time" in entry for entry in sections.tools)
    assert any("[core] [mutating] write_file" in entry for entry in sections.tools)
