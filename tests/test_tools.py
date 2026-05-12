from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nexus.security import PermissionChecker, PermissionDecision
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.delegation import DelegationRuntime
from nexus.sandbox.agent_tool import SubagentDefinition
from nexus.tools.filesystem import WriteFileTool
from nexus.skills import Skill, SkillRegistry
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, MemoryTool, WriteNoteTool
from nexus.tools.subagents import (
    load_subagent_definitions,
    load_subagent_definitions_from_skills,
    register_skill_subagent_tools,
    register_subagent_tools,
)


@pytest.mark.asyncio
async def test_get_time_tool_returns_utc_timestamp(tool_context):
    result = await GetTimeTool().execute("call-1", {}, tool_context)

    assert result.tool_name == "get_time"
    assert "T" in result.output
    assert result.metadata["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_write_note_tool_writes_in_workspace(tool_context):
    result = await WriteNoteTool().execute(
        "call-2",
        {"path": "notes/todo.txt", "content": "ship it"},
        tool_context,
    )

    assert result.is_error is False
    assert (tool_context.working_directory / "notes/todo.txt").read_text(encoding="utf-8") == "ship it"


@pytest.mark.asyncio
async def test_write_note_tool_rejects_outside_workspace(tool_context):
    result = await WriteNoteTool().execute(
        "call-3",
        {"path": "../escape.txt", "content": "nope"},
        tool_context,
    )

    assert result.is_error is True
    assert "outside the current workspace" in result.output.lower()


@pytest.mark.asyncio
async def test_write_note_tool_rejects_large_content(tool_context):
    result = await WriteNoteTool(max_bytes=8).execute(
        "call-4",
        {"path": "notes/large.txt", "content": "this is too large"},
        tool_context,
    )

    assert result.is_error is True
    assert "larger than 8 bytes" in result.output.lower()


@pytest.mark.asyncio
async def test_memory_tool_persists_entries_as_dictionary(tmp_path, tool_context):
    memory_dir = tmp_path / ".nexus" / "memory"
    tool = MemoryTool(memory_dir=memory_dir)

    result = await tool.execute(
        "call-memory",
        {"action": "set", "key": "user_name", "value": "rishikesh"},
        tool_context,
    )

    payload = json.loads((memory_dir / "user_memory.json").read_text(encoding="utf-8"))

    assert result.is_error is False
    assert payload == {"entries": {"user_name": "rishikesh"}}


def test_permission_checker_denies_direct_nexus_memory_file_writes_with_memory_hint(tmp_path, tool_context):
    result = PermissionChecker().evaluate(
        WriteFileTool(),
        {"path": ".nexus/memory/user_name.md", "content": "rishikesh"},
        ExecutionMode.DEFAULT,
        context=tool_context,
    )

    assert result.decision is PermissionDecision.DENY
    assert "memory" in result.reason.lower()


@pytest.mark.asyncio
async def test_register_subagent_tools_registers_default_and_specialist_tools():
    registry = ToolRegistry()
    delegation = DelegationRuntime(worker_ids=["worker-1"], poll_interval=0.01, base_tool_registry=registry)
    await delegation.start()
    try:
        config = SimpleNamespace(delegation_enabled=True)
        definitions = [
            SubagentDefinition(
                name="explore",
                description="Investigate a focused codebase question.",
                goal_prompt="Explore the requested slice and summarize the result.",
            )
        ]

        count = register_subagent_tools(registry, delegation, config, definitions=definitions)

        assert count == 2
        assert registry.record("delegate_task").source == "agent"
        specialist = registry.record("subagent_explore")
        assert specialist.source == "agent"
        assert specialist.origin == "explore"
    finally:
        await delegation.shutdown()


@pytest.mark.asyncio
async def test_register_subagent_tools_skips_when_delegation_disabled():
    registry = ToolRegistry()
    config = SimpleNamespace(delegation_enabled=False)

    count = register_subagent_tools(registry, None, config)

    assert count == 0
    assert registry.records() == []


def test_load_subagent_definitions_builds_definition_objects():
    config = SimpleNamespace(
        delegation_subagents=[
            {
                "name": "explore",
                "description": "Investigate a focused codebase question.",
                "goal_prompt": "Read the relevant code and summarize the answer.",
                "allowed_tools": ["read_file", "glob", "grep"],
                "max_turns": 12,
                "timeout_seconds": 300,
            }
        ]
    )

    definitions = load_subagent_definitions(config)

    assert len(definitions) == 1
    assert definitions[0].name == "explore"
    assert definitions[0].allowed_tools == ["read_file", "glob", "grep"]
    assert definitions[0].max_turns == 12


def test_load_subagent_definitions_from_skills_uses_subagent_prefix():
    registry = SkillRegistry()
    registry.register(
        Skill(
            name="subagent-review",
            description="Review a focused code slice.",
            content="Inspect the selected code and report issues.",
        )
    )
    registry.register(
        Skill(
            name="nexus-agent",
            description="Builtin self-documentation skill.",
            content="Ignore me.",
        )
    )

    definitions = load_subagent_definitions_from_skills(registry)

    assert len(definitions) == 1
    assert definitions[0].name == "review"
    assert definitions[0].description == "Review a focused code slice."


@pytest.mark.asyncio
async def test_register_subagent_tools_respects_tool_filters():
    registry = ToolRegistry()
    delegation = DelegationRuntime(worker_ids=["worker-1"], poll_interval=0.01, base_tool_registry=registry)
    await delegation.start()
    try:
        config = SimpleNamespace(
            delegation_enabled=True,
            allowed_tools=["subagent_explore"],
            denied_tools=[],
        )
        definitions = [
            SubagentDefinition(
                name="explore",
                description="Investigate a focused codebase question.",
                goal_prompt="Explore the requested slice and summarize the result.",
            )
        ]

        count = register_subagent_tools(registry, delegation, config, definitions=definitions)

        assert count == 1
        assert registry.records()[0].name == "subagent_explore"
    finally:
        await delegation.shutdown()


@pytest.mark.asyncio
async def test_register_skill_subagent_tools_registers_skill_backed_workers():
    registry = ToolRegistry()
    delegation = DelegationRuntime(worker_ids=["worker-1"], poll_interval=0.01, base_tool_registry=registry)
    await delegation.start()
    try:
        config = SimpleNamespace(delegation_enabled=True, allowed_tools=[], denied_tools=[])
        skill_registry = SkillRegistry()
        skill_registry.register(
            Skill(
                name="subagent-review",
                description="Review a focused code slice.",
                content="Inspect the selected code and report issues.",
            )
        )

        count = register_skill_subagent_tools(registry, delegation, config, skill_registry)

        assert count == 1
        record = registry.record("subagent_review")
        assert record.source == "agent-skill"
        assert record.origin == "review"
    finally:
        await delegation.shutdown()
