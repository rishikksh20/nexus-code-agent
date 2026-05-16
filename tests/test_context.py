from __future__ import annotations

from nexus.models import Message
from nexus.context import CarryOverState, ContextCompactor, TokenEstimator, compact_messages, prune_tool_outputs
from nexus.runtime.context_state import (
    AgentSessionState,
    TaskContext,
    append_artifact_record,
    append_context_packet,
    append_multi_agent_event,
    load_multi_agent_state,
    make_artifact_record,
    make_context_packet,
    make_test_failure_packet,
    upsert_agent_state,
    upsert_task_context,
)


def test_compact_messages_keeps_assistant_with_tool_results_at_recent_boundary():
    messages = [
        Message(role="user", content="older"),
        Message(role="assistant", content="calling tool"),
        Message(role="tool", content="tool output"),
        Message(role="user", content="latest"),
    ]

    compacted = compact_messages(messages, max_tokens=100, estimator=TokenEstimator(), keep_recent=2)

    roles = [message.role for message in compacted]
    tool_index = roles.index("tool")
    assert compacted[tool_index - 1].role == "assistant"
    assert roles[-2:] == ["tool", "user"]


def test_compact_messages_drops_orphaned_tool_when_budget_cannot_fit_assistant_pair():
    messages = [
        Message(role="user", content="older"),
        Message(role="assistant", content="calling tool"),
        Message(role="tool", content="tool output"),
        Message(role="user", content="latest"),
    ]

    compacted = compact_messages(messages, max_tokens=2, estimator=TokenEstimator(), keep_recent=1)

    assert [message.role for message in compacted] == ["user"]


def test_context_compactor_summarizes_only_messages_before_safe_recent_boundary():
    messages = [
        Message(role="user", content="older request"),
        Message(role="assistant", content="calling tool"),
        Message(role="tool", content="first result"),
        Message(role="tool", content="second result"),
        Message(role="user", content="latest request"),
    ]
    carry_over = CarryOverState()
    compactor = ContextCompactor(TokenEstimator(), soft_limit=1, hard_limit=100)

    compacted, updated = compactor.compact(messages, carry_over, keep_recent=2)

    assert [message.role for message in compacted] == ["assistant", "tool", "tool", "user"]
    assert updated.summarized_history
    assert "1 messages" in updated.summarized_history[-1]


def test_prune_tool_outputs_replaces_frozen_messages_in_place():
    messages = [
        Message(role="user", content="older request"),
        Message(role="assistant", content="calling tool"),
        Message(role="tool", content="x" * 120, name="read_file", tool_call_id="call-1"),
        Message(role="user", content="latest request"),
    ]

    pruned = prune_tool_outputs(messages, protect_tokens=0, minimum_tokens=1)

    assert pruned == 1
    assert messages[2].content.startswith("[Tool output cleared")


def test_multi_agent_state_helpers_create_stable_packet_ids_and_records():
    metadata = {}

    first = make_context_packet(
        metadata=metadata,
        source_agent="planner",
        target_agent="execution",
        packet_type="planner_dag",
        summary="Plan ready.",
    )
    append_context_packet(metadata, first)
    second = make_test_failure_packet(
        metadata=metadata,
        task_id="verify",
        summary="Typecheck failed.",
        failure_summary="Syntax error.",
    )
    append_context_packet(metadata, second)
    artifact = make_artifact_record(
        metadata=metadata,
        artifact_type="typecheck_output",
        task_id="verify",
        producer_agent="test",
        summary="Typecheck failed.",
        content="full output",
    )
    append_artifact_record(metadata, artifact)
    upsert_task_context(metadata, TaskContext(task_id="verify", role="test", objective="Run checks"))
    upsert_agent_state(
        metadata,
        AgentSessionState(
            agent_id="test",
            role="test",
            task_id="verify",
            status="completed",
            working_summary="Checked output.",
        ),
    )
    append_multi_agent_event(metadata, "TEST_FAILED", task_id="verify", packet_id=second.packet_id)

    state = load_multi_agent_state(metadata)

    assert first.packet_id == "packet-0001"
    assert second.packet_id == "packet-0002"
    assert artifact.artifact_id == "artifact-0001"
    assert state.tasks["verify"].objective == "Run checks"
    assert state.agents["test"].working_summary == "Checked output."
    assert state.events[-1].event_type == "TEST_FAILED"
    assert metadata["multi_agent"]["state"]["packets"][1]["packet_type"] == "test_failure"
    assert metadata["multi_agent_context"]["agents"]["test"]["summary"] == "Checked output."


def test_agent_state_upsert_refreshes_context_projection_and_filters_plain_shared_text():
    metadata = {
        "multi_agent_context": {
            "agents": {
                "execution": {
                    "agent_id": "execution",
                    "role": "execution",
                    "summary": "old summary",
                    "shared_inputs": ["plain text context"],
                }
            },
            "packets": [],
        }
    }

    legacy = load_multi_agent_state(metadata)
    assert legacy.agents["execution"].input_packet_ids == ()

    upsert_agent_state(
        metadata,
        AgentSessionState(
            agent_id="execution",
            role="execution",
            task_id="execute",
            status="completed",
            working_summary="new summary",
            input_packet_ids=("packet-0001",),
            tool_call_count=2,
        ),
    )

    record = metadata["multi_agent_context"]["agents"]["execution"]
    assert record["summary"] == "new summary"
    assert record["shared_inputs"] == ["packet-0001"]
    assert record["tool_call_count"] == 2


def test_multi_agent_state_loads_legacy_metadata_without_migration_error():
    metadata = {
        "multi_agent": {
            "shared_state": {
                "dag": {
                    "goal": "Legacy goal",
                    "nodes": [
                        {
                            "id": "execute",
                            "role": "execution",
                            "objective": "Do it",
                            "dependencies": [],
                        }
                    ],
                    "execution_order": ["execute"],
                },
                "context_packets": [
                    {
                        "packet_id": "legacy-packet",
                        "source_agent": "planner",
                        "target_agent": "execution",
                        "summary": "Legacy packet.",
                    }
                ],
            }
        },
        "multi_agent_context": {
            "agents": {
                "supervisor": {
                    "agent_id": "supervisor",
                    "role": "supervisor",
                    "summary": "Legacy supervisor.",
                    "token_estimate": 10,
                    "message_count": 1,
                }
            },
            "packets": [],
        },
    }

    state = load_multi_agent_state(metadata)

    assert state.objective == "Legacy goal"
    assert state.tasks["execute"].role == "execution"
    assert state.packets[0].packet_id == "legacy-packet"
    assert state.agents["supervisor"].working_summary == "Legacy supervisor."
