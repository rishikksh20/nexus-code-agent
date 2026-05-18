from __future__ import annotations

from nexus.tools.subagents import get_builtin_subagent_definitions


def test_builtin_execution_and_verification_subagents_bound_bash_usage():
    definitions = {definition.name: definition for definition in get_builtin_subagent_definitions()}

    execution = definitions["execution"]
    verification = definitions["verification"]

    for definition in (execution, verification):
        prompt = definition.goal_prompt.lower()
        assert "never run servers, watchers, repls, or infinite loops in the foreground" in prompt
        assert "explicit timeouts" in prompt
        assert "do not retry the same command unchanged" in prompt

    assert execution.max_turns == 14
    assert verification.max_turns == 6
