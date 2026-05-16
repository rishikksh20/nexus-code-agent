from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from nexus.integrations.fake_model import FakeModelClient
from nexus.models import AgentEventType, ConfirmationKind, Message, ToolExecutionContext
from nexus.runtime.context_state import AgentContextRecord, ContextScope, estimate_messages
from nexus.runtime.agent import Agent
from nexus.hooks import HookEvent, HookExecutor
from nexus.security.manager import ApprovalManager, ApprovalScope
from nexus.tools.base import ToolRegistry


logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    REQUESTED = "requested"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.KILLED}


class MessageKind(str, Enum):
    COMMAND = "command"
    STATUS = "status"
    RESULT = "result"
    PERMISSION = "permission"
    APPROVAL = "approval"
    CONTROL = "control"


@dataclass(slots=True)
class AgentMessage:
    sender: str
    recipient: str
    kind: MessageKind
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    correlation_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    message_id: str = field(default_factory=lambda: uuid4().hex[:8])

    @classmethod
    def command(
        cls,
        *,
        sender: str,
        recipient: str,
        task_id: str,
        title: str,
        instructions: str,
        allowed_tools: list[str] | None = None,
        shared_context: list[str] | None = None,
        input_packet_ids: list[str] | None = None,
        permission_action: str | None = None,
        permission_reason: str | None = None,
    ) -> "AgentMessage":
        if allowed_tools:
            payload: dict[str, Any] = {
                "title": title,
                "instructions": instructions,
                "allowed_tools": list(allowed_tools),
            }
        else:
            payload = {"title": title, "instructions": instructions}
        if permission_action:
            payload["permission_action"] = permission_action
        if permission_reason:
            payload["permission_reason"] = permission_reason
        if shared_context:
            payload["shared_context"] = list(shared_context)
        if input_packet_ids:
            payload["input_packet_ids"] = list(input_packet_ids)
        return cls(
            sender=sender,
            recipient=recipient,
            kind=MessageKind.COMMAND,
            payload=payload,
            task_id=task_id,
        )

    @classmethod
    def status(
        cls,
        *,
        sender: str,
        task_id: str,
        status: TaskStatus,
        message: str,
    ) -> "AgentMessage":
        return cls(
            sender=sender,
            recipient="coordinator",
            kind=MessageKind.STATUS,
            task_id=task_id,
            payload={"status": status.value, "message": message},
        )

    @classmethod
    def result(
        cls,
        *,
        sender: str,
        task_id: str,
        success: bool,
        summary: str,
        context_snapshot: dict[str, Any] | None = None,
    ) -> "AgentMessage":
        payload: dict[str, Any] = {"success": success, "summary": summary}
        if context_snapshot is not None:
            payload["context_snapshot"] = context_snapshot
        return cls(
            sender=sender,
            recipient="coordinator",
            kind=MessageKind.RESULT,
            task_id=task_id,
            payload=payload,
        )

    @classmethod
    def permission_request(
        cls,
        *,
        sender: str,
        task_id: str,
        action: str,
        reason: str,
    ) -> "AgentMessage":
        message = cls(
            sender=sender,
            recipient="coordinator",
            kind=MessageKind.PERMISSION,
            task_id=task_id,
            payload={"action": action, "reason": reason},
        )
        message.correlation_id = message.message_id
        return message

    @classmethod
    def approval(
        cls,
        *,
        sender: str,
        recipient: str,
        task_id: str,
        approved: bool,
        correlation_id: str,
    ) -> "AgentMessage":
        return cls(
            sender=sender,
            recipient=recipient,
            kind=MessageKind.APPROVAL,
            task_id=task_id,
            correlation_id=correlation_id,
            payload={"approved": approved},
        )


class InMemoryMailbox:
    def __init__(
        self,
        *,
        hooks: HookExecutor | None = None,
        history_limit: int = 200,
    ) -> None:
        self._queues: dict[str, asyncio.Queue[AgentMessage]] = {}
        self._history: deque[AgentMessage] = deque(maxlen=history_limit)
        self._hooks = hooks

    async def send(self, message: AgentMessage) -> None:
        queue = self._queue_for(message.recipient)
        await queue.put(message)
        self._history.append(message)
        logger.info(
            "agent_message sender=%s recipient=%s kind=%s task_id=%s decision_id=%s",
            message.sender,
            message.recipient,
            message.kind.value,
            message.task_id or "-",
            message.correlation_id or "-",
        )
        if self._hooks is not None:
            await self._hooks.emit(
                HookEvent.NOTIFICATION,
                {
                    "event": "agent_message",
                    "sender": message.sender,
                    "recipient": message.recipient,
                    "kind": message.kind.value,
                    "task_id": message.task_id,
                    "decision_id": message.correlation_id,
                    "message_id": message.message_id,
                },
            )

    async def receive_all(self, recipient: str) -> list[AgentMessage]:
        queue = self._queue_for(recipient)
        items: list[AgentMessage] = []
        while not queue.empty():
            items.append(queue.get_nowait())
        return items

    async def wait_for_messages(self, recipient: str, timeout: float) -> list[AgentMessage]:
        queue = self._queue_for(recipient)
        try:
            first = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return []
        items = [first]
        while not queue.empty():
            items.append(queue.get_nowait())
        return items

    def history(self, *, participant: str | None = None, limit: int = 20) -> list[AgentMessage]:
        items = list(self._history)
        if participant is not None:
            items = [
                message
                for message in items
                if message.sender == participant or message.recipient == participant
            ]
        return items[-limit:]

    def pending_count(self, recipient: str) -> int:
        return self._queue_for(recipient).qsize()

    def _queue_for(self, recipient: str) -> asyncio.Queue[AgentMessage]:
        if recipient not in self._queues:
            self._queues[recipient] = asyncio.Queue()
        return self._queues[recipient]


@dataclass(slots=True)
class DelegationRequest:
    title: str
    instructions: str
    assigned_worker: str | None = None
    allowed_tools: tuple[str, ...] = ()
    claimed_resources: tuple[str, ...] = ()
    shared_context: tuple[str, ...] = ()
    input_packet_ids: tuple[str, ...] = ()
    permission_action: str | None = None
    permission_reason: str | None = None


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    title: str
    instructions: str
    status: TaskStatus = TaskStatus.REQUESTED
    assigned_worker: str | None = None
    result_summary: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    allowed_tools: tuple[str, ...] = ()
    claimed_resources: tuple[str, ...] = ()
    resource_versions: dict[str, int] = field(default_factory=dict)
    pending_decision_id: str | None = None
    shared_context: tuple[str, ...] = ()
    input_packet_ids: tuple[str, ...] = ()
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    version: int = 0

    def append_note(self, note: str) -> None:
        self.notes.append(note)
        self.version += 1


@dataclass(slots=True)
class WorkerState:
    worker_id: str
    status: str = "idle"
    current_task_id: str | None = None
    processed_messages: int = 0
    last_error: str | None = None


class ResourceVersionStore:
    def __init__(self) -> None:
        self._versions: dict[str, int] = {}
        self._reservations: dict[str, str] = {}

    def snapshot(self, resources: tuple[str, ...]) -> dict[str, int]:
        return {resource: self._versions.get(resource, 0) for resource in resources}

    def try_reserve(self, task_id: str, resources: tuple[str, ...]) -> tuple[bool, str | None]:
        for resource in resources:
            owner = self._reservations.get(resource)
            if owner is not None and owner != task_id:
                return False, resource
        for resource in resources:
            self._reservations[resource] = task_id
        return True, None

    def release(self, task_id: str, resources: tuple[str, ...]) -> None:
        for resource in resources:
            if self._reservations.get(resource) == task_id:
                self._reservations.pop(resource, None)

    def try_commit(self, resources: tuple[str, ...], expected_versions: dict[str, int]) -> tuple[bool, str | None]:
        for resource in resources:
            current = self._versions.get(resource, 0)
            if current != expected_versions.get(resource, 0):
                return False, resource
        for resource in resources:
            self._versions[resource] = self._versions.get(resource, 0) + 1
        return True, None


class WorkerAgent:
    def __init__(
        self,
        worker_id: str,
        mailbox: InMemoryMailbox,
        state: WorkerState,
        *,
        tool_registry_factory,
        model_client_factory,
        workspace_root: Path | None = None,
        model_name: str = "",
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        auto_confirm_read_only: bool = True,
    ) -> None:
        self.worker_id = worker_id
        self.mailbox = mailbox
        self.state = state
        self._tool_registry_factory = tool_registry_factory
        self._model_client_factory = model_client_factory
        self._workspace_root = (workspace_root or Path.cwd()).resolve()
        self._model_name = model_name
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._auto_confirm_read_only = auto_confirm_read_only
        self._waiting_decisions: dict[str, str] = {}
        self._approval_results: dict[str, asyncio.Future[bool]] = {}
        self._active_runs: dict[str, asyncio.Task[None]] = {}

    async def step(self, poll_interval: float) -> None:
        for message in await self.mailbox.wait_for_messages(self.worker_id, timeout=poll_interval):
            self.state.processed_messages += 1
            if message.kind is MessageKind.COMMAND:
                await self._handle_command(message)
            elif message.kind is MessageKind.APPROVAL:
                await self._handle_approval(message)
            elif message.kind is MessageKind.CONTROL:
                await self._handle_control(message)

    async def _handle_command(self, message: AgentMessage) -> None:
        task_id = message.task_id or "unknown"
        self.state.status = "running"
        self.state.current_task_id = task_id
        await self.mailbox.send(
            AgentMessage.status(
                sender=self.worker_id,
                task_id=task_id,
                status=TaskStatus.RUNNING,
                message=f"Started task '{message.payload.get('title', task_id)}'.",
            )
        )
        if task_id in self._active_runs and not self._active_runs[task_id].done():
            await self.mailbox.send(
                AgentMessage.result(
                    sender=self.worker_id,
                    task_id=task_id,
                    success=False,
                    summary="Worker is already running this task.",
                )
            )
            return
        self._active_runs[task_id] = asyncio.create_task(self._execute_command(message))

    async def _handle_approval(self, message: AgentMessage) -> None:
        task_id = message.task_id or ""
        expected = self._waiting_decisions.get(task_id)
        if expected is None or expected != message.correlation_id:
            return
        approved = bool(message.payload.get("approved", False))
        future = self._approval_results.get(task_id)
        if future is not None and not future.done():
            future.set_result(approved)

    async def _handle_control(self, message: AgentMessage) -> None:
        task_id = message.task_id or ""
        self._waiting_decisions.pop(task_id, None)
        future = self._approval_results.pop(task_id, None)
        if future is not None and not future.done():
            future.cancel()
        run_task = self._active_runs.pop(task_id, None)
        if run_task is not None:
            run_task.cancel()
        self.state.status = "idle"
        self.state.current_task_id = None
        await self.mailbox.send(
            AgentMessage.result(
                sender=self.worker_id,
                task_id=task_id,
                success=False,
                summary=str(message.payload.get("reason", "Task cancelled.")),
            )
        )

    async def _complete(
        self,
        message: AgentMessage,
        *,
        summary: str | None = None,
        context_snapshot: dict[str, Any] | None = None,
    ) -> None:
        task_id = message.task_id or "unknown"
        title = str(message.payload.get("title", task_id))
        instructions = str(message.payload.get("instructions", "")).strip()
        self.state.status = "idle"
        self.state.current_task_id = None
        self._active_runs.pop(task_id, None)
        await self.mailbox.send(
            AgentMessage.result(
                sender=self.worker_id,
                task_id=task_id,
                success=True,
                summary=summary or f"Finished {title}: {instructions}",
                context_snapshot=context_snapshot,
            )
        )

    async def _execute_command(self, command_message: AgentMessage) -> None:
        task_id = command_message.task_id or "unknown"
        title = str(command_message.payload.get("title", task_id))
        instructions = str(command_message.payload.get("instructions", "")).strip()
        permission_action = str(command_message.payload.get("permission_action", "")).strip()
        permission_reason = str(command_message.payload.get("permission_reason", "Need coordinator approval.")).strip()
        allowed_tools = tuple(str(tool_name) for tool_name in command_message.payload.get("allowed_tools", []))
        shared_context = tuple(str(item) for item in command_message.payload.get("shared_context", []) if str(item).strip())
        input_packet_ids = tuple(
            str(item)
            for item in command_message.payload.get("input_packet_ids", [])
            if str(item).strip()
        )

        try:
            if permission_action:
                approved = await self._request_permission(task_id, permission_action, permission_reason)
                if not approved:
                    self.state.status = "idle"
                    self.state.current_task_id = None
                    self._active_runs.pop(task_id, None)
                    await self.mailbox.send(
                        AgentMessage.result(
                            sender=self.worker_id,
                            task_id=task_id,
                            success=False,
                            summary="Coordinator denied the requested permission.",
                        )
                    )
                    return

            registry = self._tool_registry_factory(allowed_tools)
            agent = Agent(model_client=self._model_client_factory(), tool_registry=registry)
            history = [Message(role="user", content=instructions)]
            approval_manager = ApprovalManager()
            assistant_summary = ""
            context = ToolExecutionContext(
                session_id=f"worker-{self.worker_id}-{task_id}",
                working_directory=self._workspace_root,
                metadata={"worker_id": self.worker_id, "task_id": task_id},
            )
            context.metadata["allowed_tools"] = list(allowed_tools)
            context.metadata["context_scope"] = ContextScope.ISOLATED.value
            context.metadata["shared_context"] = list(shared_context)
            context.metadata["input_packet_ids"] = list(input_packet_ids)
            system_prompt = _worker_system_prompt(
                title,
                instructions,
                allowed_tools,
                shared_context=shared_context,
                input_packet_ids=input_packet_ids,
            )

            while True:
                events = [
                    event
                    async for event in agent.run(
                        history,
                        context,
                        system_prompt=system_prompt,
                        model_name=self._model_name or f"worker-{self.worker_id}",
                        approval_manager=approval_manager,
                        auto_confirm_read_only=self._auto_confirm_read_only,
                        temperature=self._temperature,
                        max_output_tokens=self._max_output_tokens,
                    )
                ]

                approval_request = None
                clarification_request = None
                committed_tool_calls = {
                    event.payload.call_id
                    for event in events
                    if event.kind == AgentEventType.TOOL_RESULT
                }
                for event in events:
                    if event.kind == "model_response":
                        assistant_message = event.payload.message
                        if assistant_message.tool_calls and not all(
                            tool_call.call_id in committed_tool_calls
                            for tool_call in assistant_message.tool_calls
                        ):
                            assistant_message = Message(
                                role=assistant_message.role,
                                content=assistant_message.content,
                                name=assistant_message.name,
                                tool_calls=tuple(
                                    tool_call for tool_call in assistant_message.tool_calls
                                    if tool_call.call_id in committed_tool_calls
                                ),
                                tool_call_id=assistant_message.tool_call_id,
                            )
                            if not assistant_message.content and not assistant_message.tool_calls:
                                continue
                        history.append(assistant_message)
                        if assistant_message.content:
                            assistant_summary = assistant_message.content
                            await self.mailbox.send(
                                AgentMessage.status(
                                    sender=self.worker_id,
                                    task_id=task_id,
                                    status=TaskStatus.RUNNING,
                                    message=f"Model response: {assistant_summary}",
                                )
                            )
                    elif event.kind == "tool_result":
                        history.append(
                            Message(
                                role="tool",
                                content=event.payload.output,
                                name=event.payload.tool_name,
                                tool_call_id=event.payload.call_id,
                            )
                        )
                        assistant_summary = event.payload.output
                        await self.mailbox.send(
                            AgentMessage.status(
                                sender=self.worker_id,
                                task_id=task_id,
                                status=TaskStatus.RUNNING,
                                message=f"Tool {event.payload.tool_name} completed.",
                            )
                        )
                    elif event.kind == "confirmation_requested":
                        if event.payload.kind is ConfirmationKind.APPROVAL:
                            approval_request = event.payload
                        else:
                            clarification_request = event.payload

                if clarification_request is not None:
                    await self.mailbox.send(
                        AgentMessage.result(
                            sender=self.worker_id,
                            task_id=task_id,
                            success=False,
                            summary=f"Worker requires clarification before continuing: {clarification_request.prompt}",
                        )
                    )
                    self.state.status = "idle"
                    self.state.current_task_id = None
                    self._active_runs.pop(task_id, None)
                    return

                if approval_request is not None:
                    approved = await self._request_permission(task_id, approval_request.tool_name, approval_request.reason)
                    if not approved:
                        return
                    approval_manager.record_approval(
                        approval_request.tool_name,
                        ApprovalScope.ONCE,
                        arguments=approval_request.arguments,
                    )
                    continue

                break

            context_snapshot = _worker_context_snapshot(
                worker_id=self.worker_id,
                task_id=task_id,
                title=title,
                history=history,
                allowed_tools=allowed_tools,
                shared_context=shared_context,
                input_packet_ids=input_packet_ids,
            )
            await self._complete(
                command_message,
                summary=f"Finished {title}: {assistant_summary or instructions}",
                context_snapshot=context_snapshot,
            )
        except asyncio.CancelledError:
            self.state.status = "idle"
            self.state.current_task_id = None
            self._active_runs.pop(task_id, None)
            raise
        except Exception as exc:
            self.state.status = "idle"
            self.state.current_task_id = None
            self.state.last_error = str(exc)
            self._active_runs.pop(task_id, None)
            await self.mailbox.send(
                AgentMessage.result(
                    sender=self.worker_id,
                    task_id=task_id,
                    success=False,
                    summary=f"Worker failed: {exc}",
                )
            )

    async def _request_permission(self, task_id: str, action: str, reason: str) -> bool:
        request = AgentMessage.permission_request(
            sender=self.worker_id,
            task_id=task_id,
            action=action,
            reason=reason,
        )
        self._waiting_decisions[task_id] = request.correlation_id or request.message_id
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._approval_results[task_id] = future
        self.state.status = "waiting"
        await self.mailbox.send(
            AgentMessage.status(
                sender=self.worker_id,
                task_id=task_id,
                status=TaskStatus.WAITING,
                message=f"Waiting for approval to use '{action}'.",
            )
        )
        await self.mailbox.send(request)
        try:
            approved = await future
        except asyncio.CancelledError:
            return False
        self._approval_results.pop(task_id, None)
        self._waiting_decisions.pop(task_id, None)
        if approved:
            self.state.status = "running"
        return approved


class DelegationRuntime:
    def __init__(
        self,
        *,
        worker_ids: list[str],
        hooks: HookExecutor | None = None,
        poll_interval: float = 0.05,
        history_limit: int = 200,
        base_tool_registry: ToolRegistry | None = None,
        model_client_factory=None,
        workspace_root: Path | None = None,
        model_name: str = "",
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        auto_confirm_read_only: bool = True,
    ) -> None:
        self.mailbox = InMemoryMailbox(hooks=hooks, history_limit=history_limit)
        self.poll_interval = poll_interval
        self.tasks: dict[str, TaskRecord] = {}
        self.pending_permissions: dict[str, AgentMessage] = {}
        self.resource_versions = ResourceVersionStore()
        self.base_tool_registry = base_tool_registry or ToolRegistry()
        self._model_client_factory = model_client_factory or (lambda: FakeModelClient())
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.model_name = model_name
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.auto_confirm_read_only = auto_confirm_read_only
        self.worker_states = {worker_id: WorkerState(worker_id=worker_id) for worker_id in worker_ids}
        self.workers = {
            worker_id: WorkerAgent(
                worker_id,
                self.mailbox,
                self.worker_states[worker_id],
                tool_registry_factory=self._build_worker_registry,
                model_client_factory=self._model_client_factory,
                workspace_root=self.workspace_root,
                model_name=self.model_name,
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                auto_confirm_read_only=self.auto_confirm_read_only,
            )
            for worker_id in worker_ids
        }
        self._worker_tasks: list[asyncio.Task] = []
        self._coordinator_task: asyncio.Task | None = None
        self._next_worker_index = 0
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._coordinator_task = asyncio.create_task(self._run_coordinator_loop())
        self._worker_tasks = [
            asyncio.create_task(self._run_worker_loop(worker)) for worker in self.workers.values()
        ]

    async def shutdown(self) -> None:
        self._running = False
        tasks = [task for task in [self._coordinator_task, *self._worker_tasks] if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker_tasks = []
        self._coordinator_task = None

    async def submit(self, request: DelegationRequest) -> TaskRecord:
        worker_id = request.assigned_worker or self._choose_worker()
        if worker_id not in self.workers:
            raise ValueError(f"Unknown worker: {worker_id}")
        task_id = uuid4().hex[:8]
        task = TaskRecord(
            task_id=task_id,
            title=request.title,
            instructions=request.instructions,
            assigned_worker=worker_id,
            allowed_tools=request.allowed_tools,
            claimed_resources=request.claimed_resources,
            shared_context=request.shared_context,
            input_packet_ids=request.input_packet_ids,
            resource_versions=self.resource_versions.snapshot(request.claimed_resources),
        )
        task.append_note(f"Assigned to {worker_id}.")
        self.tasks[task_id] = task
        reserved, conflict_resource = self.resource_versions.try_reserve(task_id, request.claimed_resources)
        if not reserved:
            task.status = TaskStatus.FAILED
            task.error = f"Resource '{conflict_resource}' is already reserved by another worker task."
            task.append_note(task.error)
            return task
        await self.mailbox.send(
            AgentMessage.command(
                sender="coordinator",
                recipient=worker_id,
                task_id=task_id,
                title=request.title,
                instructions=request.instructions,
                allowed_tools=list(request.allowed_tools),
                shared_context=list(request.shared_context),
                input_packet_ids=list(request.input_packet_ids),
                permission_action=request.permission_action,
                permission_reason=request.permission_reason,
            )
        )
        return task

    def list_tasks(self, *, only_active: bool = False) -> list[TaskRecord]:
        tasks = list(self.tasks.values())
        if only_active:
            tasks = [task for task in tasks if not task.status.is_terminal]
        return sorted(tasks, key=lambda item: item.task_id)

    def list_pending_permissions(self) -> list[AgentMessage]:
        return list(self.pending_permissions.values())

    def list_worker_states(self) -> list[WorkerState]:
        return list(self.worker_states.values())

    async def decide_permission(self, correlation_id: str, *, approved: bool) -> bool:
        request = self.pending_permissions.pop(correlation_id, None)
        if request is None:
            return False
        task = self.tasks.get(request.task_id or "")
        if task is not None:
            task.pending_decision_id = None
            task.append_note(
                f"Coordinator {'approved' if approved else 'denied'} {request.payload.get('action', 'action')}."
            )
            if approved:
                task.status = TaskStatus.RUNNING
        await self.mailbox.send(
            AgentMessage.approval(
                sender="coordinator",
                recipient=request.sender,
                task_id=request.task_id or "",
                approved=approved,
                correlation_id=correlation_id,
            )
        )
        return True

    async def wait_for_task(self, task_id: str, timeout: float = 2.0) -> TaskRecord | None:
        deadline = asyncio.get_event_loop().time() + timeout
        task = self.tasks.get(task_id)
        while task is not None and not task.status.is_terminal:
            if asyncio.get_event_loop().time() > deadline:
                return task
            await asyncio.sleep(self.poll_interval)
        return task

    async def _run_worker_loop(self, worker: WorkerAgent) -> None:
        while True:
            await worker.step(self.poll_interval)

    async def _run_coordinator_loop(self) -> None:
        while True:
            for message in await self.mailbox.wait_for_messages("coordinator", timeout=self.poll_interval):
                await self._handle_coordinator_message(message)

    async def _handle_coordinator_message(self, message: AgentMessage) -> None:
        task_id = message.task_id or ""
        task = self.tasks.get(task_id)
        if task is None:
            return

        if message.kind is MessageKind.STATUS:
            status_value = str(message.payload.get("status", task.status.value))
            try:
                task.status = TaskStatus(status_value)
            except ValueError:
                pass
            note = str(message.payload.get("message", "")).strip()
            if note:
                task.append_note(note)
            return

        if message.kind is MessageKind.PERMISSION:
            task.status = TaskStatus.WAITING
            pending_decision_id = message.correlation_id or message.message_id
            task.pending_decision_id = pending_decision_id
            task.append_note(
                f"Permission requested for {message.payload.get('action', 'action')}: {message.payload.get('reason', '')}"
            )
            self.pending_permissions[pending_decision_id] = message
            return

        if message.kind is MessageKind.RESULT:
            success = bool(message.payload.get("success", False))
            summary = str(message.payload.get("summary", "")).strip()
            snapshot = message.payload.get("context_snapshot")
            if isinstance(snapshot, dict):
                task.context_snapshot = snapshot
            if success:
                committed, conflict_resource = self.resource_versions.try_commit(
                    task.claimed_resources,
                    task.resource_versions,
                )
                self.resource_versions.release(task.task_id, task.claimed_resources)
                if not committed:
                    task.status = TaskStatus.FAILED
                    error = f"Optimistic concurrency conflict for resource '{conflict_resource}'."
                    task.error = error
                    task.append_note(error)
                    return
                task.status = TaskStatus.COMPLETED
                task.result_summary = summary
                task.append_note(summary)
                return
            self.resource_versions.release(task.task_id, task.claimed_resources)
            task.status = TaskStatus.FAILED
            error = summary or "Worker failed."
            task.error = error
            task.append_note(error)

    def _choose_worker(self) -> str:
        worker_ids = list(self.workers)
        if not worker_ids:
            raise ValueError("Delegation is enabled but no workers are configured.")
        worker_id = worker_ids[self._next_worker_index % len(worker_ids)]
        self._next_worker_index += 1
        return worker_id

    def _build_worker_registry(self, allowed_tools: tuple[str, ...]) -> ToolRegistry:
        registry = ToolRegistry()
        allowed = set(allowed_tools)
        for record in self.base_tool_registry.records():
            if allowed and record.name not in allowed:
                continue
            registry.register(record.tool, source=record.source, origin=record.origin)
        return registry


def _worker_system_prompt(
    title: str,
    instructions: str,
    allowed_tools: tuple[str, ...],
    *,
    shared_context: tuple[str, ...] = (),
    input_packet_ids: tuple[str, ...] = (),
) -> str:
    tools_text = ", ".join(allowed_tools) if allowed_tools else "all currently enabled tools"
    shared_text = (
        " Shared handoff context from dependent agents: "
        + " | ".join(item[:500] for item in shared_context)
        if shared_context
        else ""
    )
    packet_text = (
        " Input handoff packet ids: " + ", ".join(input_packet_ids)
        if input_packet_ids
        else ""
    )
    return (
        "You are a delegated Nexus worker. Execute only the assigned task, keep the scope narrow, "
        "and return a concise final answer for the coordinator. Your local conversation and tool history "
        "are isolated; only your final structured summary and context snapshot are shared back. "
        "Do not ask the user directly. If the task is blocked by missing information, return "
        "`status: needs_clarification` in your final JSON. "
        f"Available tools for this task: {tools_text}. "
        "Final answer format: JSON object with keys `status`, `summary`, `findings`, "
        "`changed_files`, `related_files`, `tests_run`, `risks`, `clarifications_needed`, "
        "and `recommended_next_action`. "
        f"Task title: {title}. Task instructions: {instructions}.{shared_text}{packet_text}"
    )


def _worker_context_snapshot(
    *,
    worker_id: str,
    task_id: str,
    title: str,
    history: list[Message],
    allowed_tools: tuple[str, ...],
    shared_context: tuple[str, ...],
    input_packet_ids: tuple[str, ...],
) -> dict[str, Any]:
    tool_calls = sum(len(message.tool_calls) for message in history if message.tool_calls)
    record = AgentContextRecord(
        agent_id=f"worker-{worker_id}-{task_id}",
        role="worker",
        scope=ContextScope.ISOLATED,
        summary=f"Worker completed '{title}'. Local context is isolated; only this snapshot is shared.",
        token_estimate=estimate_messages(history),
        message_count=len(history),
        shared_inputs=input_packet_ids,
        allowed_tools=allowed_tools,
        tool_call_count=tool_calls,
    )
    payload = record.to_dict()
    payload["input_packet_ids"] = list(input_packet_ids)
    payload["shared_context"] = list(shared_context)
    payload["isolation"] = {
        "local_history_shared": False,
        "shared_inputs": list(input_packet_ids),
        "shared_outputs": "final_summary_and_context_snapshot_only",
    }
    return payload
