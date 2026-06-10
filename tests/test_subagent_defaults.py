from __future__ import annotations

from nexus.tools.subagents import get_builtin_subagent_definitions


def test_builtin_four_agent_defaults_match_supervisor_first_contract():
    definitions = {definition.name: definition for definition in get_builtin_subagent_definitions()}

    planning_analysis = definitions["planning_analysis"]
    execution = definitions["execution"]
    review = definitions["review"]
    verification = definitions["verification"]

    assert planning_analysis.allowed_tools == ["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"]
    assert planning_analysis.allowed_mcps == []
    assert planning_analysis.max_turns == 20

    coding_prompt = execution.goal_prompt.lower()
    assert "implement only the assigned change" in coding_prompt
    assert "do not choose broad verification scope yourself" in coding_prompt
    assert "run_formatter" in execution.allowed_tools
    assert "run_tests" not in execution.allowed_tools
    assert "bash" not in execution.allowed_tools
    assert execution.allowed_mcps == []
    assert execution.max_turns == 14

    reviewer_prompt = review.goal_prompt.lower()
    assert "run only the scoped verification" in reviewer_prompt
    assert "likely related to the task" in reviewer_prompt
    assert "run_tests" in review.allowed_tools
    assert "run_python_check" in review.allowed_tools
    assert review.allowed_mcps == []
    assert review.max_turns == 8

    impact_prompt = verification.goal_prompt.lower()
    assert "blast radius" in impact_prompt
    assert "manual validation" in impact_prompt
    assert verification.allowed_mcps == []
    assert verification.max_turns == 10
