from __future__ import annotations

from nexus.models import Message
from nexus.context import CarryOverState, ContextCompactor, TokenEstimator, compact_messages, prune_tool_outputs
from nexus.memory.store import MemoryEntry
from nexus.config import load_config
from nexus.memory.store import MemoryStore
from nexus.runtime.execution import ExecutionMode
from nexus.runtime.repl_state import ReplState, _load_all_memory
from nexus.runtime.sessions import SessionStore, new_snapshot
from nexus.tools.base import ToolRegistry
from rich.console import Console
from nexus.runtime.context_state import (
    AgentSessionState,
    TaskContext,
    append_artifact_record,
    append_context_packet,
    append_multi_agent_event,
    load_multi_agent_state,
    make_artifact_record,
    make_context_packet,
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


def test_prepare_turn_compaction_metadata_counts_carry_over_entries_without_len_error(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=new_snapshot("carry-over-count"),
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True, width=120),
        carry_over=CarryOverState(
            pinned_facts=["fact"],
            summarized_history=["summary-1", "summary-2"],
            active_constraints=["constraint"],
        ),
    )

    prepared = state.prepare_turn("continue", turn_id="turn-1", trace_id="trace-1")

    assert prepared.context.metadata["context_compaction"]["carry_over_entries"] == 4


def test_prepare_turn_exposes_durable_session_metadata_to_tools(tmp_path):
    config = load_config(tmp_path, global_root=tmp_path / "global")
    session = new_snapshot("durable-tool-metadata")
    state = ReplState(
        config=config,
        mode=ExecutionMode.DEFAULT,
        session=session,
        session_store=SessionStore(config.session_dir),
        tool_registry=ToolRegistry(),
        memory_store=MemoryStore(config.memory_dir),
        console=Console(record=True, no_color=True, width=120),
    )

    prepared = state.prepare_turn("inspect auth", turn_id="turn-1", trace_id="trace-1")

    assert prepared.context.metadata["session_metadata"] is session.metadata
    assert prepared.context.metadata["supervisor_task_input"] == "inspect auth"


def test_load_all_memory_uses_single_store_read_path():
    class Store:
        def __init__(self):
            self.load_all_calls = 0

        def load_all(self):
            self.load_all_calls += 1
            return [MemoryEntry(key="alpha", content="remember this")]

        def list_keys(self):
            raise AssertionError("list_keys should not be used by prompt memory loading")

        def load(self, key):
            raise AssertionError("load should not be used by prompt memory loading")

    store = Store()

    assert _load_all_memory(store) == ["alpha: remember this"]
    assert store.load_all_calls == 1


def test_multi_agent_state_helpers_create_stable_packet_ids_and_records():
    metadata = {}

    first = make_context_packet(
        metadata=metadata,
        source_agent="planner",
        target_agent="execution",
        packet_type="planning_summary",
        summary="Plan ready.",
    )
    append_context_packet(metadata, first)
    second = make_context_packet(
        metadata=metadata,
        source_agent="test",
        target_agent="execution",
        packet_type="test_failure",
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


def test_agent_state_upsert_refreshes_context_projection():
    metadata = {}

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
