from __future__ import annotations

from nexus.tools.subagents import get_builtin_subagent_definitions


def test_builtin_four_agent_defaults_match_supervisor_first_contract():
    definitions = {definition.name: definition for definition in get_builtin_subagent_definitions()}

    explorer = definitions["explorer"]
    coding = definitions["coding"]
    code_reviewer = definitions["code_reviewer"]
    impact_analyzer = definitions["impact_analyzer"]

    assert explorer.allowed_tools == ["read_file", "glob", "grep", "list_dir", "lsp", "git_diff", "git_status"]
    assert explorer.max_turns == 20

    coding_prompt = coding.goal_prompt.lower()
    assert "implement only the assigned change" in coding_prompt
    assert "do not choose broad verification scope yourself" in coding_prompt
    assert "run_formatter" in coding.allowed_tools
    assert "run_tests" not in coding.allowed_tools
    assert "bash" not in coding.allowed_tools
    assert coding.max_turns == 14

    reviewer_prompt = code_reviewer.goal_prompt.lower()
    assert "run only the scoped verification" in reviewer_prompt
    assert "likely related to the task" in reviewer_prompt
    assert "run_tests" in code_reviewer.allowed_tools
    assert "run_python_check" in code_reviewer.allowed_tools
    assert code_reviewer.max_turns == 8

    impact_prompt = impact_analyzer.goal_prompt.lower()
    assert "blast radius" in impact_prompt
    assert "manual validation" in impact_prompt
    assert impact_analyzer.max_turns == 10
