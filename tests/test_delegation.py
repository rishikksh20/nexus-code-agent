from __future__ import annotations

import pytest

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import Message, RuntimeResponse, ToolCall
from nexus.runtime.delegation import DelegationRequest, DelegationRuntime, MessageKind, TaskStatus
from nexus.tools.base import ToolRegistry
from nexus.tools.builtin import GetTimeTool, WriteNoteTool


@pytest.mark.asyncio
async def test_delegation_runtime_assigns_task_and_collects_result():
    runtime = DelegationRuntime(worker_ids=["worker-1"], poll_interval=0.01)
    await runtime.start()
    try:
        task = await runtime.submit(
            DelegationRequest(
                title="Research notes",
                instructions="Inspect the documentation and summarize key findings.",
            )
        )
        completed = await runtime.wait_for_task(task.task_id, timeout=1.0)

        assert completed is not None
        assert completed.status is TaskStatus.COMPLETED
        assert "Echo:" in (completed.result_summary or "")
        assert any(message.kind is MessageKind.RESULT for message in runtime.mailbox.history(limit=20))
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_delegation_runtime_runs_inner_agent_with_restricted_tools():
    registry = ToolRegistry()
    registry.register(GetTimeTool(), source="core", origin="builtin")
    runtime = DelegationRuntime(
        worker_ids=["worker-1"],
        poll_interval=0.01,
        base_tool_registry=registry,
    )
    await runtime.start()
    try:
        task = await runtime.submit(
            DelegationRequest(
                title="Check time",
                instructions="Please check the time and report it.",
                allowed_tools=("get_time",),
            )
        )
        completed = await runtime.wait_for_task(task.task_id, timeout=1.0)

        assert completed is not None
        assert completed.status is TaskStatus.COMPLETED
        assert "Completed get_time:" in (completed.result_summary or "")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_delegation_runtime_routes_permission_through_coordinator():
    runtime = DelegationRuntime(worker_ids=["worker-1"], poll_interval=0.01)
    await runtime.start()
    try:
        task = await runtime.submit(
            DelegationRequest(
                title="Persist findings",
                instructions="Write the findings to disk.",
                permission_action="write_note",
                permission_reason="Need to persist findings",
            )
        )

        pending = None
        for _ in range(50):
            approvals = runtime.list_pending_permissions()
            if approvals:
                pending = approvals[0]
                break
            await runtime.wait_for_task(task.task_id, timeout=0.02)

        assert pending is not None
        assert runtime.tasks[task.task_id].status is TaskStatus.WAITING

        approved = await runtime.decide_permission(pending.correlation_id or pending.message_id, approved=True)
        completed = await runtime.wait_for_task(task.task_id, timeout=1.0)

        assert approved is True
        assert completed is not None
        assert completed.status is TaskStatus.COMPLETED
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_delegation_runtime_detects_resource_conflict_via_versions():
    runtime = DelegationRuntime(worker_ids=["worker-1", "worker-2"], poll_interval=0.01)
    await runtime.start()
    try:
        first = await runtime.submit(
            DelegationRequest(
                title="Update report A",
                instructions="Write report A.",
                claimed_resources=("notes/report.md",),
                permission_action="write_note",
                permission_reason="Need to update report.",
            )
        )
        second = await runtime.submit(
            DelegationRequest(
                title="Update report B",
                instructions="Write report B.",
                claimed_resources=("notes/report.md",),
                permission_action="write_note",
                permission_reason="Need to update report.",
            )
        )

        pending = None
        for _ in range(50):
            approvals = runtime.list_pending_permissions()
            if len(approvals) == 2:
                pending = approvals
                break
            await runtime.wait_for_task(first.task_id, timeout=0.02)

        assert pending is not None
        for message in pending:
            await runtime.decide_permission(message.correlation_id or message.message_id, approved=True)

        completed_first = await runtime.wait_for_task(first.task_id, timeout=1.0)
        completed_second = await runtime.wait_for_task(second.task_id, timeout=1.0)

        statuses = {completed_first.status, completed_second.status}
        assert TaskStatus.COMPLETED in statuses
        assert TaskStatus.FAILED in statuses
        failed = completed_first if completed_first.status is TaskStatus.FAILED else completed_second
        assert "Optimistic concurrency conflict" in (failed.error or "")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_delegation_runtime_reports_clarification_as_failure():
    registry = ToolRegistry()
    registry.register(WriteNoteTool(), source="core", origin="builtin")
    runtime = DelegationRuntime(
        worker_ids=["worker-1"],
        poll_interval=0.01,
        base_tool_registry=registry,
        model_client_factory=lambda: FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Need more detail."),
                    tool_calls=(
                        ToolCall(
                            call_id="clarify-1",
                            tool_name="write_note",
                            arguments={"content": "hello"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ]
        ),
    )
    await runtime.start()
    try:
        task = await runtime.submit(
            DelegationRequest(
                title="Ambiguous",
                instructions="Do the thing.",
                allowed_tools=("write_note",),
            )
        )
        completed = await runtime.wait_for_task(task.task_id, timeout=1.0)

        assert completed is not None
        assert completed.status is TaskStatus.FAILED
        assert "requires clarification" in (completed.error or "")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_delegation_worker_uses_workspace_root_for_tool_execution(tmp_path, monkeypatch):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    outside_cwd = tmp_path / "outside"
    outside_cwd.mkdir()
    monkeypatch.chdir(outside_cwd)

    registry = ToolRegistry()
    registry.register(WriteNoteTool(), source="core", origin="builtin")
    runtime = DelegationRuntime(
        worker_ids=["worker-1"],
        poll_interval=0.01,
        base_tool_registry=registry,
        workspace_root=workspace_root,
        model_client_factory=lambda: FakeModelClient(
            scripted=[
                RuntimeResponse(
                    message=Message(role="assistant", content="Writing the delegated note."),
                    tool_calls=(
                        ToolCall(
                            call_id="write-1",
                            tool_name="write_note",
                            arguments={"path": "notes/worker.txt", "content": "delegated output"},
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                RuntimeResponse(
                    message=Message(role="assistant", content="Writing the delegated note."),
                    tool_calls=(
                        ToolCall(
                            call_id="write-1",
                            tool_name="write_note",
                            arguments={"path": "notes/worker.txt", "content": "delegated output"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            ]
        ),
    )
    await runtime.start()
    try:
        task = await runtime.submit(
            DelegationRequest(
                title="Write delegated note",
                instructions="Write the note into the workspace.",
                allowed_tools=("write_note",),
            )
        )

        pending = None
        for _ in range(50):
            approvals = runtime.list_pending_permissions()
            if approvals:
                pending = approvals[0]
                break
            await runtime.wait_for_task(task.task_id, timeout=0.02)

        assert pending is not None
        await runtime.decide_permission(pending.correlation_id or pending.message_id, approved=True)
        completed = await runtime.wait_for_task(task.task_id, timeout=1.0)

        assert completed is not None
        assert completed.status is TaskStatus.COMPLETED
        assert (workspace_root / "notes" / "worker.txt").read_text(encoding="utf-8") == "delegated output"
        assert not (outside_cwd / "notes" / "worker.txt").exists()
    finally:
        await runtime.shutdown()
