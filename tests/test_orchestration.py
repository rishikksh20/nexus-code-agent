from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.runtime.agent import Agent
from nexus.runtime.orchestration import run_orchestrated_turn
from nexus.tools.base import ToolRegistry


@pytest.mark.asyncio
async def test_orchestrated_turn_calls_shared_turn_runner_path():
    state = SimpleNamespace(config=SimpleNamespace(agent_mode="basic"), session=SimpleNamespace(metadata={}))
    agent = Agent(FakeModelClient(), ToolRegistry())
    calls = []

    async def fake_turn_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return ["shared-path"]

    result = await run_orchestrated_turn(
        state,
        agent,
        prompt_text="hello",
        turn_runner=fake_turn_runner,
    )

    assert result == ["shared-path"]
    assert calls[0][1]["prompt_text"] == "hello"
    assert state.session.metadata == {}


@pytest.mark.asyncio
async def test_advanced_agent_mode_still_uses_normal_supervisor_turn():
    registry = ToolRegistry()
    state = SimpleNamespace(
        config=SimpleNamespace(agent_mode="advanced", model_name="fake", max_output_tokens=4096),
        session=SimpleNamespace(metadata={}),
        tool_registry=registry,
    )
    agent = Agent(FakeModelClient(), registry)
    prompts = []

    async def fake_turn_runner(*args, **kwargs):
        prompts.append(kwargs["prompt_text"])
        return ["normal-supervisor-path"]

    result = await run_orchestrated_turn(
        state,
        agent,
        prompt_text="Implement this complex plan",
        turn_runner=fake_turn_runner,
    )

    assert result == ["normal-supervisor-path"]
    assert prompts == ["Implement this complex plan"]
    assert state.session.metadata == {}
